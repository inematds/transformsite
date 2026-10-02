"""Tipos de slot: extração + normalização + validação determinísticas (sem LLM).

Cada tipo expõe `extract(texto, slot) -> valor | None` e `fmt(valor) -> str`.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Callable


def _n(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# relógio injetável (testes fixam a data)
_NOW: Callable[[], datetime] = datetime.now


def set_clock(fn: Callable[[], datetime]) -> None:
    global _NOW
    _NOW = fn


def now() -> datetime:
    return _NOW()


# ---------- validadores ----------
def cpf_valid(d: str) -> bool:
    if len(d) != 11 or d == d[0] * 11:
        return False
    for i in (9, 10):
        s = sum(int(d[j]) * ((i + 1) - j) for j in range(i))
        if (s * 10 % 11) % 10 != int(d[i]):
            return False
    return True


def ex_cpf(text, slot):
    for m in re.finditer(r"\b(\d{3}\.?\d{3}\.?\d{3}-?\d{2})\b", text):
        d = re.sub(r"\D", "", m.group(1))
        if cpf_valid(d):
            return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return None


def ex_email(text, slot):
    m = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", text)
    return m.group(0).lower() if m else None


def ex_phone(text, slot):
    m = re.search(r"(?:\+?55[\s-]?)?\(?(\d{2})\)?[\s-]?(9?\d{4})[\s-]?(\d{4})\b", text)
    if not m:
        return None
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"


def ex_cep(text, slot):
    m = re.search(r"\b(\d{5})-?(\d{3})\b", text)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def ex_number(text, slot):
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    return float(m.group(0).replace(",", ".")) if m else None


YES = {"sim", "s", "isso", "confirmo", "confirma", "correto", "certo", "ok", "pode", "claro", "yes", "exato", "positivo", "perfeito", "1"}
NO = {"nao", "n", "errado", "negativo", "no", "nope", "2"}


def ex_yes_no(text, slot):
    t = _n(text).strip(" .!?")
    words = re.findall(r"\w+", t)
    if not words:
        return None
    if words[0] in NO or t.startswith("nao"):
        return False
    if words[0] in YES or t in ("pode ser", "isso mesmo", "esta certo", "ta certo", "tudo certo"):
        return True
    return None


def ex_enum(text, slot):
    values = slot.values or []
    t = _n(text)
    tw = set(re.findall(r"\w+", t))
    # número da opção ("2")
    m = re.fullmatch(r"\s*(\d{1,2})\s*[).]?\s*", t)
    if m and 1 <= int(m.group(1)) <= len(values):
        return values[int(m.group(1)) - 1]
    best = None
    for v in values:
        label = _n(str(v)).replace("_", " ")
        syns = [label] + [_n(s) for s in (slot.synonyms or {}).get(str(v), [])]
        for s in syns:
            sw = set(re.findall(r"\w+", s))
            if s and (re.search(rf"\b{re.escape(s)}\b", t) or (sw and sw <= tw)):
                if best is None or len(s) > best[1]:
                    best = (v, len(s))
    return best[0] if best else None


def ex_text(text, slot):
    t = text.strip()
    return t if t else None


def ex_name(text, slot):
    m = re.search(
        r"(?:meu nome (?:é|e)|me chamo|sou (?:o|a)|aqui (?:é|e) (?:o|a)?|nome:)\s+([A-ZÀ-Ú][\wÀ-ú']+(?:\s+(?:d[aeo]s?\s+)?[A-ZÀ-Ú][\wÀ-ú']+){0,4})",
        text,
        flags=re.I,
    )
    if m:
        return m.group(1).strip().title()
    return None


# ---------- datas em PT-BR ----------
WEEKDAYS = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6}
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}


def parse_date(text: str, base: datetime | None = None) -> date | None:
    base = base or now()
    t = _n(text)
    if re.search(r"\bdepois de amanha\b", t):
        return (base + timedelta(days=2)).date()
    if re.search(r"\bamanha\b", t):
        return (base + timedelta(days=1)).date()
    if re.search(r"\bhoje\b", t):
        return base.date()
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else base.year
        if y < 100:
            y += 2000
        try:
            dt = date(y, mo, d)
        except ValueError:
            return None
        if not m.group(3) and dt < base.date():
            dt = date(y + 1, mo, d)
        return dt
    m = re.search(r"\b(?:dia\s+)?(\d{1,2})\s+de\s+(" + "|".join(MONTHS) + r")\b", t)
    if m:
        try:
            dt = date(base.year, MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
        return dt if dt >= base.date() else dt.replace(year=base.year + 1)
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}(?:-feira| feira)?\b", t):
            delta = (wd - base.weekday()) % 7
            if delta == 0 and re.search(r"\bque vem\b|\bproxim", t):
                delta = 7
            return (base + timedelta(days=delta)).date()
    m = re.search(r"\bdia\s+(\d{1,2})\b", t)
    if m:
        d = int(m.group(1))
        try:
            dt = date(base.year, base.month, d)
        except ValueError:
            return None
        if dt < base.date():
            mo = base.month % 12 + 1
            dt = date(base.year + (1 if mo == 1 else 0), mo, d)
        return dt
    return None


def parse_time(text: str) -> tuple[int, int] | None:
    t = _n(text)
    m = re.search(r"\b(\d{1,2})\s*(?:h|:|horas?)\s*(\d{2})?\b", t)
    if not m:
        m = re.search(r"\bas\s+(\d{1,2})(?::(\d{2}))?\b", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if re.search(r"\bda tarde\b|\bda noite\b", t) and h < 12:
        h += 12
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


def ex_date(text, slot):
    d = parse_date(text)
    return d.isoformat() if d else None


def ex_datetime(text, slot):
    # escolha por número de opção já resolvida pelo motor; aqui só texto livre
    m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)
    if m:
        return m.group(0)
    d = parse_date(text)
    tm = parse_time(text)
    if d and tm:
        return datetime(d.year, d.month, d.day, tm[0], tm[1]).strftime("%Y-%m-%dT%H:%M")
    return None


def partial_datetime(text: str) -> dict:
    """Para dar feedback útil: o que foi entendido (data e/ou hora)."""
    d = parse_date(text)
    tm = parse_time(text)
    return {"date": d.isoformat() if d else None, "time": f"{tm[0]:02d}:{tm[1]:02d}" if tm else None}


DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def fmt_datetime(v: str) -> str:
    try:
        dt = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{DIAS[dt.weekday()]}, {dt:%d/%m} às {dt:%H:%M}"


def fmt_date(v: str) -> str:
    try:
        d = date.fromisoformat(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{DIAS[d.weekday()]}, {d:%d/%m/%Y}"


EXTRACTORS: dict[str, Callable[[str, Any], Any]] = {
    "text": ex_text,
    "name": ex_text,
    "enum": ex_enum,
    "email": ex_email,
    "phone_br": ex_phone,
    "cpf": ex_cpf,
    "cep": ex_cep,
    "date": ex_date,
    "datetime": ex_datetime,
    "number": ex_number,
    "yes_no": ex_yes_no,
}

# tipos que podem ser achados "de passagem" numa mensagem que responde outro slot
OPPORTUNISTIC = {"email", "phone_br", "cpf", "cep", "datetime", "date"}

FORMATTERS: dict[str, Callable[[Any], str]] = {
    "datetime": fmt_datetime,
    "date": fmt_date,
    "yes_no": lambda v: "sim" if v else "não",
}


def fmt(type_: str, v: Any) -> str:
    return FORMATTERS.get(type_, str)(v)
