"""Ferramentas padrão: tickets (crm), e-mail, webhook e POST em formulário legado."""

from __future__ import annotations

import httpx

from ..mailer import send_email


def crm_criar_ticket(ctx, subject: str, body: str, queue: str = "atendimento", **extra):
    tid = ctx.store.ticket(ctx.session.get("id"), queue, "servico", subject, body, {"service": ctx.service, **extra})
    to = extra.get("notify_email") or _first_email_target(ctx.cfg)
    mail = None
    if to:
        mail = send_email(ctx.cfg, to, f"[{tid}] {subject}", body, reply_to=extra.get("from"))
    return {"id": tid, "email": mail}


def _first_email_target(cfg) -> str | None:
    for t in cfg.handoff.targets:
        if t.startswith("email:"):
            return t.split(":", 1)[1]
    return None


def email_enviar(ctx, to: str, subject: str, body: str, **_):
    return send_email(ctx.cfg, to, subject, body)


def webhook(ctx, url: str, payload: dict | None = None, **extra):
    r = httpx.post(url, json={"service": ctx.service, "data": payload or extra}, timeout=20)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text[:500]}
    return {"status": r.status_code, **(data if isinstance(data, dict) else {"data": data})}


def http_post_form(ctx, url: str, fields: dict, method: str = "post", **_):
    """Envia os dados coletados ao formulário legado (o sistema antigo continua recebendo)."""
    if method.lower() == "get":
        r = httpx.get(url, params=fields, timeout=20, follow_redirects=True)
    else:
        r = httpx.post(url, data=fields, timeout=20, follow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError(f"formulário legado respondeu {r.status_code}")
    return {"status": r.status_code, "id": r.headers.get("x-request-id") or f"HTTP{r.status_code}"}


def register(reg):
    reg.add("crm.criar_ticket", crm_criar_ticket, side_effects=True, description="Cria ticket e avisa a equipe por e-mail")
    reg.add("email.enviar", email_enviar, side_effects=True, description="Envia e-mail")
    reg.add("webhook", webhook, side_effects=True, description="POST JSON para uma URL")
    reg.add("http.post_form", http_post_form, side_effects=True, description="Submete o formulário legado")
