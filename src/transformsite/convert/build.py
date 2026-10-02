"""Campos brutos (`RawForm`) → dict `service/1` + YAML comentado.

Regras: tipo semântico por tipo HTML e por nome/rótulo; `review: pending` SEMPRE;
`confidence` por slot; ação `http.post_form` (HTML) com os NOMES ORIGINAIS dos campos,
ou `email.enviar` (PDF).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..services.schema import SLOT_TYPES
from .html_form import RawField, RawForm

RESERVED = {"session_id", "today", "result", "channel", "user_id"}
CONF_HIGH, CONF_MID, CONF_LOW = 0.9, 0.6, 0.3


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    return "".join(c for c in s if not unicodedata.combining(c))


def snake(s: str, fallback: str = "campo") -> str:
    s = re.sub(r"\[(\w*)\]", r"_\1", s or "")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^a-z0-9]+", "_", norm(s)).strip("_")
    s = re.sub(r"_+", "_", s)
    s = re.sub(r"(^|_)e_mail(?=_|$)", r"\1email", s)
    if not s:
        s = fallback
    if not s[0].isalpha():
        s = f"{fallback}_{s}"
    return s[:50].rstrip("_")


# ---------- inferência de tipo ----------
SEMANTIC: list[tuple[str, str]] = [
    (r"\bcpf\b", "cpf"),
    (r"\bcep\b|codigo postal", "cep"),
    (r"e-?mail|correio eletronico", "email"),
    (r"telefone|celular|whats|\bfone\b|\btel\b|contato telefonico", "phone_br"),
    (r"data e hora|data/hora|horario|agendamento|data_hora|datahora", "datetime"),
    (r"nascimento|\bdata\b|\bdt\b|\bnasc\b", "date"),
    (r"quantidade|\bqtd|\bqtde|numero de (pessoas|participantes|alunos|funcionarios)|\bidade\b|\bvalor\b|\brenda\b|salari|metragem|\barea\b", "number"),
]


def infer_type(f: RawField) -> str:
    if f.kind in ("select", "radio", "checkbox_group"):
        return "enum" if f.options else "text"
    if f.kind == "checkbox":
        return "yes_no"
    explicit = {"email": "email", "tel": "phone_br", "date": "date", "datetime-local": "datetime",
                "number": "number", "range": "number"}
    if f.kind in explicit:
        return explicit[f.kind]
    if f.kind == "textarea":
        return "text"
    hay = norm(f"{f.name} {f.label}").replace("_", " ")
    hay_raw = norm(f"{f.name} {f.label}")
    for rx, t in SEMANTIC:
        if re.search(rx, hay) or re.search(rx, hay_raw):
            return t
    return "text"


def confidence(f: RawField, kind: str) -> float:
    if kind == "pdf_text" or f.label_source == "pdf_text":
        return CONF_LOW
    if f.label_source in ("label", "aria", "legend"):
        return CONF_HIGH
    if f.label_source == "tooltip":  # /TU do AcroForm = rótulo explícito
        return CONF_HIGH
    if f.label_source in ("placeholder", "title", "prev_text"):
        return CONF_MID
    return CONF_LOW


# ---------- prompts e textos ----------
def make_prompt(label: str, type_: str, group: str | None = None) -> str:
    lab = (label or "este dado").rstrip("?: ")
    ctx = f" ({group})" if group and norm(group) not in norm(lab) else ""
    if type_ == "yes_no":
        return f"{lab}{ctx}? (sim/não)"
    if type_ == "enum":
        return f"Escolha: {lab}{ctx}"
    return f"Por favor, informe: {lab}{ctx}"


def _strip_braces(s: str) -> str:
    return (s or "").replace("{", "(").replace("}", ")")


def intent_examples(title: str, slots: list[dict]) -> list[str]:
    t = norm(title).strip()
    base = re.sub(r"^(formulario|form|ficha)( de)? ", "", t).strip() or "atendimento"
    ex = [f"quero {base}" if not base.startswith("quero") else base, f"preciso de {base}", base]
    verbs = {
        "contato": ["quero falar com vocês", "enviar uma mensagem"],
        "agend": ["quero agendar", "marcar um horário"],
        "matricul": ["quero me matricular", "fazer matrícula"],
        "boleto": ["segunda via do boleto", "perdi meu boleto"],
        "ouvidoria": ["quero fazer uma reclamação", "registrar manifestação"],
        "inscri": ["quero me inscrever", "fazer inscrição"],
        "orcamento": ["quero um orçamento", "quanto custa"],
        "cadastr": ["quero me cadastrar", "fazer cadastro"],
        "cancel": ["quero cancelar", "cancelar meu contrato"],
        "reembolso": ["quero pedir reembolso", "reembolso de despesa"],
        "bolsa": ["quero uma bolsa", "inscrição na bolsa"],
        "visita": ["agendar visita técnica", "marcar uma visita"],
    }
    for k, vs in verbs.items():
        if k in t:
            ex.extend(vs)
    out: list[str] = []
    for e in ex:
        e = _strip_braces(e.strip())
        if e and e not in out:
            out.append(e)
    return out[:5]


# ---------- montagem ----------
def _validate_pattern(p: str | None, todos: list[str], fname: str) -> str | None:
    if not p:
        return None
    try:
        re.compile(p)
    except re.error:
        todos.append(f"campo '{fname}': pattern HTML incompatível com regex Python — descartado")
        return None
    if re.search(r"\{[a-z_]", p):
        todos.append(f"campo '{fname}': pattern com chaves nomeadas — descartado")
        return None
    return p


GENERIC_NAME = re.compile(r"(text|txt|f|campo|field|input|fld|untitled|caixa)_?\d+")


def _slot_base(f: RawField) -> str:
    """Nome do slot: o nome original em snake_case; se for genérico (`Text1`, `f3`), usa o rótulo."""
    if f.label_source == "pdf_text":
        return snake(f.label)
    base = snake(f.name)
    if GENERIC_NAME.fullmatch(base) and f.label_source != "name" and f.label:
        lab = re.sub(r"\(.*?\)", "", f.label)
        base = "_".join(snake(lab).split("_")[:4]) or base
    return base


def short_label(label: str) -> str:
    return _strip_braces((label or "").split(" (")[0].rstrip("?:* ").strip())


def build_slots(rf: RawForm) -> tuple[list[dict], dict[str, str], list[str], dict[str, str]]:
    """slots, mapa nome_original→slot, todos, rótulo curto por slot."""
    todos: list[str] = list(rf.todos)
    used: set[str] = set()
    name_map: dict[str, str] = {}
    slots: list[dict] = []
    for f in rf.fields:
        base = _slot_base(f)
        if base in RESERVED:
            base = f"{base}_campo"
        sname, i = base, 2
        while sname in used:
            sname, i = f"{base}_{i}", i + 1
        used.add(sname)
        name_map[f.name] = sname
    labels: dict[str, str] = {}
    for f in rf.fields:
        todos.extend(f.todos)
        labels[name_map[f.name]] = short_label(f.label) or name_map[f.name]
        t = infer_type(f)
        s: dict[str, Any] = {"name": name_map[f.name], "type": t, "prompt": _strip_braces(make_prompt(f.label, t, f.group)),
                             "required": bool(f.required)}
        if t == "enum":
            vals = [v for v, _ in f.options if v]
            vals = list(dict.fromkeys(vals))
            s["values"] = vals
            s["labels"] = {v: _strip_braces(lab or v) for v, lab in f.options if v in vals}
        pat = _validate_pattern(f.pattern, todos, f.name)
        if pat and t not in ("enum", "yes_no"):
            s["validate"] = pat
        if f.maxlength and t in ("text", "number"):
            s["max_len"] = f.maxlength
        if f.when:
            ref, op, val = f.when
            target = name_map.get(ref)
            if target and re.fullmatch(r"\S+", val):
                s["when"] = f"{{{target}}} {op} {val}"
            else:
                todos.append(f"campo '{f.name}': condição '{ref} {op} {val}' não mapeada — revisar `when`")
        if t == "email":
            s["prefill_from"] = "channel.user_email"
        elif t == "phone_br":
            s["prefill_from"] = "channel.user_phone"
        if t == "cpf":
            todos.append(f"campo '{f.name}': CPF — avaliar `sensitive: true` com `auth: otp`")
        s["confidence"] = confidence(f, rf.kind)
        slots.append(s)
    if not slots:
        todos.append("nenhum campo extraído — descrever os slots à mão")
    return slots, name_map, todos, labels


def service_id(rf: RawForm, fallback: str) -> str:
    raw = rf.title or fallback
    raw = re.sub(r"^(formul[aá]rio|form|ficha)\s+(de\s+|da\s+|do\s+)?", "", raw, flags=re.I)
    sid = snake(raw, "servico")
    sid = "_".join(sid.split("_")[:6])
    if len(sid) < 2:
        sid = snake(fallback, "servico")
    return sid[:50].rstrip("_") or "servico_convertido"


def confirm_template(slots: list[dict], what: str, labels: dict[str, str]) -> str:
    parts = [f"{labels.get(s['name'], s['name'])}: {{{s['name']}}}" for s in slots]
    body = "; ".join(parts) if parts else "(sem campos)"
    return f"Vou enviar {what} com: {body}. Confirma?"


def build_service(rf: RawForm, fallback_name: str, cfg=None) -> dict:
    slots, name_map, todos, labels = build_slots(rf)
    sid = service_id(rf, fallback_name)
    title = rf.title or fallback_name
    desc = _strip_braces(title if len(title) > 3 else f"Formulário {fallback_name}")
    svc: dict[str, Any] = {
        "service": sid,
        "version": 1,
        "review": "pending",
        "intent": {"description": desc, "examples": intent_examples(title, slots)},
        "auth": "none",
        "legal_basis": "legitimo_interesse",
        "slots": slots,
        "confirm": {"template": confirm_template(slots, desc.lower(), labels)},
    }
    if rf.kind == "html":
        fields: dict[str, Any] = {orig: f"{{{sname}}}" for orig, sname in name_map.items()}
        for k, v in rf.hidden.items():
            fields.setdefault(k, _strip_braces(v))
        svc["action"] = {
            "tool": "http.post_form",
            "args": {"url": rf.action or "", "method": rf.method if rf.method in ("get", "post") else "post", "fields": fields},
            "idempotency_key": f"{{session_id}}:{sid}",
        }
        svc["on_success"] = {"reply": "Pronto! Seus dados foram enviados. Protocolo {result.id}."}
    else:
        to = _team_email(cfg)
        if not to:
            to = "equipe@exemplo.com.br"
            todos.append("destinatário do e-mail é um placeholder (equipe@exemplo.com.br) — definir o e-mail da equipe")
        body = "\n".join(f"{labels.get(s['name'], s['name'])}: {{{s['name']}}}" for s in slots)
        svc["action"] = {
            "tool": "email.enviar",
            "args": {"to": to, "subject": f"[{sid}] Novo envio pelo chat", "body": body},
            "idempotency_key": f"{{session_id}}:{sid}",
        }
        svc["on_success"] = {"reply": "Pronto! Enviamos seus dados para a equipe."}
    svc["source"] = {
        "kind": rf.kind,
        "origin": rf.origin,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "todos": [_strip_braces(t) for t in todos],
    }
    if rf.kind == "html":
        svc["source"]["form_action"] = rf.action or ""
    svc["source"]["field_map"] = dict(name_map)  # nome original → slot
    return svc


def _team_email(cfg) -> str | None:
    try:
        for t in cfg.handoff.targets:
            if t.startswith("email:"):
                return t.split(":", 1)[1]
    except AttributeError:
        return None
    return None


# ---------- refino por LLM (opcional) ----------
LLM_SYS = (
    "TAREFA: REFINAR_SERVICO. Você recebe os campos de um formulário legado brasileiro. "
    "Responda JSON: {\"slots\": {\"<nome>\": {\"prompt\": \"pergunta amigável em PT-BR\", \"type\": \"<tipo>\"}}, "
    "\"examples\": [\"3 a 5 frases de usuário pedindo esse serviço\"]}. "
    f"Tipos válidos: {', '.join(sorted(SLOT_TYPES))}. Não invente campos."
)


def refine_with_llm(svc: dict, cfg) -> list[str]:
    """Aplica sugestões do LLM sem remover slots. Retorna notas para o relatório."""
    from ..llm import LLM, LLMError

    notes: list[str] = []
    payload = [{"name": s["name"], "type": s["type"], "prompt": s["prompt"]} for s in svc["slots"]]
    try:
        out = LLM(cfg.llm).chat_json([
            {"role": "system", "content": LLM_SYS},
            {"role": "user", "content": f"Formulário: {svc['intent']['description']}\nCampos: {payload}"},
        ])
    except (LLMError, ValueError, KeyError) as e:
        return [f"refino por LLM falhou ({e}) — mantida a extração determinística"]
    if not isinstance(out, dict):
        return notes
    sug = out.get("slots") or {}
    for s in svc["slots"]:
        g = sug.get(s["name"]) if isinstance(sug, dict) else None
        if not isinstance(g, dict):
            continue
        p = g.get("prompt")
        if isinstance(p, str) and 3 < len(p) < 200:
            s["prompt"] = _strip_braces(p.strip())
        t = g.get("type")
        if isinstance(t, str) and t in SLOT_TYPES and t != s["type"] and s["type"] not in ("enum", "yes_no") and t not in ("enum",):
            notes.append(f"LLM mudou tipo de '{s['name']}': {s['type']} → {t}")
            s["type"] = t
            s["confidence"] = round(min(s.get("confidence") or CONF_MID, CONF_MID), 2)
    ex = out.get("examples")
    if isinstance(ex, list):
        good = [_strip_braces(str(e).strip()) for e in ex if isinstance(e, str) and e.strip()]
        if good:
            svc["intent"]["examples"] = list(dict.fromkeys(good + svc["intent"]["examples"]))[:5]
    return notes


# ---------- YAML ----------
def to_yaml(svc: dict) -> str:
    todos = svc.get("source", {}).get("todos") or []
    head = [f"# Convertido automaticamente de {svc['source'].get('origin', '?')} — review: pending (revisar antes de aprovar)"]
    head += [f"# TODO: {t}" for t in todos]
    body = yaml.safe_dump(svc, sort_keys=False, allow_unicode=True, width=110, default_flow_style=False)
    return "\n".join(head) + "\n" + body


def write_service(svc: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{svc['service']}.yaml"
    path.write_text(to_yaml(svc), encoding="utf-8")
    return path
