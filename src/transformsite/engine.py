"""Motor de conversa: roteia, responde com fonte, coleta slots, confirma, executa, encaminha."""

from __future__ import annotations

import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .config import ProjectConfig
from .kb.index import terms
from .llm import LLM, LLMError
from .messages import InMsg, OutMsg
from .rag import RAG
from .services.schema import Service, Slot, load_services, render
from .services.types import EXTRACTORS, OPPORTUNISTIC, ex_enum, ex_name, ex_yes_no, fmt, now, partial_datetime
from .store import Store
from .tools import Registry, ToolContext, default_registry

CANCEL = {"cancelar", "cancela", "cancele", "parar", "desistir", "desisto", "sair", "esquece", "deixa pra la", "recomecar"}
GREET = {"oi", "ola", "bom dia", "boa tarde", "boa noite", "hello", "hi", "hey", "e ai", "opa", "menu", "ajuda", "inicio", "start", "/start", "/menu"}
QUESTION_START = re.compile(
    r"^(como|qual|quais|quando|onde|quanto|quantos|quantas|o que|oque|por que|porque|pq|tem |voces|vcs|posso|pode|existe|precisa|preciso saber|e se|da pra|e possivel)\b"
)
SKIP = {"pular", "pula", "nao tenho", "nao sei", "prefiro nao", "sem", "nenhum", "nenhuma", "-"}
PII_INBOUND = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")
RESUME_AFTER = 6 * 3600
EXPIRE_AFTER = 7 * 86400

ROUTER_PROMPT = """Classifique a mensagem de um cliente. Opções de serviço disponíveis:
{services}
Responda JSON {{"tipo": "servico"|"pergunta"|"saudacao"|"humano"|"outro", "servico": "<id ou vazio>"}}.
- "servico" só se o cliente QUER EXECUTAR um dos serviços acima agora (ex.: marcar, enviar, solicitar).
- "pergunta" para dúvidas/informações (inclusive dúvidas sobre um serviço).
- "humano" se pede para falar com uma pessoa.
Mensagem: {text}"""

EXTRACT_PROMPT = """Extraia da mensagem do cliente os valores para os campos abaixo. Só inclua campos claramente
informados na mensagem; não invente. Data/hora: copie como o cliente escreveu (ex.: "quinta às 10").
Campos:
{fields}
Mensagem: {text}
Responda JSON {{"<campo>": "<valor>"}}."""


def _n(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c)).strip()


@dataclass
class Notifier:
    """Envia avisos à equipe (handoff). Canais registram senders: notifier.senders['telegram'] = fn(chat_id, text)."""

    cfg: ProjectConfig
    senders: dict[str, Callable[[str, str], Any]] = field(default_factory=dict)
    log: list[tuple[str, str]] = field(default_factory=list)

    def notify(self, text: str, subject: str = "transformsite") -> None:
        from .mailer import send_email

        for t in self.cfg.handoff.targets:
            kind, _, dest = t.partition(":")
            self.log.append((t, text))
            try:
                if kind == "email":
                    send_email(self.cfg, dest, subject, text)
                elif kind in self.senders:
                    self.senders[kind](dest, text)
            except Exception:  # aviso nunca derruba a conversa
                pass


