"""Fase 3 — adaptadores de canal. Tudo local: nenhuma chamada de rede real (httpx.MockTransport / fakes)."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from transformsite.channels import get_channel
from transformsite.channels.email import EmailChannel, clean_body, is_auto_mail, parse_rfc822
from transformsite.channels.telegram import TelegramChannel
from transformsite.channels.web import WIDGET_JS, WebChannel
from transformsite.channels.whatsapp import CloudAPIChannel, EvolutionChannel
from transformsite.messages import OutMsg

CHOICES5 = [{"id": f"o{i}", "label": f"Opção {i}"} for i in range(1, 6)]


def opts(n: int) -> list[dict]:
    return [{"id": f"o{i}", "label": f"Op {i}"} for i in range(1, n + 1)]


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    from transformsite.cli import main
    from transformsite.config import load

    root = tmp_path_factory.mktemp("proj") / "p"
    main(["init", str(root)])
    cfg = load(root)
    cfg.llm.provider = "fake"
    return cfg


@pytest.fixture
def agent(project, tmp_path):
    import copy

    from transformsite.engine import Agent
    from transformsite.store import Store

    cfg = copy.deepcopy(project)
    return Agent(cfg, store=Store(tmp_path / "state.sqlite"))


class Recorder:
    """Transporte httpx falso: grava requisições e responde com `responder(request)`."""

    def __init__(self, responder=None):
        self.requests: list[httpx.Request] = []
        self.responder = responder or (lambda req: httpx.Response(200, json={"ok": True, "result": []}))

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return self.responder(req)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def bodies(self) -> list[dict]:
        return [json.loads(r.content or b"{}") for r in self.requests]


# ====================== Telegram ======================
TG_MESSAGE = {
    "update_id": 10001,
    "message": {
        "message_id": 55,
        "from": {"id": 777, "is_bot": False, "first_name": "João", "last_name": "Silva", "language_code": "pt-br"},
        "chat": {"id": 777, "first_name": "João", "type": "private"},
        "date": 1790000000,
        "text": "quero agendar",
    },
}
TG_CALLBACK = {
    "update_id": 10002,
    "callback_query": {
        "id": "4382bfdwdsb323b2d9",
        "from": {"id": 777, "first_name": "João"},
        "message": {
            "message_id": 56,
            "chat": {"id": 777, "type": "private"},
            "text": "Confirmo?",
            "reply_markup": {"inline_keyboard": [[{"text": "Sim, confirmar", "callback_data": "sim"}], [{"text": "Não, corrigir", "callback_data": "nao"}]]},
        },
        "data": "sim",
    },
}


def test_telegram_parse_message_e_callback():
    ch = TelegramChannel()
    m = ch.to_inbound(TG_MESSAGE)
    assert (m.channel, m.user_id, m.text, m.msg_id) == ("telegram", "777", "quero agendar", "tg:777:55")
    assert m.meta["name"] == "João Silva" and m.meta["lang"] == "pt-br"
    c = ch.to_inbound(TG_CALLBACK)
    assert (c.user_id, c.choice_id, c.text, c.msg_id) == ("777", "sim", "Sim, confirmar", "tgcb:4382bfdwdsb323b2d9")


def test_telegram_ignora_sem_texto_e_grupos():
    ch = TelegramChannel()
    sticker = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 1, "type": "private"}, "sticker": {"file_id": "x"}}}
    grupo = {"update_id": 2, "message": {"message_id": 2, "chat": {"id": -100, "type": "supergroup"}, "text": "oi"}}
    editada = {"update_id": 3, "edited_message": {"message_id": 3, "chat": {"id": 1, "type": "private"}, "text": "oi"}}
    assert ch.to_inbound(sticker) is None and ch.to_inbound(grupo) is None and ch.to_inbound(editada) is None


def test_telegram_botoes_e_degradacao():
    ch = TelegramChannel()
    p = ch.to_payloads("777", OutMsg("Escolha:", choices=CHOICES5))
    assert len(p) == 1 and p[0]["method"] == "sendMessage" and p[0]["chat_id"] == "777"
    kb = p[0]["reply_markup"]["inline_keyboard"]
    assert [row[0]["callback_data"] for row in kb] == ["o1", "o2", "o3", "o4", "o5"] and all(len(r) == 1 for r in kb)
    assert "[Opção 1]" in ch.payload_text(p[0])
    # acima de 8 → numerado
    p9 = ch.to_payloads("777", OutMsg("Escolha:", choices=opts(9)))
    assert "reply_markup" not in p9[0] and "9. Op 9" in p9[0]["text"]
    # callback_data > 64 bytes → numerado
    plong = ch.to_payloads("777", OutMsg("x", choices=[{"id": "z" * 80, "label": "Longo"}]))
    assert "reply_markup" not in plong[0] and "1. Longo" in plong[0]["text"]


def test_telegram_divide_texto_longo():
    ch = TelegramChannel()
    text = ("linha de texto bem comprida " * 10 + "\n") * 40  # ~11k chars
    p = ch.to_payloads("1", OutMsg(text, choices=opts(2)))
    assert len(p) >= 3 and all(len(x["text"]) <= 4000 for x in p)
    assert "reply_markup" in p[-1] and all("reply_markup" not in x for x in p[:-1])


def test_telegram_send_mock(project, monkeypatch):
    import copy

    cfg = copy.deepcopy(project)
    cfg.channels.telegram = {"token_env": "TG_TESTE", "api_base": "http://tg.local"}
    monkeypatch.setenv("TG_TESTE", "123:ABC")
    rec = Recorder()
    ch = TelegramChannel(cfg, http=rec.client())
    ch.send("777", OutMsg("Olá", choices=opts(2)))
    assert str(rec.requests[0].url) == "http://tg.local/bot123:ABC/sendMessage"
    body = rec.bodies()[0]
    assert body["chat_id"] == "777" and body["text"] == "Olá" and "method" not in body
    assert body["reply_markup"]["inline_keyboard"][1][0]["callback_data"] == "o2"


def test_telegram_send_erro_api(project, monkeypatch):
    import copy

    cfg = copy.deepcopy(project)
    cfg.channels.telegram = {"token_env": "TG_TESTE"}
    monkeypatch.setenv("TG_TESTE", "t")
    rec = Recorder(lambda r: httpx.Response(400, json={"ok": False, "description": "chat not found"}))
    with pytest.raises(RuntimeError, match="chat not found"):
        TelegramChannel(cfg, http=rec.client()).send("1", OutMsg("x"))


def test_telegram_poller_mock(project, agent, monkeypatch):
    agent.cfg.channels.telegram = {"token_env": "TG_TESTE", "api_base": "http://tg.local"}
    monkeypatch.setenv("TG_TESTE", "t")
    stop = threading.Event()
    calls = {"n": 0}

    def responder(req):
        method = req.url.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("rede caiu")  # 1ª: falha de rede → backoff
            if calls["n"] == 2:
                return httpx.Response(200, json={"ok": True, "result": [TG_MESSAGE, TG_CALLBACK]})
            stop.set()
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": {}})

    rec = Recorder(responder)
    ch = TelegramChannel(agent.cfg, http=rec.client())
    monkeypatch.setattr(stop, "wait", lambda t=None: None)  # sem dormir no backoff
    ch.run_poller(agent, stop)
    methods = [r.url.path.rsplit("/", 1)[-1] for r in rec.requests]
    assert "answerCallbackQuery" in methods and "sendMessage" in methods
    offsets = [json.loads(r.content).get("offset") for r in rec.requests if r.url.path.endswith("getUpdates")]
    assert offsets[-1] == 10003


# ====================== WhatsApp — Evolution ======================
EVO_TEXT = {
    "event": "messages.upsert",
    "instance": "minha-instancia",
    "data": {
        "key": {"remoteJid": "5551999990000@s.whatsapp.net", "fromMe": False, "id": "3EB0C767D26A1D8E"},
        "pushName": "Maria",
        "message": {"conversation": "quero agendar"},
        "messageType": "conversation",
        "messageTimestamp": 1790000000,
    },
    "date_time": "2026-10-02T12:00:00.000Z",
    "sender": "5551888880000@s.whatsapp.net",
    "apikey": "xxx",
}


def _evo(message: dict, **key) -> dict:
    d = json.loads(json.dumps(EVO_TEXT))
    d["data"]["message"] = message
    d["data"]["key"].update(key)
    return d


def test_evolution_parse():
    ch = EvolutionChannel()
    m = ch.to_inbound(EVO_TEXT)
    assert (m.channel, m.user_id, m.text, m.msg_id) == ("whatsapp", "5551999990000", "quero agendar", "3EB0C767D26A1D8E")
    assert m.meta == {"phone": "+5551999990000", "name": "Maria"}
    assert ch.to_inbound(_evo({"extendedTextMessage": {"text": "oi com link"}})).text == "oi com link"
    b = ch.to_inbound(_evo({"buttonsResponseMessage": {"selectedButtonId": "sim", "selectedDisplayText": "Sim"}}))
    assert (b.choice_id, b.text) == ("sim", "Sim")
    lr = ch.to_inbound(_evo({"listResponseMessage": {"title": "Suporte", "singleSelectReply": {"selectedRowId": "suporte"}}}))
    assert (lr.choice_id, lr.text) == ("suporte", "Suporte")


def test_evolution_ignora_proprias_grupos_e_audio():
    ch = EvolutionChannel()
    assert ch.to_inbound(_evo({"conversation": "eco"}, fromMe=True)) is None
    assert ch.to_inbound(_evo({"conversation": "oi grupo"}, remoteJid="120363000000@g.us")) is None
    assert ch.to_inbound(_evo({"audioMessage": {"seconds": 4}})) is None
    assert ch.to_inbound({"event": "connection.update", "data": {}}) is None


def test_evolution_degradacao_numerada_e_lista():
    ch = EvolutionChannel()
    p = ch.to_payloads("5551999990000", OutMsg("Escolha:", choices=CHOICES5))
    assert p == [{"path": "sendText", "number": "5551999990000", "text": "Escolha:\n\n1. Opção 1\n2. Opção 2\n3. Opção 3\n4. Opção 4\n5. Opção 5\n(responda com o número)"}]
    p3 = ch.to_payloads("1", OutMsg("x", choices=opts(3)))
    assert p3[0]["path"] == "sendText" and "3. Op 3" in p3[0]["text"]  # Baileys: sem botões garantidos
    lst = EvolutionChannel(use_lists=True).to_payloads("1", OutMsg("Escolha:", choices=CHOICES5))
    assert lst[0]["path"] == "sendList" and [r["rowId"] for r in lst[0]["sections"][0]["rows"]] == ["o1", "o2", "o3", "o4", "o5"]
    assert EvolutionChannel(use_lists=True).to_payloads("1", OutMsg("x", choices=opts(11)))[0]["path"] == "sendText"


def test_evolution_send_mock(project, monkeypatch):
    import copy

    cfg = copy.deepcopy(project)
    cfg.channels.whatsapp = {"base_url": "http://evo.local:8080/", "instance": "loja", "apikey_env": "EVO_TESTE"}
    monkeypatch.setenv("EVO_TESTE", "chave")
    rec = Recorder(lambda r: httpx.Response(201, json={"key": {"id": "x"}}))
    EvolutionChannel(cfg, http=rec.client()).send("5551999990000", OutMsg("Olá"))
    req = rec.requests[0]
    assert str(req.url) == "http://evo.local:8080/message/sendText/loja" and req.headers["apikey"] == "chave"
    assert rec.bodies()[0] == {"number": "5551999990000", "text": "Olá"}


def test_evolution_webhook_router(agent, monkeypatch):
    sent = []
    ch = EvolutionChannel(agent.cfg)
    monkeypatch.setattr(ch, "_post", lambda p: sent.append(p))
    app = FastAPI()
    app.include_router(ch.router(agent))
    r = TestClient(app).post("/webhook/whatsapp", json=EVO_TEXT)
    assert r.status_code == 200
    assert sent and all(p["number"] == "5551999990000" for p in sent)


# ====================== WhatsApp — Cloud API ======================
CLOUD_TEXT = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "102290129340398",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "15550783881", "phone_number_id": "106540352242922"},
                        "contacts": [{"profile": {"name": "Sheena Nelson"}, "wa_id": "16505551234"}],
                        "messages": [{"from": "16505551234", "id": "wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQTRBNjU5OUFFRTAzODEwMTQ0RgA=", "timestamp": "1749416383", "type": "text", "text": {"body": "Does it come in another color?"}}],
                    },
                }
            ],
        }
    ],
}


def _cloud_msg(m: dict) -> dict:
    d = json.loads(json.dumps(CLOUD_TEXT))
    d["entry"][0]["changes"][0]["value"]["messages"] = [m]
    return d


def test_cloud_parse():
    ch = CloudAPIChannel()
    m = ch.to_inbound(CLOUD_TEXT)
    assert (m.channel, m.user_id, m.text) == ("whatsapp_cloud", "16505551234", "Does it come in another color?")
    assert m.msg_id.startswith("wamid.") and m.meta == {"phone": "+16505551234", "name": "Sheena Nelson"}
    br = ch.to_inbound(_cloud_msg({"from": "16505551234", "id": "w2", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "sim", "title": "Sim"}}}))
    assert (br.choice_id, br.text) == ("sim", "Sim")
    lr = ch.to_inbound(_cloud_msg({"from": "16505551234", "id": "w3", "type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "o4", "title": "Op 4", "description": ""}}}))
    assert lr.choice_id == "o4"
    status = json.loads(json.dumps(CLOUD_TEXT))
    v = status["entry"][0]["changes"][0]["value"]
    del v["messages"]
    v["statuses"] = [{"id": "wamid.x", "status": "delivered", "recipient_id": "16505551234"}]
    assert ch.to_inbound(status) is None


def test_cloud_degradacao_botoes_lista_numerado():
    ch = CloudAPIChannel()
    b = ch.to_payloads("1", OutMsg("Escolha:", choices=opts(3)))[0]
    assert b["type"] == "interactive" and b["interactive"]["type"] == "button" and len(b["interactive"]["action"]["buttons"]) == 3
    lst = ch.to_payloads("1", OutMsg("Escolha:", choices=CHOICES5))[0]
    assert lst["interactive"]["type"] == "list" and len(lst["interactive"]["action"]["sections"][0]["rows"]) == 5
    num = ch.to_payloads("1", OutMsg("Escolha:", choices=opts(12)))[0]
    assert num["type"] == "text" and "12. Op 12" in num["text"]["body"]
    # rótulo > 20 chars não cabe em botão → lista com descrição
    longo = ch.to_payloads("1", OutMsg("x", choices=[{"id": "a", "label": "segunda, 05/10 às 12:00 (sala 2)"}, {"id": "b", "label": "B"}]))[0]
    row = longo["interactive"]["action"]["sections"][0]["rows"][0]
    assert longo["interactive"]["type"] == "list" and len(row["title"]) <= 24 and row["title"] + row["description"] == "segunda, 05/10 às 12:00 (sala 2)"


def test_cloud_send_mock(project, monkeypatch):
    import copy

    cfg = copy.deepcopy(project)
    cfg.channels.whatsapp = {"phone_number_id": "106540352242922", "token_env": "WA_TESTE"}
    monkeypatch.setenv("WA_TESTE", "tok")
    rec = Recorder(lambda r: httpx.Response(200, json={"messages": [{"id": "wamid.x"}]}))
    CloudAPIChannel(cfg, http=rec.client()).send("16505551234", OutMsg("Escolha:", choices=opts(2)))
    req = rec.requests[0]
    assert str(req.url) == "https://graph.facebook.com/v20.0/106540352242922/messages"
    assert req.headers["authorization"] == "Bearer tok" and rec.bodies()[0]["interactive"]["type"] == "button"


def test_cloud_verificacao_e_assinatura(agent, monkeypatch):
    agent.cfg.channels.whatsapp = {"verify_token": "segredo-verif", "app_secret_env": "WA_APP_SECRET"}
    monkeypatch.setenv("WA_APP_SECRET", "app-secret")
    sent = []
    ch = CloudAPIChannel(agent.cfg)
    monkeypatch.setattr(ch, "_post", lambda p: sent.append(p))
    app = FastAPI()
    app.include_router(ch.router(agent))
    c = TestClient(app)
    ok = c.get("/webhook/whatsapp-cloud", params={"hub.mode": "subscribe", "hub.verify_token": "segredo-verif", "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    assert c.get("/webhook/whatsapp-cloud", params={"hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "1"}).status_code == 403
    body = json.dumps(CLOUD_TEXT).encode()
    assert c.post("/webhook/whatsapp-cloud", content=body, headers={"x-hub-signature-256": "sha256=00"}).status_code == 403
    assert not sent
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert c.post("/webhook/whatsapp-cloud", content=body, headers={"x-hub-signature-256": sig, "content-type": "application/json"}).status_code == 200
    assert sent and sent[0]["to"] == "16505551234"


# ====================== E-mail ======================
EMAIL_REPLY = {
    "from": "Maria Souza <Maria.Souza@Exemplo.com>",
    "subject": "RE: Re: Agendamento",
    "body": (
        "Pode ser na quinta às 10.\r\n\r\n"
        "-- \r\nMaria Souza\r\nGerente\r\n\r\n"
        "Em seg., 5 de out. de 2026 às 10:00, Atendimento <bot@empresa.com> escreveu:\r\n"
        "> Qual horário prefere?\r\n> 1. 09:00\r\n"
    ),
    "message_id": "<CAF123@mail.gmail.com>",
    "in_reply_to": "<abc@transformsite>",
}


def test_email_parse_e_limpeza():
    ch = EmailChannel()
    m = ch.to_inbound(EMAIL_REPLY)
    assert (m.channel, m.user_id, m.text, m.msg_id) == ("email", "maria.souza@exemplo.com", "Pode ser na quinta às 10.", "<CAF123@mail.gmail.com>")
    assert m.meta["email"] == "maria.souza@exemplo.com" and m.meta["name"] == "Maria Souza" and m.meta["subject"] == "Agendamento"
    assert clean_body("Sim, confirmo.\n\nOn Mon, Oct 5, 2026 at 10:00 AM Bot <b@x.com> wrote:\n> antigo") == "Sim, confirmo."
    assert clean_body("ok\n\nEm seg., 5 de out. de 2026 às 10:00, Fulano de Tal <f@x.com>\nescreveu:\n> antigo") == "ok"
    assert clean_body("> citado\nresposta nova\n> mais citado") == "resposta nova"
    assert clean_body("certo\n\n-----Mensagem original-----\nDe: x\nEnviado: y") == "certo"


def test_email_saida_numerada_assunto_re():
    ch = EmailChannel()
    ch.to_inbound(EMAIL_REPLY)
    p = ch.to_payloads("maria.souza@exemplo.com", OutMsg("Escolha:", choices=CHOICES5))[0]
    assert p["subject"] == "Re: Agendamento" and p["in_reply_to"] == "<CAF123@mail.gmail.com>"
    assert "1. Opção 1" in p["body"] and "5. Opção 5" in p["body"] and "(responda com o número)" in p["body"]
    merged = ch.merge([OutMsg("Certo, vamos lá."), OutMsg("Qual assunto?", choices=opts(2))])
    assert merged.text.startswith("Certo, vamos lá.\n\nQual assunto?") and len(merged.choices) == 2


def test_email_send_outbox(project, tmp_path):
    import copy

    cfg = copy.deepcopy(project)
    cfg.root = tmp_path
    cfg.email.smtp_host = ""  # sem SMTP → .eml em outbox (nada sai da máquina)
    ch = EmailChannel(cfg)
    ch.to_inbound(EMAIL_REPLY)
    ch.send("maria.souza@exemplo.com", OutMsg("Agendado!"))
    emls = list((tmp_path / cfg.email.outbox_dir).glob("*.eml"))
    assert len(emls) == 1
    raw = emls[0].read_bytes()
    assert b"Subject: Re: Agendamento" in raw and b"Agendado!" in raw


def test_email_anti_laco():
    assert is_auto_mail({"Auto-Submitted": "auto-replied", "From": "x@y.com"})
    assert is_auto_mail({"Precedence": "bulk", "From": "x@y.com"})
    assert is_auto_mail({"From": "MAILER-DAEMON@mx.google.com"})
    assert is_auto_mail({"From": "Bot <bot@empresa.com>"}, own_address="bot@empresa.com")
    assert not is_auto_mail({"From": "Maria <maria@exemplo.com>", "Auto-Submitted": "no"}, own_address="bot@empresa.com")


class FakeIMAP:
    def __init__(self, raws: list[bytes]):
        self.raws = raws
        self.seen: list[bytes] = []

    def select(self, box):
        return "OK", [b"1"]

    def search(self, charset, crit):
        assert crit == "UNSEEN"
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raws)))]

    def fetch(self, num, what):
        return "OK", [(b"1 (BODY[] {n}", self.raws[int(num) - 1]), b")"]

    def store(self, num, flags, val):
        self.seen.append(num)
        return "OK", []

    def logout(self):
        return "BYE", []


def test_email_poller_imap_fake(agent, tmp_path):
    agent.cfg.root = tmp_path
    raw_ok = b"From: Ana <ana@exemplo.com>\r\nTo: bot@empresa.com\r\nSubject: Duvida\r\nMessage-ID: <m1@x>\r\n\r\noi\r\n"
    raw_auto = b"From: Ana <ana@exemplo.com>\r\nSubject: Ausente\r\nAuto-Submitted: auto-replied\r\nMessage-ID: <m2@x>\r\n\r\nEstou de ferias\r\n"
    imap = FakeIMAP([raw_ok, raw_auto])
    ch = EmailChannel(agent.cfg, imap_factory=lambda: imap)
    assert parse_rfc822(raw_ok)["subject"] == "Duvida"
    stop = threading.Event()
    stop.wait = lambda t=None: stop.set()  # uma volta só
    ch.run_poller(agent, stop)
    assert imap.seen == [b"1", b"2"]  # ambos marcados como lidos
    emls = list((tmp_path / agent.cfg.email.outbox_dir).glob("*.eml"))
    assert len(emls) == 1  # só o e-mail humano gerou resposta (um e-mail por turno)
    assert b"Subject: Re: Duvida" in emls[0].read_bytes()


# ====================== Web ======================
def test_web_widget_e_api(agent):
    ch = WebChannel(agent.cfg)
    ch.attach(agent)
    app = FastAPI()
    app.include_router(ch.router(agent))
    c = TestClient(app)
    js = c.get("/widget.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "localStorage" in js.text
    assert "innerHTML" not in WIDGET_JS and "textContent" in WIDGET_JS
    page = c.get("/chat")
    assert page.status_code == 200 and '<script src="/widget.js"' in page.text
    r = c.post("/api/web/message", json={"session": None, "text": "oi", "choice": None}, headers={"origin": "https://site.gov.br"})
    d = r.json()
    assert r.status_code == 200 and len(d["session"]) >= 8 and d["messages"]
    assert r.headers["access-control-allow-origin"] == "*"
    menu = d["messages"][-1]
    assert menu["choices"] and {"id", "label"} <= set(menu["choices"][0])
    # clique no botão do menu
    r2 = c.post("/api/web/message", json={"session": d["session"], "text": menu["choices"][0]["label"], "choice": menu["choices"][0]["id"]})
    assert "nome" in r2.json()["messages"][-1]["text"].lower()
    # resposta humana via painel chega pelo poll
    sess = agent.store.session("web", d["session"])
    agent.human_reply(sess["id"], "Olá, aqui é a Ana da equipe.")
    polled = c.get("/api/web/poll", params={"session": d["session"]}).json()["messages"]
    assert [m["text"] for m in polled] == ["Olá, aqui é a Ana da equipe."]
    assert c.get("/api/web/poll", params={"session": d["session"]}).json()["messages"] == []
    assert c.options("/api/web/message", headers={"origin": "https://x.com", "access-control-request-method": "POST"}).status_code == 204


def test_web_via_create_app(agent):
    """Integração com server.create_app: POST real em /api/web/message (regressão do 422 'missing query.request')."""
    from transformsite.server import create_app

    app = create_app(agent.cfg, agent=agent, channels=["web"], start_pollers=False)
    c = TestClient(app)
    r = c.post("/api/web/message", json={"session": "sessao-teste-123", "text": "oi"})
    assert r.status_code == 200, r.text
    assert r.json()["session"] == "sessao-teste-123" and r.json()["messages"]
    assert c.get("/widget.js").status_code == 200


def test_web_botoes_citacoes_e_degradacao():
    ch = WebChannel()
    p = ch.to_payloads("s", OutMsg("Resposta.", choices=CHOICES5, citations=[{"n": 1, "url": "https://x.gov.br/a", "title": "A"}]))[0]
    assert p["text"] == "Resposta." and len(p["choices"]) == 5 and p["citations"][0]["url"] == "https://x.gov.br/a"
    assert "[Opção 5]" in ch.payload_text(p) and "[1] https://x.gov.br/a" in ch.payload_text(p)
    p7 = ch.to_payloads("s", OutMsg("x", choices=opts(7)))[0]
    assert p7["choices"] == [] and "7. Op 7" in p7["text"]


def test_web_cors_restrito(project):
    import copy

    cfg = copy.deepcopy(project)
    cfg.channels.web = {"enabled": True, "allowed_origins": ["https://site.gov.br"]}
    ch = WebChannel(cfg)
    assert ch._cors("https://site.gov.br")["Access-Control-Allow-Origin"] == "https://site.gov.br"
    assert ch._cors("https://malicioso.com") == {}


# ====================== comum: attach, dedup, conformance ======================
@pytest.mark.parametrize("name", ["telegram", "whatsapp", "whatsapp_cloud", "email", "web"])
def test_dedup_por_msg_id(agent, name):
    ch = get_channel(name)
    first = ch.exchange(agent, "5551999990000", "oi", msg_id="dup-1")
    again = ch.exchange(agent, "5551999990000", "oi", msg_id="dup-1")
    assert first and again == []


def test_attach_hooks_por_canal(agent, monkeypatch):
    tg, wa = TelegramChannel(agent.cfg), EvolutionChannel(agent.cfg)
    sent = []
    monkeypatch.setattr(tg, "send", lambda u, o: sent.append(("telegram", u, o.text)))
    monkeypatch.setattr(wa, "send", lambda u, o: sent.append(("whatsapp", u, o.text)))
    tg.attach(agent)
    wa.attach(agent)
    assert "telegram" in agent.notifier.senders and "whatsapp" in agent.notifier.senders
    ch_in = wa.to_inbound(EVO_TEXT)
    agent.handle(ch_in)
    sess = agent.store.session("whatsapp", ch_in.user_id)
    agent.human_reply(sess["id"], "resposta humana")
    assert sent == [("whatsapp", "5551999990000", "resposta humana")]
    agent.notifier.senders["telegram"]("-1001234", "handoff!")
    assert sent[-1] == ("telegram", "-1001234", "handoff!")


def test_conformance_todos_os_canais(project):
    from transformsite.evals import run_scenarios

    sdir = Path(project.root) / "tests" / "cenarios"
    chans = ["cli", "telegram", "whatsapp", "email", "web"]
    res = run_scenarios(project, sdir, channels=chans)
    falhas = [(r["channel"], r["name"], r["error"]) for r in res["results"] if not r["ok"]]
    assert not falhas, falhas
    n = len(res["results"]) // len(chans)
    assert n >= 10 and res["passed"] == res["total"] == n * len(chans)
