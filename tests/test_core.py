"""Testes do núcleo: tipos, templates, schema, índice, RAG, agenda, privacidade e cenários."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from transformsite import cli
from transformsite.config import load
from transformsite.evals import run_scenarios
from transformsite.kb.extract import chunk_doc, html_to_doc
from transformsite.kb.index import Index
from transformsite.llm import LLM, parse_json
from transformsite.messages import Capabilities, OutMsg, render_text
from transformsite.rag import RAG
from transformsite.services import types as T
from transformsite.services.schema import Service, lint_service, load_services, render
from transformsite.store import Store, mask_pii
from transformsite.tools import default_registry
from transformsite.tools.agenda import free_slots, is_free, parse_hours


@pytest.fixture(autouse=True)
def clock():
    T.set_clock(lambda: datetime(2026, 10, 5, 10, 0))  # segunda
    yield
    T.set_clock(datetime.now)


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    cli.main(["init", str(d), "--org", "Teste", "--contact", "equipe@teste.com"])
    cfg = load(d)
    cfg.llm.provider = "fake"
    return cfg


# ---------- tipos ----------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("meu cpf é 529.982.247-25", "529.982.247-25"),
        ("52998224725", "529.982.247-25"),
        ("111.111.111-11", None),
        ("529.982.247-24", None),
    ],
)
def test_cpf(text, expected):
    assert T.ex_cpf(text, None) == expected


def test_email_phone_cep():
    assert T.ex_email("é Joao.Silva@Ex.com.br ok", None) == "joao.silva@ex.com.br"
    assert T.ex_phone("me liga 51 99999-0000", None) == "(51) 99999-0000"
    assert T.ex_phone("(11)3333-4444", None) == "(11) 3333-4444"
    assert T.ex_cep("cep 90010-120", None) == "90010-120"
    assert T.ex_email("sem email", None) is None


@pytest.mark.parametrize(
    "text,iso",
    [
        ("quinta às 10", "2026-10-08T10:00"),
        ("amanhã 14h", "2026-10-06T14:00"),
        ("15/10 às 9h30", "2026-10-15T09:30"),
        ("dia 20 de outubro às 16:00", "2026-10-20T16:00"),
        ("segunda que vem 11h", "2026-10-12T11:00"),
        ("sexta às 3 da tarde", "2026-10-09T15:00"),
        ("2026-10-07T09:00", "2026-10-07T09:00"),
        ("quero ter um horário", None),
        ("qualquer dia", None),
    ],
)
def test_datetime_ptbr(text, iso):
    assert T.ex_datetime(text, None) == iso


def test_date_rollover():
    assert T.parse_date("01/02").isoformat() == "2027-02-01"
    assert T.parse_date("hoje").isoformat() == "2026-10-05"


def test_yes_no_enum():
    assert T.ex_yes_no("Sim, pode", None) is True
    assert T.ex_yes_no("não, o email está errado", None) is False
    assert T.ex_yes_no("talvez", None) is None

    class S:
        values = ["duvida", "reclamacao", "sugestao"]
        synonyms = {"reclamacao": ["reclamar"]}

    assert T.ex_enum("2", S) == "reclamacao"
    assert T.ex_enum("quero reclamar", S) == "reclamacao"
    assert T.ex_enum("Sugestão", S) == "sugestao"
    assert T.ex_enum("nada a ver", S) is None


def test_fmt():
    assert T.fmt_datetime("2026-10-08T10:00") == "quinta, 08/10 às 10:00"


# ---------- templates / schema ----------
def test_render_filters():
    ctx = {"nome": "Ana", "cpf": "529.982.247-25", "dt": "2026-10-08T10:00", "result": {"id": "X1"}}
    assert render("Oi {nome}, {cpf|last4}, {dt|fmt_br}, {result.id}", ctx) == "Oi Ana, 7-25, quinta, 08/10 às 10:00, X1"
    assert render({"a": ["{nome}"]}, ctx) == {"a": ["Ana"]}


def _svc(**kw):
    base = {"service": "teste", "intent": {"description": "d"}, "slots": [{"name": "x", "type": "text", "prompt": "x?"}]}
    base.update(kw)
    return Service(**base)


def test_lint_rules():
    reg = default_registry()
    # ação com efeito sem confirm/idempotência
    errs = lint_service(_svc(action={"tool": "crm.criar_ticket", "args": {"subject": "{x}", "body": "{x}"}}), reg)
    assert any("confirm" in e for e in errs) and any("idempotency" in e for e in errs)
    # ferramenta desconhecida e template quebrado
    errs = lint_service(_svc(action={"tool": "nao.existe"}, confirm={"template": "{y}"}), reg)
    assert any("desconhecida" in e for e in errs) and any("{y}" in e for e in errs)
    # sensível sem auth
    errs = lint_service(_svc(slots=[{"name": "cpf", "type": "cpf", "prompt": "cpf?", "sensitive": True}]), reg)
    assert any("sensível" in e for e in errs)
    with pytest.raises(Exception):
        _svc(slots=[{"name": "x", "type": "inexistente", "prompt": "?"}])


def test_pending_service_not_loaded(project):
    p = project.services_dir / "fale_conosco.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("review: approved", "review: pending"), encoding="utf-8")
    svcs, errs = load_services(project.services_dir, only_approved=True, tools=default_registry(project))
    assert "fale_conosco" not in svcs and "agendar" in svcs and not errs


# ---------- índice / RAG ----------
HTML = """<html><head><title>Cursos | Escola</title></head><body><nav>menu inicio</nav><main>
<h1>Cursos</h1><p>Todos os cursos da Escola são gratuitos e abertos para a comunidade.</p>
<h2>Certificado</h2><p>O certificado é emitido automaticamente ao concluir 100% das aulas do curso.</p>
<h2>Horário</h2><p>As lives acontecem toda quarta-feira às 20h no canal do YouTube.</p>
</main><footer>rodapé</footer></body></html>"""


def test_extract_and_chunk():
    doc = html_to_doc(HTML, "https://escola.test/cursos")
    assert doc.title == "Cursos | Escola"
    assert "menu inicio" not in doc.text and "rodapé" not in doc.text
    assert [h for h, _ in doc.sections] == ["Cursos", "Certificado", "Horário"]
    chunks = chunk_doc(doc, 50, 10)
    assert chunks and all(c["url"] == "https://escola.test/cursos" for c in chunks)


def test_index_hybrid_and_rag(project):
    idx = Index(project.kb_path)
    llm = LLM(project.llm)
    doc = html_to_doc(HTML, "https://escola.test/cursos")
    chunks = [{"url": doc.url, "title": doc.title, "heading": h, "text": t} for h, t in doc.sections]
    idx.upsert_page(doc.url, doc.title, doc.text, chunks, llm.embed([c["text"] for c in chunks]))
    assert idx.stats()["chunks"] == 3
    hits = idx.search("como recebo o certificado", llm.embed(["como recebo o certificado"])[0], k=2)
    assert hits[0].heading == "Certificado"
    rag = RAG(project, llm, idx)
    a = rag.answer("como recebo o certificado?")
    assert not a.nao_sei and a.citations[0]["url"] == "https://escola.test/cursos"
    assert rag.answer("como recebo o certificado?").cached
    # reingestão idempotente: página sai do índice
    idx.delete_page(doc.url)
    assert idx.stats()["chunks"] == 0
    assert rag.answer("qual a capital da França").nao_sei


def test_rag_requires_valid_citation(project):
    idx = Index(project.kb_path)
    idx.upsert_page("https://x.test", "X", "texto", [{"url": "https://x.test", "title": "X", "heading": "", "text": "a resposta é 42"}], None)
    llm = LLM(project.llm, fake_fn=lambda m, j: '{"nao_sei": false, "resposta": "inventei", "fontes": [7]}')
    assert RAG(project, llm, idx).answer("qual a resposta?").nao_sei  # fonte inexistente → não responde


def test_parse_json_tolerant():
    assert parse_json('<think>hmm</think>```json\n{"a": "{b}"}\n```') == {"a": "{b}"}
    assert parse_json('Claro! {"x": 1} fim') == {"x": 1}


# ---------- agenda ----------
def test_agenda(project):
    st = Store(project.db_path)
    assert parse_hours("mon-fri 09:00-17:00; sat 09:00-12:00")[5] == (540, 720)
    slots = free_slots(project, st, 3)
    assert slots == ["2026-10-05T12:00", "2026-10-05T12:30", "2026-10-05T13:00"]
    assert is_free(project, st, datetime(2026, 10, 11, 10, 0))[0] is False  # domingo
    assert "antecedência" in is_free(project, st, datetime(2026, 10, 5, 11, 0))[1]
    assert "minutos" in is_free(project, st, datetime(2026, 10, 6, 10, 10))[1]


# ---------- privacidade / store ----------
def test_mask_and_forget(project):
    assert mask_pii("cpf 529.982.247-25 email ana@x.com tel 51 99999-0000") == "cpf ***.***.247-25 email ***@x.com tel (**) *****-0000"
    st = Store(project.db_path)
    s = st.session("telegram", "42")
    st.add_message(s["id"], "in", "meu email é ana@x.com")
    assert "***@x.com" in st.messages(s["id"])[0]["text"]
    assert st.forget("telegram", "42") == 1 and st.messages(s["id"]) == []
    assert st.add_message(s["id"], "in", "a", dedup="m1") and not st.add_message(s["id"], "in", "a", dedup="m1")


def test_render_degradation():
    out = OutMsg("Escolha:", choices=[{"id": str(i), "label": f"op{i}"} for i in range(5)], citations=[{"n": 1, "url": "https://a"}])
    t, ch, mode = render_text(out, Capabilities(buttons=3, lists=10))
    assert mode == "list" and "Fontes:\n[1] https://a" in t
    t, ch, mode = render_text(out, Capabilities())
    assert mode == "numbered" and "5. op4" in t and not ch


# ---------- cenários (F2) ----------
def test_scenarios_all_pass(project):
    res = run_scenarios(project, project.root / "tests" / "cenarios")
    failed = [f"{r['name']}: {r['error']}" for r in res["results"] if not r["ok"]]
    assert not failed, failed
    assert res["total"] >= 11


def test_cli_validate_ok(project, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["-C", str(project.root), "validate"])
    assert e.value.code == 0
    assert "0 erro(s)" in capsys.readouterr().out