class Agent:
    def __init__(
        self,
        cfg: ProjectConfig,
        llm: LLM | None = None,
        store: Store | None = None,
        registry: Registry | None = None,
        services: dict[str, Service] | None = None,
        rag: RAG | None = None,
    ):
        self.cfg = cfg
        self.llm = llm or LLM(cfg.llm)
        self.store = store or Store(cfg.db_path)
        self.tools = registry or default_registry(cfg)
        if services is None:
            services, errors = load_services(cfg.services_dir, only_approved=True, tools=self.tools)
            self.load_errors = errors
        else:
            self.load_errors = []
        self.services = services
        self.rag = rag or RAG(cfg, self.llm)
        self.notifier = Notifier(cfg)
        self.outbound_hooks: list[Callable[[dict, OutMsg], None]] = []  # p/ painel enviar resposta humana

    @property
    def smart(self) -> bool:
        return self.cfg.llm.provider != "fake"

    # ================= entrada =================
    def handle(self, msg: InMsg) -> list[OutMsg]:
        t0 = time.time()
        sess = self.store.session(msg.channel, msg.user_id, msg.meta)
        sid = sess["id"]
        if not self.store.add_message(sid, "in", msg.text, author="user", dedup=msg.msg_id):
            self.store.event(sid, "duplicate_dropped")
            return []
        if sess["status"] == "human":
            self.notifier.notify(f"[{sid}] cliente: {msg.text}", subject=f"Mensagem na conversa {sid}")
            self.store.event(sid, "to_human")
            return []
        try:
            outs = self._dispatch(sess, msg)
        except Exception as e:  # nunca deixar o cliente sem resposta
            self.store.event(sid, "error", error=str(e)[:300])
            outs = [OutMsg(f"Tive um problema técnico aqui. {self._fallback()}", kind="error")]
        for o in outs:
            self.store.add_message(sid, "out", o.text, author="bot", data={"kind": o.kind, "citations": o.citations, "choices": o.choices})
        self.store.event(sid, "turn", ms=int((time.time() - t0) * 1000), kinds=[o.kind for o in outs])
        return outs

    def _fallback(self) -> str:
        return f"Se preferir, fale com a equipe: {self.cfg.fallback_contact}." if self.cfg.fallback_contact else ""

    # ================= roteamento =================
    def _dispatch(self, sess: dict, msg: InMsg) -> list[OutMsg]:
        state = sess["state"]
        sid = sess["id"]
        text = (msg.text or "").strip()
        nt = _n(text).strip(" .!?")
        outs: list[OutMsg] = []

        if not state.get("welcomed"):
            state["welcomed"] = True
            self.store.save_state(sid, state)
            if self.cfg.welcome:
                outs.append(OutMsg(self.cfg.welcome, kind="welcome"))
                self.store.event(sid, "session_start")
                if nt in GREET or not nt:
                    outs.append(self._menu())
                    return outs
            else:
                self.store.event(sid, "session_start")

        # pedido explícito de humano
        if self._is_handoff_trigger(nt, state):
            return outs + [self._handoff(sess, "pedido do cliente", text)]

        # oferta pendente de handoff (após "não sei")
        if state.get("offer") == "handoff":
            yn = ex_yes_no(text, None) if not msg.choice_id else msg.choice_id == "sim"
            state.pop("offer", None)
            q = state.pop("offer_q", "")
            self.store.save_state(sid, state)
            if yn is True:
                return outs + [self._handoff(sess, "bot não soube responder", q)]
            if yn is False:
                return outs + [OutMsg("Tudo bem! Posso ajudar com outra coisa?", choices=self._menu().choices)]

        # serviço em andamento
        if state.get("service"):
            age = time.time() - sess["updated_at"]
            if age > EXPIRE_AFTER:
                state = {"welcomed": True}
                self.store.save_state(sid, state)
            else:
                if nt in CANCEL or (nt.startswith("cancel") and len(nt) < 20):
                    svc = self.services.get(state["service"])
                    self.store.event(sid, "service_cancelled", service=state["service"], slot=state.get("current"))
                    self.store.save_state(sid, {"welcomed": True})
                    return outs + [OutMsg(f"Ok, cancelei {('o pedido de ' + svc.intent.description.lower()) if svc else 'o pedido'}. Posso ajudar em algo mais?", kind="service")]
                prefix = None
                if age > RESUME_AFTER:
                    prefix = self._resume_summary(state)
                    self.store.event(sid, "service_resumed", service=state["service"])
                res = self._continue_service(sess, state, msg)
                if prefix:
                    res = [OutMsg(prefix, kind="service")] + res
                return outs + res

        if nt in GREET or nt in ("", "oi tudo bem", "tudo bem"):
            return outs + [self._menu()]
        if ex_yes_no(nt, None) is not None and len(nt.split()) <= 2:
            m = self._menu()
            m.text = "Posso ajudar em algo mais? " + m.text
            return outs + [m]

        # aviso de dado sensível fora de serviço
        if PII_INBOUND.search(text):
            self.store.event(sid, "pii_warning")
            outs.append(OutMsg("Por segurança, não envie CPF, cartão ou documentos por aqui fora de um atendimento que peça isso. Já descartei esse dado.", kind="reply"))
            text = PII_INBOUND.sub("[removido]", text)

        kind, svc_name = self._route(text, msg)
        if kind == "servico" and svc_name in self.services:
            return outs + self._start_service(sess, self.services[svc_name], msg)
        if kind == "humano":
            return outs + [self._handoff(sess, "pedido do cliente", text)]
        if kind == "saudacao":
            return outs + [self._menu()]
        return outs + self._answer(sess, text)

    def _is_handoff_trigger(self, nt: str, state: dict) -> bool:
        trig = list(self.cfg.handoff.triggers)
        svc = self.services.get(state.get("service", ""))
        if svc:
            trig += svc.handoff.triggers
        return any(re.search(rf"\b{re.escape(_n(t))}\b", nt) for t in trig) and len(nt.split()) <= 12

    def _route(self, text: str, msg: InMsg) -> tuple[str, str]:
        if msg.choice_id and msg.choice_id.startswith("svc:"):
            return "servico", msg.choice_id[4:]
        nt = _n(text)
        # escolha pelo número/rótulo do menu
        menu = list(self.services.values())
        m = re.fullmatch(r"\s*(\d{1,2})\s*", nt)
        if m and 1 <= int(m.group(1)) <= len(menu):
            return "servico", menu[int(m.group(1)) - 1].service
        best, score = self._keyword_scores(text)
        if score >= 0.6:
            return "servico", best
        if self.smart and self.services:
            listing = "\n".join(
                f"- {s.service}: {s.intent.description} (ex.: {'; '.join(s.intent.examples[:3])})" for s in self.services.values()
            )
            try:
                d = self.llm.chat_json([{"role": "user", "content": ROUTER_PROMPT.format(services=listing, text=text)}])
                tipo = str(d.get("tipo", "pergunta"))
                if tipo == "servico" and d.get("servico") in self.services:
                    return "servico", d["servico"]
                if tipo in ("humano", "saudacao"):
                    return tipo, ""
            except LLMError:
                pass
        elif score >= 0.4:
            return "servico", best
        return "pergunta", ""

    def _keyword_scores(self, text: str) -> tuple[str, float]:
        tt = set(terms(text))
        if QUESTION_START.match(_n(text)) or text.strip().endswith("?"):
            penal = 0.5  # pergunta sobre o serviço ≠ querer executar
        else:
            penal = 1.0
        best, score = "", 0.0
        for s in self.services.values():
            for ex in s.intent.examples + s.intent.keywords:
                et = set(terms(ex))
                if not et:
                    continue
                sc = len(tt & et) / len(et) * penal
                if sc > score:
                    best, score = s.service, sc
        return best, score

    def _menu(self) -> OutMsg:
        choices = [{"id": f"svc:{s.service}", "label": s.intent.description} for s in self.services.values()]
        txt = "Como posso ajudar? Pode me perguntar qualquer coisa"
        txt += " ou escolher um atendimento:" if choices else "."
        return OutMsg(txt, choices=choices, kind="menu")

    # ================= perguntas (RAG) =================
    def _answer(self, sess: dict, text: str) -> list[OutMsg]:
        sid = sess["id"]
        hist = ""
        state = sess["state"]
        if state.get("last_q"):
            hist = f"Pergunta anterior: {state['last_q']}"
        ans = self.rag.answer(text, history=hist if self._is_followup(text) else "")
        state["last_q"] = text[:300]
        if ans.nao_sei:
            state["offer"] = "handoff"
            state["offer_q"] = text[:500]
            self.store.save_state(sid, state)
            self.store.event(sid, "nao_sei", error=ans.error)
            return [
                OutMsg(
                    "Não encontrei essa informação na nossa base e prefiro não chutar. Quer que eu passe sua pergunta para a equipe?",
                    choices=[{"id": "sim", "label": "Sim, passar para a equipe"}, {"id": "nao", "label": "Não, obrigado"}],
                    kind="nao_sei",
                )
            ]
        self.store.save_state(sid, state)
        self.store.event(sid, "answer", cited=len(ans.citations), cached=ans.cached, conflito=ans.conflito)
        return [OutMsg(ans.text, citations=ans.citations, kind="answer")]

    def _is_followup(self, text: str) -> bool:
        return len(text.split()) <= 5 and bool(re.match(r"^(e |e o|e a|e quanto|e como|e onde|mas |entao)", _n(text)))

    # ================= handoff =================
    def _handoff(self, sess: dict, reason: str, text: str) -> OutMsg:
        sid = sess["id"]
        queue = "atendimento"
        svc = self.services.get(sess["state"].get("service", ""))
        if svc:
            queue = svc.handoff.queue
        last = self.store.messages(sid, 12)
        transcript = "\n".join(f"{m['direction']}: {m['text']}" for m in last)
        tid = self.store.ticket(sid, queue, "handoff", f"{reason}: {text[:80]}", transcript, {"reason": reason})
        who = sess["meta"].get("name") or sess["user_id"]
        self.notifier.notify(
            f"Handoff {tid} ({reason}) — {sess['channel']}:{who}\nÚltima mensagem: {text}\n\n{transcript[-1500:]}",
            subject=f"[{tid}] Atendimento humano solicitado",
        )
        self.store.event(sid, "handoff", reason=reason, ticket=tid)
        return OutMsg(
            f"Encaminhei para a equipe (protocolo {tid}). Alguém responde por aqui em horário de atendimento ({self.cfg.handoff.hours}). {self._fallback()}".strip(),
            kind="handoff",
        )

    # ================= serviços =================
    def _start_service(self, sess: dict, svc: Service, msg: InMsg) -> list[OutMsg]:
        state = {"welcomed": True, "service": svc.service, "slots": {}, "attempts": {}, "phase": "collect", "started": time.time()}
        if svc.auth_method != "none" and svc.auth_method != "channel":
            state["auth_ok"] = False
        self.store.event(sess["id"], "service_started", service=svc.service)
        # prefill
        for s in svc.slots:
            if s.prefill_from:
                key = s.prefill_from.split(".", 1)[-1].replace("user_", "")
                v = sess["meta"].get(key)
                if v:
                    val = EXTRACTORS[s.type](str(v), s)
                    if val is not None:
                        state["slots"][s.name] = val
        # a própria frase inicial pode já trazer dados ("quero agendar quinta às 10")
        self._extract_into(svc, state, msg.text, current=None, initial=True)
        intro = OutMsg(f"Certo, vamos a {svc.intent.description.lower()}. Para cancelar a qualquer momento, diga \"cancelar\".", kind="service")
        for s in svc.slots:
            if s.name in state["slots"] and s.constraints.get("validate_tool"):
                v = self._validate_with_tool(sess, svc, s, state)
                if v:
                    return [intro] + v
        return [intro] + self._next(sess, svc, state)

    def _active_slots(self, svc: Service, state: dict) -> list[Slot]:
        out = []
        for s in svc.slots:
            if s.when:
                m = re.fullmatch(r"\{(\w+)\}\s*(==|!=)\s*(\S+)", s.when.strip())
                if m:
                    v = str(state["slots"].get(m.group(1), ""))
                    ok = (v == m.group(3)) if m.group(2) == "==" else (v != m.group(3))
                    if not ok:
                        continue
            out.append(s)
        return out

    def _next(self, sess: dict, svc: Service, state: dict, pre: list[OutMsg] | None = None) -> list[OutMsg]:
        sid = sess["id"]
        outs = list(pre or [])
        # autenticação OTP antes dos slots sensíveis
        if state.get("auth_ok") is False:
            ident = svc.auth.get("identifier") if isinstance(svc.auth, dict) else None
            if ident and ident in state["slots"] and state.get("phase") != "otp":
                return outs + self._send_otp(sess, svc, state)
            if state.get("phase") == "otp":
                self.store.save_state(sid, state)
                return outs + [OutMsg("Digite o código de 6 dígitos que enviei.", kind="service")]
        for s in self._active_slots(svc, state):
            if s.name in state["slots"]:
                continue
            if not s.required and s.name in state.get("skipped", []):
                continue
            state["current"] = s.name
            state["phase"] = "collect"
            prompt = s.prompt + ("" if s.required else " (ou \"pular\")")
            choices: list[dict] = []
            if s.options_from:
                try:
                    opts = self._options(sess, svc, s, state)
                except Exception as e:
                    self.store.event(sid, "tool_error", tool=s.options_from, error=str(e)[:200])
                    opts = []
                state["options"] = opts
                choices = opts
                if s.type == "datetime" and opts:
                    prompt += " Tenho estes horários livres (ou escreva outro dia/horário):"
                elif s.type == "datetime" and not opts:
                    prompt += " Não encontrei horários livres nos próximos dias."
            elif s.type == "enum" and s.values:
                choices = [{"id": v, "label": (s.labels or {}).get(v, v.replace("_", " "))} for v in s.values]
                state["options"] = choices
            else:
                state.pop("options", None)
            self.store.save_state(sid, state)
            return outs + [OutMsg(prompt, choices=choices, kind="service")]
        # todos preenchidos
        if svc.confirm:
            state["phase"] = "confirm"
            state.pop("current", None)
            state.pop("options", None)
            self.store.save_state(sid, state)
            return outs + [
                OutMsg(
                    render(svc.confirm.template, self._ctx(sess, svc, state)),
                    choices=[{"id": "sim", "label": "Sim, confirmar"}, {"id": "nao", "label": "Não, corrigir"}],
                    kind="service",
                )
            ]
        return outs + self._execute(sess, svc, state)

    def _options(self, sess, svc, s: Slot, state) -> list[dict]:
        tool = s.options_from.split(":", 1)[1]
        args = {}
        if state.get("partial_date"):
            args["day"] = state["partial_date"]
        res = self.tools.call(tool, self._tctx(sess, svc), n=int(s.constraints.get("options", 3)), **args)
        return [{"id": str(o["id"]), "label": str(o.get("label", o["id"]))} for o in res]

    def _tctx(self, sess, svc) -> ToolContext:
        return ToolContext(self.cfg, self.store, sess, svc.service)

    def _ctx(self, sess, svc, state, result: dict | None = None, labels: bool = True) -> dict:
        """Contexto dos templates. `labels=False` mantém os valores brutos (para ações/sistemas legados)."""
        ctx: dict[str, Any] = {}
        for k, v in state["slots"].items():
            s = svc.slot(k)
            if labels and s and s.type == "enum" and s.labels:
                ctx[k] = s.labels.get(v, v)
            else:
                ctx[k] = v
        ctx["session_id"] = sess["id"]
        ctx["today"] = now().date().isoformat()
        ctx["channel"] = sess["channel"]
        ctx["user_id"] = sess["user_id"]
        ctx["result"] = result or {}
        return ctx

    def _resume_summary(self, state: dict) -> str:
        svc = self.services.get(state["service"])
        got = ", ".join(state["slots"].keys()) or "nada ainda"
        desc = svc.intent.description.lower() if svc else state["service"]
        return f"Retomando seu pedido de {desc}. Já tenho: {got}."

    def _continue_service(self, sess: dict, state: dict, msg: InMsg) -> list[OutMsg]:
        svc = self.services.get(state["service"])
        sid = sess["id"]
        if svc is None:
            self.store.save_state(sid, {"welcomed": True})
            return [OutMsg("Esse atendimento foi atualizado. Vamos recomeçar?", choices=self._menu().choices)]
        text = msg.text or ""
        phase = state.get("phase", "collect")

        if phase == "otp":
            return self._check_otp(sess, svc, state, text)

        if phase == "confirm":
            yn = (msg.choice_id == "sim") if msg.choice_id in ("sim", "nao") else ex_yes_no(text, None)
            if yn is True:
                return self._execute(sess, svc, state)
            # correção direta ("não, o telefone é 51 98888-7777")
            changed = self._extract_into(svc, state, text, current=None, correction=True)
            if changed:
                self.store.event(sid, "slot_corrected", service=svc.service, slots=changed)
                return self._next(sess, svc, state, [OutMsg(f"Corrigido: {', '.join(changed)}.", kind="service")])
            if yn is False:
                state["phase"] = "correct"
                self.store.save_state(sid, state)
                ch = [{"id": f"fix:{s.name}", "label": s.name.replace("_", " ")} for s in self._active_slots(svc, state)]
                return [OutMsg("O que você quer corrigir? Pode escrever o dado certo direto também.", choices=ch, kind="service")]
            if self._looks_question(text):
                return self._answer_inline(sess, svc, state, text)
            return [OutMsg("Não entendi. Confirma? Responda \"sim\" ou \"não\".", choices=[{"id": "sim", "label": "Sim"}, {"id": "nao", "label": "Não"}], kind="service")]

        if phase == "correct":
            target = None
            if msg.choice_id and msg.choice_id.startswith("fix:"):
                target = msg.choice_id[4:]
            else:
                for s in svc.slots:
                    if _n(s.name.replace("_", " ")) in _n(text):
                        target = s.name
                        break
            changed = self._extract_into(svc, state, text, current=None, correction=True)
            if changed:
                return self._next(sess, svc, state, [OutMsg(f"Corrigido: {', '.join(changed)}.", kind="service")])
            if target:
                state["slots"].pop(target, None)
                state.pop("partial_date", None)
                return self._next(sess, svc, state)
            state["phase"] = "confirm"
            return self._next(sess, svc, state)

        # ---- coleta ----
        cur = svc.slot(state.get("current", "")) if state.get("current") else None
        filled_before = set(state["slots"])
        if cur and not cur.required and _n(text).strip(" .!") in SKIP:
            state.setdefault("skipped", []).append(cur.name)
            return self._next(sess, svc, state)
        # opção escolhida (botão / número / rótulo)
        if cur and state.get("options"):
            picked = self._pick_option(state["options"], msg, cur)
            if picked is not None:
                state["slots"][cur.name] = picked
        correction = bool(re.match(r"^(nao|na verdade|errei|corrig)", _n(text)))
        changed = self._extract_into(svc, state, text, current=cur, correction=correction)
        new = set(state["slots"]) - filled_before
        # validação via ferramenta (ex.: horário livre)
        if cur and cur.name in new and cur.constraints.get("validate_tool"):
            v = self._validate_with_tool(sess, svc, cur, state)
            if v:
                return v
        if cur and cur.name not in state["slots"]:
            if not new and not changed and self._looks_question(text):
                return self._answer_inline(sess, svc, state, text)
            # data sem hora → oferece horários daquele dia
            if cur.type == "datetime":
                p = partial_datetime(text)
                if p["date"] and not p["time"]:
                    state["partial_date"] = p["date"]
                    return self._next(sess, svc, state)
            att = state["attempts"].get(cur.name, 0) + 1
            state["attempts"][cur.name] = att
            self.store.event(sid, "slot_retry", service=svc.service, slot=cur.name, attempt=att)
            if att >= 3:
                self.store.save_state(sid, {"welcomed": True})
                return [self._handoff(sess, f"não consegui coletar '{cur.name}'", text)]
            self.store.save_state(sid, state)
            hint = self._hint(cur, text)
            return [OutMsg(f"{hint} {cur.prompt}".strip(), choices=state.get("options") or [], kind="service")]
        state.pop("partial_date", None) if cur and cur.type == "datetime" and cur.name in state["slots"] else None
        pre = []
        if changed:
            pre.append(OutMsg(f"Corrigido: {', '.join(changed)}.", kind="service"))
        return self._next(sess, svc, state, pre)

    def _hint(self, s: Slot, text: str) -> str:
        return {
            "email": "Esse e-mail não parece válido.",
            "phone_br": "Não reconheci o telefone (use DDD + número).",
            "cpf": "Esse CPF não é válido.",
            "cep": "Não reconheci o CEP.",
            "datetime": "Não entendi a data e o horário.",
            "date": "Não entendi a data.",
            "enum": "Não reconheci a opção.",
            "number": "Preciso de um número.",
        }.get(s.type, "Não entendi.")

    def _pick_option(self, options: list[dict], msg: InMsg, s: Slot):
        if msg.choice_id:
            for o in options:
                if o["id"] == msg.choice_id:
                    return o["id"]
        nt = _n(msg.text or "")
        m = re.fullmatch(r"\s*(?:opcao\s*)?(\d{1,2})\s*[).]?\s*", nt)
        if m and 1 <= int(m.group(1)) <= len(options):
            return options[int(m.group(1)) - 1]["id"]
        for o in options:
            if _n(o["label"]) == nt or _n(o["id"]) == nt:
                return o["id"]
        if s.type == "datetime":
            from .services.types import parse_time

            tm = parse_time(msg.text or "")
            if tm and not partial_datetime(msg.text or "")["date"]:
                hits = [o for o in options if o["id"].endswith(f"T{tm[0]:02d}:{tm[1]:02d}")]
                if len(hits) == 1:
                    return hits[0]["id"]
        return None

    def _validate_with_tool(self, sess, svc, s: Slot, state) -> list[OutMsg] | None:
        tool = s.constraints["validate_tool"]
        try:
            r = self.tools.call(tool, self._tctx(sess, svc), start=state["slots"][s.name])
        except Exception as e:
            r = {"ok": False, "motivo": str(e)[:100]}
        if r.get("ok"):
            return None
        bad = state["slots"].pop(s.name)
        self.store.event(sess["id"], "slot_invalid", service=svc.service, slot=s.name)
        state["partial_date"] = bad[:10]
        outs = self._next(sess, svc, state)
        if outs and not outs[0].choices:  # dia lotado/fechado → próximos livres
            state.pop("partial_date", None)
            outs = self._next(sess, svc, state)
        outs[0].text = f"{fmt(s.type, bad)} não dá: {r.get('motivo', 'indisponível')}. " + outs[0].text
        return outs

    def _looks_question(self, text: str) -> bool:
        nt = _n(text)
        return text.strip().endswith("?") or bool(QUESTION_START.match(nt))

    def _answer_inline(self, sess, svc, state, text) -> list[OutMsg]:
        ans = self.rag.answer(text)
        self.store.event(sess["id"], "question_in_service", service=svc.service, nao_sei=ans.nao_sei)
        if ans.nao_sei:
            first = OutMsg("Não encontrei isso na nossa base; posso passar para a equipe depois. Vamos continuar:", kind="nao_sei")
        else:
            first = OutMsg(ans.text, citations=ans.citations, kind="answer")
        nxt = self._next(sess, svc, state)
        if nxt:
            nxt[0].text = "Voltando ao seu pedido: " + nxt[0].text
        return [first] + nxt

    def _extract_into(self, svc: Service, state: dict, text: str, current: Slot | None, correction=False, initial=False) -> list[str]:
        """Preenche slots a partir do texto. Retorna nomes de slots já preenchidos que foram alterados."""
        slots = state["slots"]
        active = self._active_slots(svc, state)
        changed: list[str] = []
        found: dict[str, Any] = {}
        # 1) slot atual pelo tipo
        if current and current.name not in slots:
            v = self._extract_slot(current, text, alone=True)
            if v is not None:
                found[current.name] = v
        # 2) oportunista: tipos inequívocos em qualquer slot
        for s in active:
            if s.name in found or (s.name in slots and not correction):
                continue
            if s is current:
                continue
            v = None
            if s.type in OPPORTUNISTIC:
                v = EXTRACTORS[s.type](text, s)
            elif s.type == "enum" and s.values and (correction or initial or len(text.split()) > 3):
                v = ex_enum(text, s) if not re.fullmatch(r"\s*\d+\s*", text) else None
            elif s.type == "text" and s.name in ("nome", "name", "nome_completo"):
                v = ex_name(text, s)
            if v is not None and (s.name not in slots or slots[s.name] != v):
                found[s.name] = v
        # 3) LLM multi-slot (mensagens longas ou quando nada saiu)
        if self.smart and (len(text.split()) >= 6 or (current and current.name not in found and not initial)):
            missing = [s for s in active if s.name not in found and (correction or s.name not in slots)]
            if missing:
                fields = "\n".join(
                    f"- {s.name} ({s.type}{': ' + ', '.join(s.values) if s.values else ''}): {s.prompt}" for s in missing
                )
                try:
                    d = self.llm.chat_json([{"role": "user", "content": EXTRACT_PROMPT.format(fields=fields, text=text)}])
                except LLMError:
                    d = {}
                for s in missing:
                    raw = d.get(s.name) if isinstance(d, dict) else None
                    if raw in (None, "", [], {}):
                        continue
                    v = self._extract_slot(s, str(raw), alone=True, from_llm=True)
                    if v is not None and (s.type != "text" or _n(str(raw)) in _n(text) or s is current):
                        found[s.name] = v
        for k, v in found.items():
            s = svc.slot(k)
            if s.max_len and isinstance(v, str):
                v = v[: s.max_len]
            if s.validate_regex and isinstance(v, str) and not re.fullmatch(s.validate_regex, v):
                continue
            if k in slots and slots[k] != v:
                changed.append(k)
            slots[k] = v
        return changed

    def _extract_slot(self, s: Slot, text: str, alone: bool, from_llm=False):
        if s.type == "text":
            if not alone:
                return None
            if s.name in ("nome", "name", "nome_completo"):
                nm = ex_name(text, s)
                if nm:
                    return nm
                t = text.strip().strip(".!")
                if len(t.split()) <= 6 and not any(ch.isdigit() for ch in t) and not self._looks_question(t):
                    return t.title() if t.islower() else t
                return None
            return text.strip() or None
        return EXTRACTORS[s.type](text, s)

    # ---- OTP ----
    def _send_otp(self, sess, svc, state) -> list[OutMsg]:
        ident_name = svc.auth.get("identifier")
        ident = state["slots"][ident_name]
        s = svc.slot(ident_name)
        contact = None
        if "auth.lookup" in self.tools:
            try:
                contact = self.tools.call("auth.lookup", self._tctx(sess, svc), identifier=ident).get("email")
            except Exception:
                contact = None
        if contact is None and s and s.type == "email":
            contact = ident
        if not contact:
            self.store.save_state(sess["id"], {"welcomed": True})
            return [self._handoff(sess, "cadastro não localizado para validação", f"{ident_name} informado")]
        code = f"{random.SystemRandom().randint(0, 999999):06d}"
        state["otp"] = {"code": code, "exp": time.time() + 600, "tries": 0}
        state["phase"] = "otp"
        self.store.save_state(sess["id"], state)
        from .mailer import send_email

        send_email(self.cfg, contact, f"Seu código: {code}", f"Seu código de confirmação é {code}. Ele vale por 10 minutos.")
        self.store.event(sess["id"], "otp_sent", service=svc.service)
        masked = re.sub(r"^(.).*(@.*)$", r"\1***\2", contact)
        return [OutMsg(f"Enviei um código de 6 dígitos para {masked}. Digite o código aqui.", kind="service")]

    def _check_otp(self, sess, svc, state, text) -> list[OutMsg]:
        otp = state.get("otp") or {}
        m = re.search(r"\b(\d{6})\b", text)
        if time.time() > otp.get("exp", 0):
            state.pop("otp", None)
            state["phase"] = "collect"
            return self._send_otp(sess, svc, state)
        if m and m.group(1) == otp.get("code"):
            state["auth_ok"] = True
            state.pop("otp", None)
            state["phase"] = "collect"
            self.store.event(sess["id"], "otp_ok", service=svc.service)
            return self._next(sess, svc, state, [OutMsg("Código confirmado.", kind="service")])
        otp["tries"] = otp.get("tries", 0) + 1
        if otp["tries"] >= 3:
            self.store.save_state(sess["id"], {"welcomed": True})
            return [self._handoff(sess, "falha na validação do código", "3 tentativas")]
        self.store.save_state(sess["id"], state)
        return [OutMsg("Código incorreto. Tente de novo.", kind="service")]

    # ---- execução ----
    def _execute(self, sess, svc: Service, state) -> list[OutMsg]:
        sid = sess["id"]
        ctx = self._ctx(sess, svc, state)
        raw = self._ctx(sess, svc, state, labels=False)
        result: dict = {}
        if svc.action:
            args = render(svc.action.args, raw)
            idem = render(svc.action.idempotency_key, raw) if svc.action.idempotency_key else None
            prev = self.store.action_done(f"{svc.service}:{idem}") if idem else None
            try:
                if prev is not None:
                    result = prev
                    self.store.event(sid, "action_deduplicated", service=svc.service)
                else:
                    result = self.tools.call(svc.action.tool, self._tctx(sess, svc), **args) or {}
                    if not isinstance(result, dict):
                        result = {"value": result}
                    if idem:
                        self.store.action_save(f"{svc.service}:{idem}", svc.action.tool, result)
                self.store.audit(session=sid, service=svc.service, version=svc.version, tool=svc.action.tool, args=args, ok=True, result=result)
            except Exception as e:
                self.store.audit(session=sid, service=svc.service, version=svc.version, tool=svc.action.tool, args=args, ok=False, error=str(e)[:300])
                self.store.event(sid, "service_failed", service=svc.service, error=str(e)[:300])
                self.store.save_state(sid, {"welcomed": True, "service": None})
                outs = [OutMsg(render(svc.on_failure.reply, ctx) or "Não consegui concluir.", kind="error")]
                if svc.on_failure.handoff is not None:
                    outs.append(self._handoff(sess, f"falha em {svc.service}: {str(e)[:80]}", ""))
                return outs
        ctx["result"] = result
        dur = time.time() - state.get("started", time.time())
        self.store.event(sid, "service_completed", service=svc.service, seconds=round(dur, 1))
        self.store.save_state(sid, {"welcomed": True})
        for n in svc.on_success.notify:
            kind, _, dest = n.partition(":")
            if kind == "email":
                from .mailer import send_email

                send_email(self.cfg, dest, f"[{svc.service}] concluído", render(svc.confirm.template if svc.confirm else svc.service, ctx))
        out = OutMsg(render(svc.on_success.reply, ctx) or "Pronto!", kind="service_done")
        if svc.on_success.attach:
            out.attachments.append({"url": render(svc.on_success.attach, ctx)})
        return [out]

    # ================= painel: humano assume / devolve =================
    def takeover(self, sid: str) -> None:
        self.store.set_status(sid, "human")
        self.store.event(sid, "takeover")

    def release(self, sid: str) -> None:
        self.store.set_status(sid, "bot")
        self.store.event(sid, "release")

    def human_reply(self, sid: str, text: str, agent_name: str = "equipe") -> OutMsg:
        out = OutMsg(text, kind="human")
        self.store.add_message(sid, "out", text, author=f"human:{agent_name}")
        sess = self.store.get_session(sid)
        for hook in self.outbound_hooks:
            hook(sess, out)
        return out
