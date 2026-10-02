"""Fase 5: servidor, painel admin (5 telas), handoff operado por humano e métricas. Sem rede, LLM fake."""

from __future__ import annotations

import csv
import re
import time

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from transformsite.cli import main
from transformsite.config import load
from transformsite.engine import Agent
from transformsite.messages import InMsg
from transformsite.server import create_app
from transformsite.services.schema import load_service
from transformsite.store import Store

TOKEN = "segredo-de-teste"


class FakeChannel:
    """Canal fake no contrato esperado pelo servidor: attach / router (sem run_poller)."""

    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def attach(self, agent):
        agent.outbound_hooks.append(self._hook)

    def _hook(self, sess, out):
        if sess and sess["channel"] == self.name:
            self.sent.append((sess["user_id"], out.text))

    def router(self, agent):
        r = APIRouter()

        @r.post("/fake/webhook")
        def webhook(p: dict):
            outs = agent.handle(InMsg(self.name, p["user"], p["text"], msg_id=p.get("id")))
            return {"replies": [o.text for o in outs]}

        return r


@pytest.fixture
def proj(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    main(["init", str(d), "--org", "T"])
    monkeypatch.delenv("TRANSFORMSITE_LLM", raising=False)
    cfg = load(d)
    cfg.llm.provider = "fake"
    return cfg


@pytest.fixture
def env(proj, monkeypatch):
    monkeypatch.setenv(proj.admin_token_env, TOKEN)
    agent = Agent(proj)
    fake = FakeChannel()
    app = create_app(proj, agent=agent, channels=[fake])
    with TestClient(app) as c:
        yield c, agent, fake


def login(c) -> str:
    r = c.post("/admin/login", data={"token": TOKEN})
    assert r.status_code == 200 and "Conversas" in r.text
    return csrf_of(c.get("/admin/conversas").text)


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf(?:-token)?" (?:value|content)="([^"]+)"', html)
    assert m, "página sem token CSRF"
    return m.group(1)


def say(c, user, text):
    r = c.post("/fake/webhook", json={"user": user, "text": text})
    assert r.status_code == 200
    return r.json()["replies"]


# ---------------- health / auth ----------------
def test_health(env):
    c, agent, _ = env
    r = c.get("/health")
    assert r.status_code == 200
    h = r.json()
    assert h["ok"] is True and h["version"]
    assert set(h["services"]) == {"agendar", "fale_conosco"}
    assert h["channels"] == ["fake"]
    assert {"pages", "chunks"} <= set(h["kb"])


def test_login(env):
    c, _, _ = env
    r = c.get("/admin/conversas", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert c.get("/admin/conversas", headers={"Authorization": "Bearer errado"}).status_code == 401
    assert c.post("/admin/login", data={"token": "errado"}).status_code == 401
    assert c.get("/admin/conversas", follow_redirects=False).status_code == 303  # login falho não abre sessão
    assert c.get("/admin/conversas", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    login(c)
    assert c.get("/admin/conversas").status_code == 200


def test_cinco_telas(env):
    c, agent, _ = env
    say(c, "u1", "oi")
    login(c)
    sid = agent.store.session("fake", "u1")["id"]
    for path in ["/admin/conversas", "/admin/fila", "/admin/servicos", "/admin/kb", "/admin/metricas",
                 f"/admin/conversas/{sid}", "/admin/conversas?status=human", "/admin/metricas?dias=30"]:
        r = c.get(path)
        assert r.status_code == 200, path
    r = c.get("/admin/metricas.csv?dias=3")
    assert r.status_code == 200 and r.text.startswith("dia,conversas")
    assert c.get("/admin/conversas/naoexiste").status_code == 404


def test_csrf_bloqueia_post(env):
    c, agent, _ = env
    say(c, "u1", "oi")
    sid = agent.store.session("fake", "u1")["id"]
    tok = login(c)
    assert c.post(f"/admin/conversas/{sid}/assumir", data={}).status_code == 403
    assert c.post(f"/admin/conversas/{sid}/assumir", data={"csrf": "forjado"}).status_code == 403
    assert agent.store.get_session(sid)["status"] == "bot"
    assert c.post(f"/admin/conversas/{sid}/assumir", data={"csrf": tok}).status_code == 200
    assert agent.store.get_session(sid)["status"] == "human"


def test_sem_token_so_local(proj, monkeypatch):
    monkeypatch.delenv(proj.admin_token_env, raising=False)
    app = create_app(proj, agent=Agent(proj), channels=[])
    with TestClient(app) as c:  # TestClient conta como local
        r = c.get("/admin/conversas")
        assert r.status_code == 200 and "não definido" in r.text
    with TestClient(app, client=("203.0.113.9", 5000)) as c:
        assert c.get("/admin/conversas").status_code == 403
    monkeypatch.setenv(proj.admin_token_env, "troque-este-token")  # valor do .env.example não vale como token
    with TestClient(app, client=("203.0.113.9", 5000)) as c:
        assert c.get("/admin/conversas").status_code == 403
        assert c.post("/admin/login", data={"token": "troque-este-token"}).status_code == 401


# ---------------- handoff operado por humano ----------------
def test_takeover_silencia_e_release_retoma(env):
    c, agent, fake = env
    say(c, "u1", "quero agendar")
    assert any("e-mail" in t for t in say(c, "u1", "João Silva"))
    sid = agent.store.session("fake", "u1")["id"]
    tok = login(c)

    c.post(f"/admin/conversas/{sid}/assumir", data={"csrf": tok})
    assert agent.store.get_session(sid)["status"] == "human"
    n = len(agent.notifier.log)
    assert say(c, "u1", "tem alguém aí?") == []  # bot em silêncio
    assert any("tem alguém aí?" in txt for _, txt in agent.notifier.log[n:])  # virou notificação

    # resposta humana chega ao canal
    r = c.post(f"/admin/conversas/{sid}/responder", data={"csrf": tok, "text": "Oi João, aqui é a Ana."})
    assert r.status_code == 200
    assert fake.sent == [("u1", "Oi João, aqui é a Ana.")]
    assert "Oi João, aqui é a Ana." in c.get(f"/admin/conversas/{sid}").text

    c.post(f"/admin/conversas/{sid}/devolver", data={"csrf": tok})
    assert agent.store.get_session(sid)["status"] == "bot"
    replies = say(c, "u1", "joao@exemplo.com")  # continua do mesmo slot (email → motivo)
    assert any("assunto" in t for t in replies)
    st = agent.store.get_session(sid)["state"]
    assert st["service"] == "agendar" and st["slots"]["nome"] == "João Silva" and st["current"] == "motivo"


def test_resposta_errada_vira_ticket_e_fecha(env):
    c, agent, _ = env
    say(c, "u1", "oi")
    sid = agent.store.session("fake", "u1")["id"]
    bot = [m for m in agent.store.messages(sid) if m["author"] == "bot"][0]
    tok = login(c)
    c.post(f"/admin/conversas/{sid}/errada", data={"csrf": tok, "msg_id": str(bot["id"])})
    t = [t for t in agent.store.tickets(status="open") if t["kind"] == "erro"]
    assert len(t) == 1 and bot["text"][:30] in t[0]["body"]
    assert t[0]["id"] in c.get("/admin/fila").text
    c.post(f"/admin/fila/{t[0]['id']}/fechar", data={"csrf": tok})
    assert not [x for x in agent.store.tickets(status="open") if x["kind"] == "erro"]


# ---------------- serviços ----------------
def test_aprovar_servico_pending(env, proj):
    c, agent, _ = env
    src = (proj.services_dir / "fale_conosco.yaml").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^service:.*$", "service: ouvidoria", src)
    text = re.sub(r"(?m)^review:.*$", "review: pending   # gerado pelo conversor", text)
    text = "# comentário do topo\n" + text
    p = proj.services_dir / "ouvidoria.yaml"
    p.write_text(text, encoding="utf-8")
    tok = login(c)
    c.post("/admin/servicos/recarregar", data={"csrf": tok})
    assert "ouvidoria" not in agent.services
    assert "pending" in c.get("/admin/servicos").text

    r = c.post("/admin/servicos/ouvidoria.yaml/aprovar", data={"csrf": tok})
    assert r.status_code == 200
    new = p.read_text(encoding="utf-8")
    assert new == text.replace("review: pending", "review: approved")  # resto preservado
    assert load_service(p).review == "approved"
    assert "ouvidoria" in agent.services
    assert c.post("/admin/servicos/..%2Fx.yaml/aprovar", data={"csrf": tok}).status_code in (400, 404)


# ---------------- métricas ----------------
def test_report_sintetico(proj, tmp_path):
    from transformsite.report import report

    st = Store(proj.db_path)
    plan = {
        "s1": [("turn", {"ms": 100}), ("answer", {"cited": 1})],
        "s2": [("turn", {"ms": 200}), ("answer", {"cited": 0}), ("handoff", {"reason": "x"})],
        "s3": [("turn", {"ms": 300}), ("service_started", {"service": "agendar"}), ("service_completed", {"service": "agendar", "seconds": 60})],
        "s4": [("turn", {"ms": 400}), ("service_started", {"service": "agendar"}), ("slot_retry", {"service": "agendar", "slot": "email", "attempt": 1}), ("nao_sei", {})],
        "s5": [("turn", {"ms": 1000}), ("service_started", {"service": "fale_conosco"}), ("service_completed", {"service": "fale_conosco", "seconds": 120})],
        "s6": [("turn", {"ms": 50}), ("answer", {"cited": 2})],  # fora da janela
    }
    for sid, evs in plan.items():
        for kind, data in evs:
            st.event(sid, kind, **data)
    now = time.time()
    st.db.execute("UPDATE events SET ts=? WHERE session_id='s5'", (now - 86400,))
    st.db.execute("UPDATE events SET ts=? WHERE session_id='s6'", (now - 30 * 86400,))
    st.db.commit()
    st.ticket("s1", "qualidade", "erro", "Resposta errada", "x")

    out = tmp_path / "m.csv"
    res = report(proj, since_days=7, out=out)
    r = res["resumo"]
    assert r["conversas"] == 5
    assert r["resolvidas_sem_humano"] == 3 and r["taxa_resolucao"] == 0.6
    assert r["handoffs"] == 1 and r["taxa_handoff"] == 0.2
    assert r["respostas"] == 2 and r["respostas_com_citacao"] == 1 and r["taxa_citacao"] == 0.5
    assert r["nao_sei"] == 1
    assert (r["servicos_iniciados"], r["servicos_concluidos"], r["servicos_cancelados"]) == (3, 2, 0)
    assert r["mediana_conclusao_s"] == 90.0
    assert r["latencia_p50_ms"] == 300 and r["latencia_p95_ms"] == 1000
    assert r["reportadas_erradas"] == 1
    assert res["por_servico"]["agendar"] == {"started": 2, "completed": 1}
    assert res["abandono_por_slot"] == {"agendar:email": 1}
    dias = {d["dia"]: d for d in res["por_dia"]}
    assert len(dias) == 7
    assert dias[time.strftime("%Y-%m-%d")]["conversas"] == 4
    assert dias[time.strftime("%Y-%m-%d", time.localtime(now - 86400))]["servicos_concluidos"] == 1

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 8 and rows[-1]["dia"] == "total"
    assert rows[-1]["conversas"] == "5" and rows[-1]["taxa_resolucao"] == "0.6" and rows[-1]["latencia_p95_ms"] == "1000.0"

    # CLI usa o mesmo cálculo
    out2 = tmp_path / "cli.csv"
    main(["-C", str(proj.root), "--llm", "fake", "report", "--since", "7", "--out", str(out2)])
    assert out2.read_text(encoding="utf-8") == out.read_text(encoding="utf-8")


def test_reingerir_kb_em_thread(env):
    c, agent, _ = env
    tok = login(c)
    assert c.post("/admin/kb/reingerir", data={"csrf": tok}).status_code == 200
    ing = c.app.state.panel.ingest
    for _ in range(100):
        if not ing["running"]:
            break
        time.sleep(0.05)
    assert not ing["running"] and ing["error"] is None and ing["result"]["pages_seen"] == 0
    assert "concluída" in c.get("/admin/kb").text
