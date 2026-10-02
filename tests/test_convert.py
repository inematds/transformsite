"""Fase 4 — conversor de formulários legados → service.yaml (sem rede externa, LLM fake)."""

from __future__ import annotations

import functools
import http.server
import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from transformsite.config import LLMConfig
from transformsite.convert import ConvertError, convert_file
from transformsite.convert.build import build_service, refine_with_llm, snake
from transformsite.convert.html_form import extract_html
from transformsite.services.schema import load_service, load_services, lint_service, render
from transformsite.tools import ToolContext, default_registry

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "forms"
FORMS = sorted(p for p in FIX.iterdir() if p.suffix in (".html", ".pdf"))
HTML_FORMS = [p for p in FORMS if p.suffix == ".html"]
PDF_FORMS = [p for p in FORMS if p.suffix == ".pdf"]


def _metric_module():
    spec = importlib.util.spec_from_file_location("convert_metric", ROOT / "scripts" / "convert_metric.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _cfg(provider="fake", email="equipe@empresa.com.br"):
    return SimpleNamespace(llm=LLMConfig(provider=provider), handoff=SimpleNamespace(targets=["panel", f"email:{email}"]))


def test_fixtures_suficientes():
    assert len(HTML_FORMS) >= 10
    assert sum(1 for p in PDF_FORMS if "chapado" not in p.name) >= 2
    for p in FORMS:
        assert (FIX / f"{p.stem}.expected.yaml").exists(), f"falta gabarito de {p.name}"


@pytest.mark.parametrize("src", FORMS, ids=lambda p: p.name)
def test_convertido_passa_schema_e_lint(src, tmp_path):
    res = convert_file(src, tmp_path, cfg=None, use_llm=False)
    path = Path(res["path"])
    svc = load_service(path)
    assert lint_service(svc, default_registry()) == []
    assert svc.review == "pending"
    assert svc.slots and res["slots"] == len(svc.slots)
    assert all(s.confidence is not None and 0 < s.confidence <= 1 for s in svc.slots)
    assert svc.confirm and "{" in svc.confirm.template
    assert svc.action and svc.action.idempotency_key == f"{{session_id}}:{svc.service}"
    assert svc.source["kind"] in ("html", "pdf_acroform", "pdf_text")
    assert len(svc.intent.examples) >= 3
    expected_tool = "http.post_form" if src.suffix == ".html" else "email.enviar"
    assert svc.action.tool == expected_tool
    text = path.read_text(encoding="utf-8")
    assert text.count("# TODO:") == len(svc.source.get("todos") or [])


def test_serve_recusa_convertidos(tmp_path):
    for src in FORMS:
        convert_file(src, tmp_path, use_llm=False)
    tools = default_registry()
    approved, errors = load_services(tmp_path, only_approved=True, tools=tools)
    assert errors == []  # recusa é por review: pending, não por YAML quebrado
    assert approved == {}
    todos, errors = load_services(tmp_path, only_approved=False, tools=tools)
    assert errors == [] and len(todos) == len(FORMS)


def test_metrica_vs_gabarito(tmp_path, capsys):
    mod = _metric_module()
    rows, metric = mod.run(tmp_path)
    with capsys.disabled():
        ok = sum(r["ok"] for r in rows)
        tot = sum(r["expected"] for r in rows)
        print(f"\n[métrica conversor] {ok}/{tot} slots com tipo E obrigatoriedade corretos = {metric:.1%}")
    assert metric >= 0.8


def test_post_form_recebe_nomes_originais(tmp_path, monkeypatch):
    res = convert_file(FIX / "matricula_curso.html", tmp_path, use_llm=False)
    svc = load_service(Path(res["path"]))
    raw = {"aluno_nome": "Ana Souza", "aluno_cpf": "529.982.247-25", "aluno_nascimento": "2008-03-01",
           "aluno_email": "ana@x.com", "responsavel_nome": "Rui", "responsavel_telefone": "(51) 99999-0000",
           "curso": "TI", "turno": "noite"}
    args = render(svc.action.args, {**raw, "session_id": "s1"})
    sent = {}

    def fake_post(url, data=None, timeout=None, follow_redirects=None):
        sent.update(url=url, data=data)
        return SimpleNamespace(status_code=200, headers={"x-request-id": "REQ-1"})

    monkeypatch.setattr("transformsite.tools.builtin.httpx.post", fake_post)
    reg = default_registry()
    out = reg.call(svc.action.tool, ToolContext(cfg=None, store=None), **args)
    assert out["id"] == "REQ-1"
    assert sent["url"] == "https://secretaria.etprogresso.edu.br/matricula/nova"
    assert set(sent["data"]) == {"aluno[nome]", "aluno[cpf]", "aluno[nascimento]", "aluno[email]",
                                 "responsavel[nome]", "responsavel[telefone]", "curso", "turno"}
    assert sent["data"]["aluno[cpf]"] == "529.982.247-25" and sent["data"]["curso"] == "TI"


def test_hidden_literal_e_csrf(tmp_path):
    res = convert_file(FIX / "contato.html", tmp_path, use_llm=False)
    svc = load_service(Path(res["path"]))
    fields = svc.action.args["fields"]
    assert fields["origem"] == "site"
    assert "csrf_token" not in fields and "website" not in fields  # token dinâmico e honeypot fora
    assert any("csrf_token" in t for t in svc.source["todos"])
    assert svc.slot("assunto").values == ["duvida", "elogio", "reclamacao", "trabalhe"]
    assert svc.slot("assunto").labels["reclamacao"] == "Reclamação"
    assert svc.slot("nome").max_len == 120


def test_condicional_e_todo_js(tmp_path):
    res = convert_file(FIX / "cancelamento.html", tmp_path, use_llm=False)
    svc = load_service(Path(res["path"]))
    assert svc.slot("motivo_outro").when == "{motivo} == outro"
    assert svc.slot("cep_novo").when == "{motivo} == mudanca"
    assert svc.slot("aceita_oferta").when is None
    text = Path(res["path"]).read_text(encoding="utf-8")
    assert "# TODO: campo 'aceita_oferta': lógica condicional não detectada" in text
    svc2 = load_service(Path(convert_file(FIX / "ouvidoria.html", tmp_path, use_llm=False)["path"]))
    assert svc2.slot("nome").when == "{identificar} == sim"
    assert svc2.slot("tipo").type == "enum" and svc2.slot("tipo").values == ["reclamacao", "denuncia", "sugestao", "elogio"]


def test_pdf_acroform(tmp_path):
    cfg = _cfg()
    res = convert_file(FIX / "pdf_inscricao_bolsa.pdf", tmp_path, cfg=cfg, use_llm=True)  # provider fake → sem LLM
    svc = load_service(Path(res["path"]))
    assert svc.source["kind"] == "pdf_acroform"
    assert svc.action.args["to"] == "equipe@empresa.com.br"
    assert svc.slot("curso").type == "enum" and "Direito" in svc.slot("curso").values
    assert svc.slot("turno").values == ["manha", "noite"]
    assert svc.slot("declaracao").type == "yes_no" and svc.slot("declaracao").required
    assert svc.slot("cpf").type == "cpf" and svc.slot("cpf").max_len is None
    assert any("PDF preenchido" in t for t in svc.source["todos"])
    reemb = load_service(Path(convert_file(FIX / "pdf_reembolso.pdf", tmp_path, use_llm=False)["path"]))
    assert reemb.slot("tipo_despesa").labels == {"transp": "Transporte", "alim": "Alimentação", "hosp": "Hospedagem", "outros": "Outros"}
    assert reemb.slot("matricula_funcional").max_len == 8  # Text2 → nome pelo /TU


def test_pdf_chapado_confianca_baixa(tmp_path):
    svc = load_service(Path(convert_file(FIX / "pdf_chapado_visita.pdf", tmp_path, use_llm=False)["path"]))
    assert svc.source["kind"] == "pdf_text"
    assert {s.name for s in svc.slots} >= {"nome", "telefone", "email", "cep", "data_preferida"}
    assert all(s.confidence == 0.3 for s in svc.slots)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def local_server(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "orcamento.html").write_text((FIX / "orcamento.html").read_text(encoding="utf-8"), encoding="utf-8")
    multi = (FIX / "contato.html").read_text(encoding="utf-8").replace(
        "</main>", "</main>" + (FIX / "inscricao_evento.html").read_text(encoding="utf-8").split("<section>")[1].split("</section>")[0]
    )
    (site / "dois.html").write_text(multi, encoding="utf-8")
    handler = functools.partial(_QuietHandler, directory=str(site))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_conversao_por_url(local_server, tmp_path):
    out = tmp_path / "out"
    res = convert_file(f"{local_server}/orcamento.html", out, use_llm=False)
    svc = load_service(Path(res["path"]))
    assert svc.action.args["url"] == f"{local_server}/orcamento/enviar"  # action relativo → absoluto
    assert svc.slot("convidados").type == "number"
    assert svc.source["origin"].startswith("http://127.0.0.1")
    # página com vários formulários: busca e newsletter ignoradas, 2 serviços gerados
    res2 = convert_file(f"{local_server}/dois.html", out, use_llm=False)
    assert isinstance(res2["path"], list) and len(res2["path"]) == 2
    names = {load_service(Path(p)).service for p in res2["path"]}
    assert len(names) == 2


def test_formularios_irrelevantes_ignorados():
    html = """<form role=search action=/busca><input name=q></form>
    <form action=/login method=post><input name=user><input type=password name=pw></form>
    <form action=/news><input type=email name=e></form>"""
    forms, _ = extract_html(html, origin="x")
    assert forms == []
    with pytest.raises(ConvertError):
        convert_file(FIX / "make_pdfs.py", Path("/nonexistent-out"), use_llm=False)


def test_refino_llm_aplica_so_tipo_valido(tmp_path, monkeypatch):
    forms, _ = extract_html((FIX / "labels_ruins.html").read_text(encoding="utf-8"), origin="x")
    svc = build_service(forms[0], "labels_ruins")
    n = len(svc["slots"])
    resposta = {
        "slots": {
            "cidade_onde_mora": {"prompt": "Em qual cidade você mora?", "type": "tipo_inventado"},
            "pretensao_salarial": {"prompt": "Qual sua pretensão salarial?", "type": "number"},
            "fale_um_pouco_sobre": {"type": "email"},
        },
        "examples": ["quero trabalhar aí", "tem vaga?", "enviar currículo"],
    }
    import json

    monkeypatch.setattr("transformsite.llm._fake_default", lambda m, j: json.dumps(resposta))
    notes = refine_with_llm(svc, _cfg())
    s = {x["name"]: x for x in svc["slots"]}
    assert len(svc["slots"]) == n  # nunca remove slots
    assert s["cidade_onde_mora"]["prompt"] == "Em qual cidade você mora?"
    assert s["cidade_onde_mora"]["type"] == "text"  # tipo inválido ignorado
    assert s["fale_um_pouco_sobre"]["type"] == "email" and notes
    assert svc["intent"]["examples"][0] == "quero trabalhar aí"
    assert svc["review"] == "pending"


def test_sem_cfg_nao_chama_llm(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM não deveria ser chamado")

    monkeypatch.setattr("transformsite.convert.refine_with_llm", boom)
    convert_file(FIX / "contato.html", tmp_path, cfg=None, use_llm=True)
    convert_file(FIX / "contato.html", tmp_path, cfg=_cfg("fake"), use_llm=True)


def test_nao_sobrescreve_servico_aprovado(tmp_path):
    import shutil

    scaffold = ROOT / "src" / "transformsite" / "scaffold" / "default" / "services" / "fale_conosco.yaml"
    shutil.copy(scaffold, tmp_path / "fale_conosco.yaml")
    before = (tmp_path / "fale_conosco.yaml").read_text(encoding="utf-8")
    res = convert_file(FIX / "contato.html", tmp_path, use_llm=False)
    assert (tmp_path / "fale_conosco.yaml").read_text(encoding="utf-8") == before
    svc = load_service(Path(res["path"]))
    assert svc.service == "fale_conosco_2"
    assert svc.action.idempotency_key == "{session_id}:fale_conosco_2"
    assert any("já existia o serviço 'fale_conosco'" in t for t in svc.source["todos"])
    approved, errors = load_services(tmp_path, only_approved=True, tools=default_registry())
    assert errors == [] and set(approved) == {"fale_conosco"}
    # reconverter por cima do próprio pending é permitido (mesmo arquivo)
    res2 = convert_file(FIX / "contato.html", tmp_path, use_llm=False)
    assert res2["path"] == res["path"]


def test_nomes_de_slot():
    assert snake("aluno[nome]") == "aluno_nome"
    assert snake("txtCPF") == "txt_cpf"
    assert snake("E-mail*") == "email"
    assert snake("1º campo")[0].isalpha()
    forms, _ = extract_html(
        '<form action="http://x/y" method=post><input name="session_id" placeholder="Sessão">'
        '<input name="Nome" placeholder="Nome"><input name="nome" placeholder="Nome de novo"></form>', origin="x")
    svc = build_service(forms[0], "t")
    names = [s["name"] for s in svc["slots"]]
    assert names == ["session_id_campo", "nome", "nome_2"]
    assert set(svc["action"]["args"]["fields"]) == {"session_id", "Nome", "nome"}
    assert yaml.safe_dump(svc)  # serializável
