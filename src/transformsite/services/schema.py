"""Schema `service/1`: um serviço = um arquivo YAML declarativo."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .types import EXTRACTORS

SLOT_TYPES = set(EXTRACTORS)


class Intent(BaseModel):
    description: str
    examples: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class Slot(BaseModel):
    name: str
    type: str = "text"
    prompt: str
    required: bool = True
    values: list[str] | None = None
    labels: dict[str, str] | None = None  # rótulo amigável por valor de enum
    synonyms: dict[str, list[str]] | None = None
    validate_regex: str | None = Field(default=None, alias="validate")
    max_len: int | None = None
    sensitive: bool = False
    when: str | None = None  # ex.: "{motivo} == outro"
    prefill_from: str | None = None  # channel.user_phone | channel.user_email | channel.user_name
    options_from: str | None = None  # tool:agenda.slots_livres
    constraints: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None  # preenchido pelo conversor

    model_config = {"populate_by_name": True}

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v):
        if v not in SLOT_TYPES:
            raise ValueError(f"tipo de slot desconhecido: {v} (use: {', '.join(sorted(SLOT_TYPES))})")
        return v

    @model_validator(mode="after")
    def _enum_values(self):
        if self.type == "enum" and not self.values and not self.options_from:
            raise ValueError(f"slot {self.name}: enum precisa de `values` ou `options_from`")
        return self


class Confirm(BaseModel):
    template: str


class Action(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class Outcome(BaseModel):
    reply: str = ""
    notify: list[str] = Field(default_factory=list)
    handoff: dict[str, Any] | None = None
    attach: str | None = None


class Handoff(BaseModel):
    triggers: list[str] = Field(default_factory=list)
    queue: str = "atendimento"


class Service(BaseModel):
    service: str
    version: int = 1
    review: Literal["pending", "approved"] = "pending"
    intent: Intent
    auth: str | dict[str, Any] = "none"
    legal_basis: str = "legitimo_interesse"
    slots: list[Slot] = Field(default_factory=list)
    confirm: Confirm | None = None
    action: Action | None = None
    on_success: Outcome = Field(default_factory=Outcome)
    on_failure: Outcome = Field(default_factory=lambda: Outcome(reply="Não consegui concluir agora. Vou passar para a equipe."))
    handoff: Handoff = Field(default_factory=Handoff)
    knowledge: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)  # de onde veio (conversor)

    @field_validator("service")
    @classmethod
    def _id(cls, v):
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,60}", v):
            raise ValueError("service deve ser snake_case (a-z, 0-9, _)")
        return v

    @property
    def auth_method(self) -> str:
        return self.auth if isinstance(self.auth, str) else self.auth.get("method", "none")

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.slots if s.name == name), None)


class ServiceError(Exception):
    pass


def load_service(path: Path) -> Service:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ServiceError(f"{path.name}: YAML inválido: {e}") from e
    if not isinstance(raw, dict):
        raise ServiceError(f"{path.name}: esperado um mapa YAML")
    try:
        return Service(**raw)
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        raise ServiceError(f"{path.name}: {msgs}") from e


def lint_service(svc: Service, tools: dict | None = None) -> list[str]:
    """Regras além do schema. Retorna lista de erros (vazia = ok)."""
    errs = []
    names = [s.name for s in svc.slots]
    if len(set(names)) != len(names):
        errs.append("slots com nome repetido")
    refs = set(re.findall(r"\{([a-z_][\w]*)", yaml.safe_dump(svc.model_dump(by_alias=True), allow_unicode=True)))
    allowed = set(names) | {"session_id", "today", "result", "channel", "user_id"}
    for r in refs - allowed:
        errs.append(f"template usa {{{r}}} que não é slot")
    if svc.action:
        spec = (tools or {}).get(svc.action.tool)
        if tools is not None and spec is None:
            errs.append(f"ferramenta desconhecida: {svc.action.tool}")
        side = spec.side_effects if spec else True
        if side and not svc.confirm:
            errs.append("ação com efeito exige `confirm`")
        if side and not svc.action.idempotency_key:
            errs.append("ação com efeito exige `action.idempotency_key`")
    if any(s.sensitive for s in svc.slots) and svc.auth_method == "none":
        errs.append("slot sensível exige `auth` diferente de none")
    for s in svc.slots:
        if s.options_from and not s.options_from.startswith("tool:"):
            errs.append(f"slot {s.name}: options_from deve começar com tool:")
        if s.when and not re.fullmatch(r"\{\w+\}\s*(==|!=)\s*\S+", s.when.strip()):
            errs.append(f"slot {s.name}: `when` deve ser '{{slot}} == valor'")
    return errs


def load_services(directory: Path, only_approved=True, tools: dict | None = None) -> tuple[dict[str, Service], list[str]]:
    services: dict[str, Service] = {}
    errors: list[str] = []
    if not directory.exists():
        return services, errors
    for p in sorted(directory.glob("*.y*ml")):
        try:
            svc = load_service(p)
        except ServiceError as e:
            errors.append(str(e))
            continue
        lint = lint_service(svc, tools)
        if lint:
            errors.extend(f"{p.name}: {m}" for m in lint)
            continue
        if only_approved and svc.review != "approved":
            continue
        services[svc.service] = svc
    return services, errors


# ---------- templates "{slot|filtro}" ----------
def render(template: Any, ctx: dict) -> Any:
    from .types import fmt_date, fmt_datetime

    filters = {
        "fmt_br": lambda v: fmt_datetime(v) if "T" in str(v) else fmt_date(v),
        "last4": lambda v: str(v)[-4:] if v else "",
        "trunc80": lambda v: (str(v)[:80] + "…") if len(str(v)) > 80 else str(v),
        "upper": lambda v: str(v).upper(),
        "label": lambda v: str(v).replace("_", " "),
    }
    if isinstance(template, dict):
        return {k: render(v, ctx) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, ctx) for v in template]
    if not isinstance(template, str):
        return template

    def sub(m):
        expr = m.group(1)
        name, *flt = [x.strip() for x in expr.split("|")]
        val: Any = ctx
        for part in name.split("."):
            val = val.get(part, "") if isinstance(val, dict) else getattr(val, part, "")
        for f in flt:
            val = filters.get(f, str)(val)
        return "" if val is None else str(val)

    return re.sub(r"\{([\w.]+(?:\s*\|\s*\w+)*)\}", sub, template)
