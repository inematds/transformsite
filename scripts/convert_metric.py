"""Evidência da Fase 4: roda o conversor em todas as fixtures e compara com o gabarito manual.

Métrica = slots com tipo E obrigatoriedade corretos / slots do gabarito (slot faltante = erro;
slot extra não conta). Critério de pronto: ≥ 0.80.

Uso: `.venv/bin/python scripts/convert_metric.py [--out DIR]`
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from transformsite.convert import convert_file  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "forms"


def score_form(src: Path, out_dir: Path) -> dict:
    """Converte uma fixture e compara com `<nome>.expected.yaml`."""
    exp = yaml.safe_load((FIXTURES / f"{src.stem}.expected.yaml").read_text(encoding="utf-8"))
    res = convert_file(src, out_dir, cfg=None, use_llm=False)
    svc = yaml.safe_load(Path(res["path"]).read_text(encoding="utf-8"))
    fmap = svc["source"]["field_map"]
    slots = {s["name"]: s for s in svc["slots"]}
    ok, misses = 0, []
    for e in exp["slots"]:
        s = slots.get(fmap.get(e["name_original"], ""))
        if s is None:
            misses.append(f"{e['name_original']}: ausente")
            continue
        good_t = s["type"] == e["type"]
        good_r = bool(s.get("required", True)) == bool(e["required"])
        if good_t and good_r:
            ok += 1
        else:
            misses.append(f"{e['name_original']}: {s['type']}/{s.get('required')} ≠ {e['type']}/{e['required']}")
    return {"form": src.name, "service": svc["service"], "expected": len(exp["slots"]), "ok": ok,
            "extracted": len(svc["slots"]), "todos": res["todos"], "misses": misses, "path": res["path"]}


def fixtures() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if p.suffix in (".html", ".pdf") and (FIXTURES / f"{p.stem}.expected.yaml").exists())


def run(out_dir: Path) -> tuple[list[dict], float]:
    rows = [score_form(p, out_dir) for p in fixtures()]
    total = sum(r["expected"] for r in rows)
    ok = sum(r["ok"] for r in rows)
    return rows, (ok / total if total else 0.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="pasta para os YAML gerados (padrão: temporária)")
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as td:
        out = Path(args.out) if args.out else Path(td)
        rows, metric = run(out)
    w = max(len(r["form"]) for r in rows)
    print(f"{'formulário':<{w}}  {'serviço':<34} {'ok/esp':>7} {'extr':>5} {'TODO':>5}")
    for r in rows:
        print(f"{r['form']:<{w}}  {r['service']:<34} {r['ok']:>3}/{r['expected']:<3} {r['extracted']:>5} {r['todos']:>5}")
        for m in r["misses"]:
            print(f"{'':<{w}}    ✗ {m}")
    ok = sum(r["ok"] for r in rows)
    tot = sum(r["expected"] for r in rows)
    print(f"\nformulários: {len(rows)} · slots corretos (tipo E obrigatoriedade): {ok}/{tot} = {metric:.1%}")
    print("critério ≥ 80%:", "OK" if metric >= 0.8 else "FALHOU")
    return 0 if metric >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
