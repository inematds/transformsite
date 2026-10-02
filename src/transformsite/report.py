"""Métricas (PLANO §8) a partir de events/tickets/sessions do Store. Usado pelo CLI e pelo painel."""

from __future__ import annotations

import csv
import io
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import ProjectConfig
from .store import Store

COLS = [
    "dia", "conversas", "resolvidas_sem_humano", "taxa_resolucao", "handoffs", "taxa_handoff", "nao_sei",
    "respostas", "respostas_com_citacao", "taxa_citacao", "servicos_iniciados", "servicos_concluidos",
    "servicos_cancelados", "mediana_conclusao_s", "latencia_p50_ms", "latencia_p95_ms", "reportadas_erradas",
]


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = max(0, math.ceil(p / 100 * len(v)) - 1)  # nearest-rank
    return v[k]


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def _metrics(evs: list[dict], erradas: int) -> dict:
    """Métricas de um conjunto de eventos (um dia ou a janela inteira)."""
    by_sess: dict[str, set[str]] = defaultdict(set)
    for e in evs:
        if e["session_id"]:
            by_sess[e["session_id"]].add(e["kind"])
    ativas = {s for s, k in by_sess.items() if "turn" in k}
    resolvidas = {s for s in ativas if by_sess[s] & {"answer", "service_completed"} and not by_sess[s] & {"handoff", "takeover"}}
    humano = {s for s in ativas if by_sess[s] & {"handoff", "takeover"}}
    kinds = Counter(e["kind"] for e in evs)
    answers = [e for e in evs if e["kind"] == "answer"]
    cited = sum(1 for e in answers if (e["data"].get("cited") or 0) > 0)
    ms = [float(e["data"]["ms"]) for e in evs if e["kind"] == "turn" and "ms" in e["data"]]
    secs = [float(e["data"]["seconds"]) for e in evs if e["kind"] == "service_completed" and "seconds" in e["data"]]
    return {
        "conversas": len(ativas),
        "resolvidas_sem_humano": len(resolvidas),
        "taxa_resolucao": _rate(len(resolvidas), len(ativas)),
        "handoffs": len(humano),
        "taxa_handoff": _rate(len(humano), len(ativas)),
        "nao_sei": kinds["nao_sei"],
        "respostas": len(answers),
        "respostas_com_citacao": cited,
        "taxa_citacao": _rate(cited, len(answers)),
        "servicos_iniciados": kinds["service_started"],
        "servicos_concluidos": kinds["service_completed"],
        "servicos_cancelados": kinds["service_cancelled"],
        "mediana_conclusao_s": round(statistics.median(secs), 1) if secs else None,
        "latencia_p50_ms": _pct(ms, 50),
        "latencia_p95_ms": _pct(ms, 95),
        "reportadas_erradas": erradas,
    }


def _abandono(evs: list[dict], store: Store) -> dict[str, int]:
    """Slot onde parou cada serviço iniciado e não concluído (chave 'servico:slot')."""
    runs: dict[tuple[str, str], dict] = {}
    for e in evs:  # em ordem de id
        sid, d = e["session_id"], e["data"]
        svc = d.get("service")
        if not sid or not svc:
            continue
        key = (sid, svc)
        if e["kind"] == "service_started":
            runs[key] = {"done": False, "slot": None}
        elif key in runs:
            r = runs[key]
            if e["kind"] == "service_completed":
                r["done"] = True
            elif e["kind"] in ("slot_retry", "slot_invalid") and d.get("slot"):
                r["slot"] = d["slot"]
            elif e["kind"] == "service_cancelled":
                r["slot"] = d.get("slot") or r["slot"]
                r["cancelled"] = True
    out: Counter = Counter()
    for (sid, svc), r in runs.items():
        if r["done"]:
            continue
        slot = r["slot"]
        if not r.get("cancelled"):
            sess = store.get_session(sid)
            st = (sess or {}).get("state", {})
            if st.get("service") == svc:
                slot = st.get("current") or ("confirmacao" if st.get("phase") == "confirm" else slot)
        out[f"{svc}:{slot or '?'}"] += 1
    return dict(out.most_common())


def report(cfg: ProjectConfig, since_days: int = 7, out: Path | str | None = None, store: Store | None = None) -> dict:
    st = store or Store(cfg.db_path)
    since_days = max(1, int(since_days))
    first = date.today() - timedelta(days=since_days - 1)
    since = time.mktime(first.timetuple())
    evs = st.events(since=since)
    erradas = [t for t in st.tickets(limit=100000) if t["kind"] == "erro" and t["created_at"] >= since]

    ev_day: dict[str, list[dict]] = defaultdict(list)
    for e in evs:
        ev_day[_day(e["ts"])].append(e)
    err_day = Counter(_day(t["created_at"]) for t in erradas)
    dias = []
    for i in range(since_days):
        d = (first + timedelta(days=i)).isoformat()
        dias.append({"dia": d, **_metrics(ev_day.get(d, []), err_day.get(d, 0))})

    por_servico: dict[str, Counter] = defaultdict(Counter)
    for e in evs:
        svc = e["data"].get("service")
        if svc and e["kind"] in ("service_started", "service_completed", "service_cancelled", "service_failed"):
            por_servico[svc][e["kind"].split("_", 1)[1]] += 1

    res = {
        "desde": first.isoformat(),
        "dias_janela": since_days,
        "resumo": _metrics(evs, len(erradas)),
        "por_servico": {k: dict(v) for k, v in sorted(por_servico.items())},
        "abandono_por_slot": _abandono(evs, st),
        "por_dia": dias,
    }
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(to_csv(res), encoding="utf-8")
        res["csv"] = str(p)
    return res


def to_csv(res: dict) -> str:
    """Uma linha por dia + linha `total` (resumo da janela)."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for d in res["por_dia"]:
        w.writerow({k: ("" if d.get(k) is None else d.get(k)) for k in COLS})
    tot = {"dia": "total", **res["resumo"]}
    w.writerow({k: ("" if tot.get(k) is None else tot.get(k)) for k in COLS})
    return buf.getvalue()
