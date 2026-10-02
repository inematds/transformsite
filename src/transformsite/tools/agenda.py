"""Agenda.

- `local` (padrão, zero configuração): horários comerciais + SQLite + convite .ics por e-mail.
- `calcom` (opcional/experimental): usa a API do Cal.com — exige `CALCOM_API_KEY` e autorização.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from ..mailer import send_email
from ..services.types import fmt_datetime, now

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_hours(spec: str) -> dict[int, tuple[int, int]]:
    """'mon-fri 09:00-17:00; sat 09:00-12:00' → {weekday: (min_ini, min_fim)}"""
    out: dict[int, tuple[int, int]] = {}
    for part in spec.split(";"):
        m = re.match(r"\s*(\w{3})(?:-(\w{3}))?\s+(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", part)
        if not m:
            continue
        a = DAYS[m.group(1)]
        b = DAYS[m.group(2)] if m.group(2) else a
        for d in range(a, b + 1):
            out[d] = (int(m.group(3)) * 60 + int(m.group(4)), int(m.group(5)) * 60 + int(m.group(6)))
    return out


def _busy(store) -> set[str]:
    return {r[0] for r in store.db.execute("SELECT start FROM appointments WHERE status='confirmed'")}


def is_free(cfg, store, start: datetime) -> tuple[bool, str]:
    hours = parse_hours(cfg.agenda.business_hours)
    rng = hours.get(start.weekday())
    if not rng:
        return False, "fora dos dias de atendimento"
    mins = start.hour * 60 + start.minute
    if not (rng[0] <= mins and mins + cfg.agenda.slot_minutes <= rng[1]):
        return False, "fora do horário de atendimento"
    if (mins - rng[0]) % cfg.agenda.slot_minutes:
        return False, f"os horários são de {cfg.agenda.slot_minutes} em {cfg.agenda.slot_minutes} minutos"
    if start < now() + timedelta(hours=cfg.agenda.min_lead_hours):
        return False, "precisa ser com mais antecedência"
    if start.strftime("%Y-%m-%dT%H:%M") in _busy(store):
        return False, "esse horário já está ocupado"
    return True, ""


def free_slots(cfg, store, n=3, day: str | None = None, after: str | None = None) -> list[str]:
    hours = parse_hours(cfg.agenda.business_hours)
    busy = _busy(store)
    t0 = now() + timedelta(hours=cfg.agenda.min_lead_hours)
    if after:
        t0 = max(t0, datetime.fromisoformat(after))
    start_day = datetime.fromisoformat(day) if day else t0
    out = []
    for dd in range(cfg.agenda.horizon_days):
        d = (start_day + timedelta(days=dd)).replace(hour=0, minute=0, second=0, microsecond=0)
        if day and dd > 0:
            break
        rng = hours.get(d.weekday())
        if not rng:
            continue
        for m in range(rng[0], rng[1] - cfg.agenda.slot_minutes + 1, cfg.agenda.slot_minutes):
            s = d + timedelta(minutes=m)
            iso = s.strftime("%Y-%m-%dT%H:%M")
            if s >= t0 and iso not in busy:
                out.append(iso)
                if len(out) >= n:
                    return out
    return out


def ics(uid: str, start: datetime, minutes: int, title: str, org: str) -> bytes:
    end = start + timedelta(minutes=minutes)
    f = "%Y%m%dT%H%M%S"
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//transformsite//PT\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}@transformsite\r\nDTSTAMP:{datetime.now(timezone.utc):{f}}Z\r\nDTSTART:{start:{f}}\r\nDTEND:{end:{f}}\r\n"
        f"SUMMARY:{title}\r\nORGANIZER:{org}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


# ---------- ferramentas (provider local) ----------
def slots_livres(ctx, n: int = 3, day: str | None = None, after: str | None = None, **_):
    if ctx.cfg.agenda.provider == "calcom":
        return calcom_slots(ctx, n, day)
    return [{"id": s, "label": fmt_datetime(s)} for s in free_slots(ctx.cfg, ctx.store, n, day, after)]


def validar_horario(ctx, start: str, **_):
    if ctx.cfg.agenda.provider == "calcom":
        return {"ok": True}
    ok, why = is_free(ctx.cfg, ctx.store, datetime.fromisoformat(start))
    return {"ok": ok, "motivo": why}


def criar_evento(ctx, title: str, start: str, notes: str = "", phone: str = "", email: str = "", name: str = "", **_):
    if ctx.cfg.agenda.provider == "calcom":
        return calcom_book(ctx, start, name or title, email, notes)
    st = datetime.fromisoformat(start)
    ok, why = is_free(ctx.cfg, ctx.store, st)
    if not ok:
        raise RuntimeError(f"horário indisponível: {why}")
    aid = "A" + uuid.uuid4().hex[:6].upper()
    end = (st + timedelta(minutes=ctx.cfg.agenda.slot_minutes)).strftime("%Y-%m-%dT%H:%M")
    import json, time

    with ctx.store.lock:
        ctx.store.db.execute(
            "INSERT INTO appointments VALUES(?,?,?,?,?,?,?)",
            (aid, start, end, title, json.dumps({"phone": phone, "email": email, "notes": notes, "session": ctx.session.get("id")}), "confirmed", time.time()),
        )
        ctx.store.db.commit()
    invite = ics(aid, st, ctx.cfg.agenda.slot_minutes, title, ctx.cfg.org)
    sent = []
    if email:
        sent.append(send_email(ctx.cfg, email, f"Confirmado: {title}", f"{title}\n{fmt_datetime(start)}\nProtocolo {aid}", [("convite.ics", invite, "text/calendar")]))
    for t in ctx.cfg.handoff.targets:
        if t.startswith("email:"):
            sent.append(send_email(ctx.cfg, t[6:], f"Novo agendamento {aid}: {title}", f"{fmt_datetime(start)}\nTelefone: {phone}\nE-mail: {email}\nNotas: {notes}", [("convite.ics", invite, "text/calendar")]))
    return {"id": aid, "start": start, "end": end, "emails": len(sent)}


# ---------- Cal.com (experimental; exige CALCOM_API_KEY e autorização de uso da API) ----------
def _calcom_headers():
    key = os.environ.get("CALCOM_API_KEY")
    if not key:
        raise RuntimeError("CALCOM_API_KEY não definido")
    return {"Authorization": f"Bearer {key}", "cal-api-version": "2024-09-04"}


def calcom_slots(ctx, n=3, day=None):
    a = ctx.cfg.agenda
    start = (datetime.fromisoformat(day) if day else now()).date().isoformat()
    end = ((datetime.fromisoformat(day) if day else now()) + timedelta(days=a.horizon_days)).date().isoformat()
    r = httpx.get(f"{a.calcom_url}/slots", params={"eventTypeId": a.calcom_event_type_id, "start": start, "end": end, "timeZone": a.timezone}, headers=_calcom_headers(), timeout=20)
    r.raise_for_status()
    out = []
    for _d, items in sorted((r.json().get("data") or {}).items()):
        for it in items:
            iso = it["start"][:16]
            out.append({"id": iso, "label": fmt_datetime(iso)})
            if len(out) >= n:
                return out
    return out


def calcom_book(ctx, start, name, email, notes):
    a = ctx.cfg.agenda
    body = {"start": start + ":00", "eventTypeId": a.calcom_event_type_id, "attendee": {"name": name, "email": email or "sem-email@example.com", "timeZone": a.timezone}, "metadata": {"notes": notes[:400]}}
    r = httpx.post(f"{a.calcom_url}/bookings", json=body, headers=_calcom_headers(), timeout=20)
    r.raise_for_status()
    d = r.json().get("data", {})
    return {"id": d.get("uid") or d.get("id"), "start": start}


def register(reg, cfg=None):
    reg.add("agenda.slots_livres", slots_livres, description="Lista próximos horários livres")
    reg.add("agenda.validar_horario", validar_horario, description="Valida se um horário está livre")
    reg.add("agenda.criar_evento", criar_evento, side_effects=True, description="Cria o agendamento e envia convite .ics")
