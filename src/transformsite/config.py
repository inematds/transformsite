"""Carrega `transformsite.yaml` (o arquivo de configuração de um projeto)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

CONFIG_NAME = "transformsite.yaml"


class LLMConfig(BaseModel):
    # ollama | openai_compat | claude_cli | codex_cli | fake
    provider: str = "ollama"
    model: str = "qwen3.6:35b-a3b"
    base_url: str = "http://localhost:11434"
    embed_provider: str | None = None  # default: igual a provider (claude/codex → none)
    embed_model: str = "bge-m3"
    temperature: float = 0.1
    timeout: float = 180.0
    allow_pii: bool = True  # local: ok. Provider externo deve ser false salvo autorização.


class KBConfig(BaseModel):
    urls: list[str] = Field(default_factory=list)
    sitemaps: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)  # regex de URL a incluir
    exclude: list[str] = Field(default_factory=list)  # regex de URL a excluir
    files: list[str] = Field(default_factory=list)  # docs locais (md, txt, pdf, html)
    max_pages: int = 300
    chunk_words: int = 220
    overlap_words: int = 40
    top_k: int = 6
    min_score: float = 0.0


class AgendaConfig(BaseModel):
    provider: str = "local"  # local | calcom
    timezone: str = "America/Sao_Paulo"
    business_hours: str = "mon-fri 09:00-17:00"
    slot_minutes: int = 30
    min_lead_hours: float = 2
    horizon_days: int = 21
    calcom_url: str = "https://api.cal.com/v2"
    calcom_event_type_id: int | None = None


class HandoffConfig(BaseModel):
    # destinos: "telegram:<chat_id>", "email:<addr>", "panel"
    targets: list[str] = Field(default_factory=lambda: ["panel"])
    triggers: list[str] = Field(
        default_factory=lambda: ["falar com humano", "atendente", "falar com uma pessoa", "pessoa de verdade", "humano"]
    )
    hours: str = "mon-fri 09:00-18:00"


class RetentionConfig(BaseModel):
    transcripts_days: int = 90
    audit_months: int = 12


class PrivacyConfig(BaseModel):
    controller: str = ""
    dpo_contact: str = ""
    notice: str = ""


class EmailConfig(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password_env: str = "SMTP_PASSWORD"
    smtp_from: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password_env: str = "IMAP_PASSWORD"
    outbox_dir: str = "data/outbox"  # sem SMTP configurado, e-mails vão para cá (.eml)


class ChannelsConfig(BaseModel):
    telegram: dict[str, Any] = Field(default_factory=dict)  # token_env, api_base
    whatsapp: dict[str, Any] = Field(default_factory=dict)  # Evolution: base_url, instance, apikey_env
    whatsapp_cloud: dict[str, Any] = Field(default_factory=dict)  # Cloud API: phone_number_id, token_env, verify_token
    email: dict[str, Any] = Field(default_factory=dict)
    web: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})


class ProjectConfig(BaseModel):
    name: str = "meu-bot"
    org: str = "Minha Empresa"
    language: str = "pt-BR"
    welcome: str = ""
    fallback_contact: str = ""  # contato humano tradicional (e-mail/telefone) sempre visível
    llm: LLMConfig = Field(default_factory=LLMConfig)
    kb: KBConfig = Field(default_factory=KBConfig)
    agenda: AgendaConfig = Field(default_factory=AgendaConfig)
    handoff: HandoffConfig = Field(default_factory=HandoffConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    admin_token_env: str = "ADMIN_TOKEN"

    # preenchido em load()
    root: Path = Path(".")

    model_config = {"arbitrary_types_allowed": True}

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def kb_path(self) -> Path:
        return self.root / "kb" / "index.sqlite"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.sqlite"

    @property
    def services_dir(self) -> Path:
        return self.root / "services"

    @property
    def tools_dir(self) -> Path:
        return self.root / "tools"


def _expand_env(obj: Any) -> Any:
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def load_dotenv(path: Path) -> None:
    """Carrega .env simples (KEY=VALUE) sem sobrescrever variáveis já definidas."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def find_root(start: Path | None = None) -> Path | None:
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / CONFIG_NAME).exists():
            return d
    return None


def load(root: Path | str | None = None) -> ProjectConfig:
    r = Path(root) if root else find_root()
    if r is None:
        raise SystemExit(f"{CONFIG_NAME} não encontrado. Rode `transformsite init <pasta>` primeiro.")
    r = r.resolve()
    load_dotenv(r / ".env")
    raw = yaml.safe_load((r / CONFIG_NAME).read_text(encoding="utf-8")) or {}
    cfg = ProjectConfig(**_expand_env(raw))
    cfg.root = r
    # overrides por ambiente (úteis em Docker/VPS)
    if os.environ.get("TRANSFORMSITE_LLM"):
        cfg.llm.provider = os.environ["TRANSFORMSITE_LLM"]
    if os.environ.get("TRANSFORMSITE_LLM_BASE_URL"):
        cfg.llm.base_url = os.environ["TRANSFORMSITE_LLM_BASE_URL"]
    if os.environ.get("LLM_MODEL"):
        cfg.llm.model = os.environ["LLM_MODEL"]
    if os.environ.get("EMBED_MODEL"):
        cfg.llm.embed_model = os.environ["EMBED_MODEL"]
    return cfg
