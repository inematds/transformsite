"""Contrato único de mensagem entre canais e motor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class InMsg:
    channel: str
    user_id: str
    text: str
    msg_id: str | None = None  # id do canal — usado para descartar entregas duplicadas
    choice_id: str | None = None  # botão clicado
    meta: dict[str, Any] = field(default_factory=dict)  # phone, email, name, lang
    attachments: list[dict] = field(default_factory=list)


@dataclass
class OutMsg:
    text: str
    choices: list[dict] = field(default_factory=list)  # [{"id": "...", "label": "..."}]
    citations: list[dict] = field(default_factory=list)  # [{"n": 1, "url": "...", "title": "..."}]
    attachments: list[dict] = field(default_factory=list)
    kind: str = "reply"  # reply | answer | nao_sei | handoff | service | error

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Capabilities:
    buttons: int = 0  # máximo de botões inline
    lists: int = 0  # máximo de itens em lista nativa
    markdown: bool = False
    files: bool = False


def render_text(out: OutMsg, caps: Capabilities, cite_style: str = "plain") -> tuple[str, list[dict], str]:
    """Degradação por canal. Retorna (texto, choices_nativos, modo) com modo em buttons|list|numbered|none."""
    text = out.text
    if out.citations:
        refs = "\n".join(f"[{c['n']}] {c['url']}" for c in out.citations)
        text = f"{text}\n\nFontes:\n{refs}"
    if not out.choices:
        return text, [], "none"
    n = len(out.choices)
    if caps.buttons and n <= caps.buttons:
        return text, out.choices, "buttons"
    if caps.lists and n <= caps.lists:
        return text, out.choices, "list"
    numbered = "\n".join(f"{i}. {c['label']}" for i, c in enumerate(out.choices, 1))
    return f"{text}\n\n{numbered}\n(responda com o número)", [], "numbered"
