"""Servidor HTTP: /health, webhooks/rotas dos canais e painel admin (/admin).

Concorrência: webhooks chegam em threads do pool do FastAPI. O `Agent` compartilha uma conexão
SQLite e estado de sessão em memória, então todas as chamadas que mexem em conversa
(`handle`, `takeover`, `release`, `human_reply`) passam por UM lock global (`agent_lock`).
É simples e suficiente para o volume de um atendimento; o custo é serializar turnos de
usuários diferentes (um turno lento de LLM atrasa os demais).
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from . import __version__
from .config import ProjectConfig

log = logging.getLogger("transformsite.server")


def lock_agent(agent) -> threading.RLock:
    """Envolve os métodos do agent com um lock global (idempotente). Retorna o lock."""
    lk = getattr(agent, "agent_lock", None)
    if lk is not None:
        return lk
    lk = threading.RLock()
    agent.agent_lock = lk
    for name in ("handle", "takeover", "release", "human_reply"):
        fn = getattr(agent, name)

        def wrap(fn=fn):
            @functools.wraps(fn)
            def locked(*a, **kw):
                with lk:
                    return fn(*a, **kw)

            return locked

        setattr(agent, name, wrap())
    return lk


def configured_channels(cfg: ProjectConfig) -> list[str]:
    """Canais ativos pela configuração (+ segredos presentes no ambiente)."""
    ch = cfg.channels
    names: list[str] = []
    if ch.web.get("enabled"):
        names.append("web")
    if os.environ.get(ch.telegram.get("token_env", "TELEGRAM_BOT_TOKEN")):
        names.append("telegram")
    prov = ch.whatsapp.get("provider")
    if prov and os.environ.get(ch.whatsapp.get("apikey_env") or ch.whatsapp.get("token_env") or "EVOLUTION_API_KEY"):
        names.append("whatsapp_cloud" if prov == "cloud" else "whatsapp")
    wc = getattr(ch, "whatsapp_cloud", None) or {}
    if wc.get("phone_number_id") and os.environ.get(wc.get("token_env") or "WHATSAPP_CLOUD_TOKEN") and "whatsapp_cloud" not in names:
        names.append("whatsapp_cloud")
    if ch.email.get("enabled"):
        names.append("email")
    return names


def _load_channels(cfg: ProjectConfig, channels: list | None) -> tuple[list, dict[str, str]]:
    from .channels import get_channel

    items = channels if channels is not None else configured_channels(cfg)
    out, errors = [], {}
    for c in items:
        if not isinstance(c, str):
            out.append(c)
            continue
        try:
            out.append(get_channel(c, cfg))
        except Exception as e:  # canal não implementado/mal configurado não derruba o servidor
            errors[c] = f"{type(e).__name__}: {e}"[:200]
            log.warning("canal %s indisponível: %s", c, e)
    return out, errors


def create_app(cfg: ProjectConfig, agent=None, channels: list | None = None, start_pollers: bool = False) -> FastAPI:
    """`channels`: nomes (`web`, `telegram`…) ou instâncias de `Channel`; None = os configurados."""
    if agent is None:
        from .engine import Agent

        agent = Agent(cfg)
    lock_agent(agent)
    chans, ch_errors = _load_channels(cfg, channels)
    stop = threading.Event()
    threads: list[threading.Thread] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_pollers:
            for c in chans:
                run = getattr(c, "run_poller", None)
                if callable(run):
                    t = threading.Thread(target=run, args=(agent, stop), name=f"poller-{c.name}", daemon=True)
                    t.start()
                    threads.append(t)
        yield
        stop.set()
        for t in threads:
            t.join(timeout=5)

    app = FastAPI(title=f"transformsite — {cfg.name}", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.agent = agent
    app.state.channels = chans
    app.state.channel_errors = ch_errors
    app.state.stop = stop

    for c in chans:
        attach = getattr(c, "attach", None)
        if callable(attach):
            attach(agent)
        mk = getattr(c, "router", None)
        if callable(mk):
            r = mk(agent)
            if r is not None:
                app.include_router(r)

    @app.get("/health")
    def health() -> dict[str, Any]:
        from .kb.index import Index

        try:
            kb = Index(cfg.kb_path).stats()
        except Exception as e:
            kb = {"error": str(e)[:200]}
        return {
            "ok": True,
            "name": cfg.name,
            "version": __version__,
            "services": sorted(agent.services),
            "service_errors": list(getattr(agent, "load_errors", [])),
            "kb": kb,
            "channels": [getattr(c, "name", type(c).__name__) for c in chans],
            "channel_errors": ch_errors,
            "llm": cfg.llm.provider,
        }

    from .admin import mount_admin

    mount_admin(app, cfg, agent)
    return app


def serve(cfg: ProjectConfig, host: str = "0.0.0.0", port: int = 8000, channels: list[str] | None = None) -> None:
    import uvicorn

    app = create_app(cfg, channels=channels, start_pollers=True)
    st = app.state
    print(f"transformsite {__version__} — {cfg.name}")
    print(f"  canais: {', '.join(c.name for c in st.channels) or 'nenhum'}")
    for n, e in st.channel_errors.items():
        print(f"  [aviso] canal {n} indisponível: {e}")
    for e in getattr(st.agent, "load_errors", []):
        print(f"  [aviso] serviço ignorado: {e}")
    from .admin import admin_token

    if not admin_token(cfg):
        print(f"  [aviso] {cfg.admin_token_env} não definido (ou valor de exemplo): painel /admin só aceita acesso de 127.0.0.1")
    print(f"  painel: http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/admin")
    uvicorn.run(app, host=host, port=port, log_level="info")
