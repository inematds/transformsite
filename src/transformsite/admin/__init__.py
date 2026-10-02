"""Painel admin (FastAPI + Jinja2, server-rendered). Escopo travado em 5 telas:
Conversas · Fila · Serviços · Base de conhecimento · Métricas.

Auth: token em `os.environ[cfg.admin_token_env]`, via login (cookie de sessão do painel) ou
`Authorization: Bearer <token>`. Sem token configurado o painel só aceita 127.0.0.1 e avisa.
CSRF: todo POST com cookie exige o campo `csrf` da sessão do painel (Bearer dispensa: não é cookie).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader

from ..config import ProjectConfig
from ..store import mask_pii

COOKIE = "ts_admin"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}  # "testclient" = TestClient do Starlette
SESSION_TTL = 12 * 3600
PLACEHOLDER_TOKENS = {"troque-este-token", "changeme", "admin"}  # valor do .env.example = sem token

_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=True)
_env.filters["dt"] = lambda ts: datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "—"
_env.filters["pct"] = lambda v: "—" if v is None else f"{v * 100:.0f}%"
_env.filters["mask"] = lambda v: mask_pii(str(v))
_env.filters["url"] = lambda u: u if re.match(r"https?://", str(u or "")) else "#"  # bloqueia javascript: etc.


def approve_text(text: str) -> str:
    """Troca `review: pending` → `approved` preservando o resto do arquivo (comentários, ordem, aspas)."""
    new, n = re.subn(r"(?m)^(review:\s*)(['\"]?)pending\2", r"\1\2approved\2", text, count=1)
    if n:
        return new
    if re.search(r"(?m)^review:", text):
        return text  # já tem outro valor (approved)
    new, n = re.subn(r"(?m)^(service:.*\n)", r"\1review: approved\n", text, count=1)
    return new if n else "review: approved\n" + text


def admin_token(cfg: ProjectConfig) -> str:
    """Token do painel; o valor de exemplo do scaffold conta como ausente (painel fica só local)."""
    tok = os.environ.get(cfg.admin_token_env, "").strip()
    return "" if tok in PLACEHOLDER_TOKENS else tok


class Panel:
    def __init__(self, cfg: ProjectConfig, agent):
        self.cfg = cfg
        self.agent = agent
        self.sessions: dict[str, dict] = {}  # cookie → {csrf, exp}
        self.ingest: dict = {"running": False, "log": [], "result": None, "error": None, "started": None, "ended": None}

    # ---------- auth ----------
    def token(self) -> str:
        return admin_token(self.cfg)

    def new_session(self) -> tuple[str, dict]:
        now = time.time()
        for k in [k for k, v in self.sessions.items() if v["exp"] < now][:500]:
            self.sessions.pop(k, None)
        while len(self.sessions) >= 1000:  # teto: descarta a mais antiga
            self.sessions.pop(next(iter(self.sessions)))
        c = secrets.token_urlsafe(24)
        s = {"csrf": secrets.token_urlsafe(24), "exp": time.time() + SESSION_TTL}
        self.sessions[c] = s
        return c, s

    def auth(self, request: Request) -> dict:
        tok = self.token()
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            if tok and hmac.compare_digest(h[7:].strip(), tok):
                return {"bearer": True, "csrf": ""}
            raise HTTPException(401, "token inválido")
        host = request.client.host if request.client else ""
        if not tok and host not in LOCAL_HOSTS:
            raise HTTPException(403, f"{self.cfg.admin_token_env} não definido (ou valor de exemplo): painel só aceita acesso local")
        c = request.cookies.get(COOKIE, "")
        s = self.sessions.get(c)
        if s and s["exp"] > time.time():
            return {"bearer": False, "csrf": s["csrf"]}
        if not tok:  # modo local sem token: abre sessão automaticamente
            c, s = self.new_session()
            return {"bearer": False, "csrf": s["csrf"], "new_cookie": c}
        raise HTTPException(303, headers={"Location": "/admin/login"})

    # ---------- render ----------
    def render(self, request: Request, a: dict, tpl: str, code: int = 200, **ctx) -> HTMLResponse:
        html = _env.get_template(tpl).render(
            csrf=a.get("csrf", ""),
            no_token=not self.token(),
            token_env=self.cfg.admin_token_env,
            org=self.cfg.org,
            name=self.cfg.name,
            msg=request.query_params.get("msg", ""),
            **ctx,
        )
        r = HTMLResponse(html, status_code=code)
        if a.get("new_cookie"):
            set_cookie(r, a["new_cookie"])
        return r

    # ---------- serviços ----------
    def reload_services(self) -> None:
        from ..services.schema import load_services

        with self.agent.agent_lock:
            svcs, errs = load_services(self.cfg.services_dir, only_approved=True, tools=self.agent.tools)
            self.agent.services = svcs
            self.agent.load_errors = errs

    def service_file(self, fname: str) -> Path:
        if not re.fullmatch(r"[\w.-]+\.ya?ml", fname):
            raise HTTPException(400, "nome de arquivo inválido")
        p = self.cfg.services_dir / fname
        if not p.is_file():
            raise HTTPException(404, "serviço não encontrado")
        return p

    # ---------- KB ----------
    def run_ingest(self) -> bool:
        if self.ingest["running"]:
            return False
        st = self.ingest
        st.update(running=True, log=[], result=None, error=None, started=time.time(), ended=None)

        def work():
            from ..kb.ingest import ingest
            from ..rag import RAG

            try:
                st["result"] = ingest(self.cfg, log=lambda m: st["log"].append(str(m)[:300]))
                with self.agent.agent_lock:  # índice novo → RAG novo (descarta cache de vetores)
                    self.agent.rag = RAG(self.cfg, self.agent.llm)
            except Exception as e:
                st["error"] = f"{type(e).__name__}: {e}"[:500]
            finally:
                st["running"] = False
                st["ended"] = time.time()

        threading.Thread(target=work, name="ingest", daemon=True).start()
        return True


def set_cookie(r: Response, value: str) -> None:
    r.set_cookie(COOKIE, value, httponly=True, samesite="strict", max_age=SESSION_TTL, path="/admin")


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _go(url: str, msg: str = "") -> RedirectResponse:
    return RedirectResponse(url + (("&" if "?" in url else "?") + "msg=" + quote(msg) if msg else ""), status_code=303)


def mount_admin(app: FastAPI, cfg: ProjectConfig, agent) -> Panel:
    panel = Panel(cfg, agent)
    app.state.panel = panel
    store = agent.store
    r = APIRouter(prefix="/admin")

    def auth(request: Request) -> dict:
        return panel.auth(request)

    def post(request: Request, f: dict = Depends(_form), a: dict = Depends(auth)) -> dict:
        if not a["bearer"] and not (f.get("csrf") and hmac.compare_digest(f["csrf"], a["csrf"])):
            raise HTTPException(403, "CSRF inválido")
        return f

    # ---------- login ----------
    @r.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return panel.render(request, {}, "login.html", error="")

    @r.post("/login")
    def login(request: Request, f: dict = Depends(_form)):
        tok = panel.token()
        if not tok or not hmac.compare_digest(f.get("token", ""), tok):
            return panel.render(request, {}, "login.html", code=401, error="Token inválido." if tok else "Painel sem token configurado.")
        c, _ = panel.new_session()
        resp = _go("/admin/conversas")
        set_cookie(resp, c)
        return resp

    @r.get("/logout")
    def logout(request: Request):
        panel.sessions.pop(request.cookies.get(COOKIE, ""), None)
        resp = _go("/admin/login")
        resp.delete_cookie(COOKIE, path="/admin")
        return resp

    @r.get("")
    @r.get("/")
    def home(a: dict = Depends(auth)):
        return _go("/admin/conversas")

    # ---------- (a) Conversas ----------
    @r.get("/conversas", response_class=HTMLResponse)
    def conversas(request: Request, status: str = "", a: dict = Depends(auth)):
        rows = store.list_sessions(limit=200, status=status if status in ("bot", "human") else None)
        for s in rows:
            last = store.messages(s["id"], 1)
            s["last"] = last[0]["text"][:120] if last else ""
        return panel.render(request, a, "conversas.html", tab="conversas", sessions=rows, status=status)

    @r.get("/conversas/{sid}", response_class=HTMLResponse)
    def conversa(request: Request, sid: str, a: dict = Depends(auth)):
        sess = store.get_session(sid)
        if not sess:
            raise HTTPException(404, "conversa não encontrada")
        msgs = store.messages(sid, 300)
        for m in msgs:
            m["data"] = json.loads(m.get("data") or "{}")
        st = sess["state"]
        tickets = [t for t in store.tickets(limit=1000) if t["session_id"] == sid]
        return panel.render(
            request, a, "conversa.html", tab="conversas", s=sess, msgs=msgs, tickets=tickets,
            service=st.get("service"), current=st.get("current"), phase=st.get("phase"), slots=st.get("slots") or {},
            has_hooks=bool(agent.outbound_hooks),
        )

    @r.post("/conversas/{sid}/assumir")
    def assumir(sid: str, f: dict = Depends(post)):
        _need(store, sid)
        agent.takeover(sid)
        return _go(f"/admin/conversas/{sid}", "Conversa assumida: o bot está em silêncio nesta sessão.")

    @r.post("/conversas/{sid}/devolver")
    def devolver(sid: str, f: dict = Depends(post)):
        _need(store, sid)
        agent.release(sid)
        return _go(f"/admin/conversas/{sid}", "Conversa devolvida ao bot (estado preservado).")

    @r.post("/conversas/{sid}/responder")
    def responder(sid: str, f: dict = Depends(post)):
        _need(store, sid)
        text = (f.get("text") or "").strip()
        if not text:
            return _go(f"/admin/conversas/{sid}", "Mensagem vazia.")
        if not agent.outbound_hooks:
            msg = "Nenhum canal registrou envio: a resposta ficou só no histórico."
        else:
            msg = "Resposta enviada."
        try:
            agent.human_reply(sid, text, agent_name=f.get("author") or "equipe")
        except Exception as e:
            msg = f"Falha ao enviar: {type(e).__name__}: {str(e)[:150]}"
        return _go(f"/admin/conversas/{sid}", msg)

    @r.post("/conversas/{sid}/errada")
    def errada(sid: str, f: dict = Depends(post)):
        _need(store, sid)
        try:
            mid = int(f.get("msg_id", ""))
        except ValueError:
            raise HTTPException(400, "msg_id inválido")
        m = next((x for x in store.messages(sid, 1000) if x["id"] == mid and x["direction"] == "out"), None)
        if m is None:
            raise HTTPException(404, "mensagem não encontrada")
        cits = json.loads(m.get("data") or "{}").get("citations") or []
        fontes = "\n".join(f"[{c.get('n')}] {c.get('url')}" for c in cits) or "(sem citação)"
        body = f"Resposta do bot:\n{m['text']}\n\nFontes usadas:\n{fontes}\n\nNota: {f.get('note', '')}".strip()
        tid = store.ticket(sid, "qualidade", "erro", f"Resposta errada (msg {mid})", body, {"message_id": mid, "citations": cits})
        store.event(sid, "reported_wrong", message=mid, ticket=tid)
        return _go(f"/admin/conversas/{sid}", f"Ticket {tid} criado na fila.")

    # ---------- (b) Fila ----------
    @r.get("/fila", response_class=HTMLResponse)
    def fila(request: Request, a: dict = Depends(auth)):
        return panel.render(request, a, "fila.html", tab="fila", tickets=store.tickets(status="open"))

    @r.post("/fila/{tid}/fechar")
    def fechar(tid: str, f: dict = Depends(post)):
        store.close_ticket(tid)
        return _go("/admin/fila", f"Ticket {tid} fechado.")

    # ---------- (c) Serviços ----------
    @r.get("/servicos", response_class=HTMLResponse)
    def servicos(request: Request, a: dict = Depends(auth)):
        from ..services.schema import ServiceError, lint_service, load_service

        items = []
        d = cfg.services_dir
        for p in sorted(d.glob("*.y*ml")) if d.exists() else []:
            it = {"file": p.name, "yaml": p.read_text(encoding="utf-8"), "errors": [], "service": "", "review": "", "desc": ""}
            try:
                svc = load_service(p)
                it.update(service=svc.service, review=svc.review, desc=svc.intent.description, version=svc.version)
                it["errors"] = lint_service(svc, agent.tools)
            except ServiceError as e:
                it["errors"] = [str(e)]
            it["loaded"] = it["service"] in agent.services
            items.append(it)
        return panel.render(request, a, "servicos.html", tab="servicos", items=items, load_errors=agent.load_errors)

    @r.post("/servicos/{fname}/aprovar")
    def aprovar(fname: str, f: dict = Depends(post)):
        from ..services.schema import ServiceError, lint_service, load_service

        p = panel.service_file(fname)
        try:
            errs = lint_service(load_service(p), agent.tools)
        except ServiceError as e:
            errs = [str(e)]
        if errs:
            return _go("/admin/servicos", f"{fname} não aprovado: corrija antes — {'; '.join(errs)[:300]}")
        p.write_text(approve_text(p.read_text(encoding="utf-8")), encoding="utf-8")
        panel.reload_services()
        return _go("/admin/servicos", f"{fname} aprovado e serviços recarregados.")

    @r.post("/servicos/recarregar")
    def recarregar(f: dict = Depends(post)):
        panel.reload_services()
        return _go("/admin/servicos", f"{len(agent.services)} serviço(s) carregado(s).")

    # ---------- (d) Base de conhecimento ----------
    @r.get("/kb", response_class=HTMLResponse)
    def kb(request: Request, a: dict = Depends(auth)):
        from ..kb.index import Index

        idx = Index(cfg.kb_path)
        pages = [dict(zip(("url", "title", "fetched_at", "words"), row)) for row in idx.db.execute(
            "SELECT url, title, fetched_at, words FROM pages ORDER BY fetched_at DESC LIMIT 30")]
        return panel.render(request, a, "kb.html", tab="kb", stats=idx.stats(), pages=pages, ing=panel.ingest,
                             sources=cfg.kb.urls + cfg.kb.sitemaps + cfg.kb.files)

    @r.post("/kb/reingerir")
    def reingerir(f: dict = Depends(post)):
        ok = panel.run_ingest()
        return _go("/admin/kb", "Re-ingestão iniciada." if ok else "Já há uma ingestão em andamento.")

    # ---------- (e) Métricas ----------
    def _dias(dias: int) -> int:
        return max(1, min(int(dias), 90))

    @r.get("/metricas", response_class=HTMLResponse)
    def metricas(request: Request, dias: int = 7, a: dict = Depends(auth)):
        from ..report import report

        res = report(cfg, since_days=_dias(dias), store=store)
        mx = max([d["conversas"] for d in res["por_dia"]] + [1])
        return panel.render(request, a, "metricas.html", tab="metricas", res=res, dias=_dias(dias), mx=mx)

    @r.get("/metricas.csv")
    def metricas_csv(dias: int = 7, a: dict = Depends(auth)):
        from ..report import report, to_csv

        res = report(cfg, since_days=_dias(dias), store=store)
        return PlainTextResponse(to_csv(res), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="metricas-{res["desde"]}-{_dias(dias)}d.csv"'})

    app.include_router(r)
    return panel


def _need(store, sid: str) -> None:
    if not store.get_session(sid):
        raise HTTPException(404, "conversa não encontrada")
