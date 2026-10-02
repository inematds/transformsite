"""Fase 4 — conversor de formulários legados (HTML, PDF, URL) → `service/1` YAML revisável.

Determinístico primeiro (DOM / AcroForm), LLM depois (opcional), humano por último:
todo YAML nasce `review: pending` e o `serve` não o carrega até alguém aprovar.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..services.schema import Service, lint_service
from .build import build_service, refine_with_llm, snake, write_service
from .html_form import RawForm, extract_html
from .pdf_form import extract_pdf

__all__ = ["convert_file", "extract_forms"]


class ConvertError(RuntimeError):
    pass


def _fetch(url: str) -> tuple[str, str, bytes, str]:
    """(url_final, content_type, bytes, texto)."""
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "transformsite-convert/1"})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise ConvertError(f"não consegui baixar {url}: {e}") from e
    return str(r.url), r.headers.get("content-type", ""), r.content, r.text


def extract_forms(src: str | Path) -> tuple[list[RawForm], str]:
    """Formulários brutos + nome-base (para o id do serviço quando não houver título)."""
    s = str(src)
    if re.match(r"https?://", s):
        final, ctype, content, text = _fetch(s)
        stem = snake(Path(final.split("?")[0].rstrip("/")).stem or "formulario")
        if "pdf" in ctype or final.lower().endswith(".pdf"):
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
                tmp.write(content)
                tmp.flush()
                rf = extract_pdf(Path(tmp.name))
            rf.origin = s
            return [rf], stem
        forms, _ = extract_html(text, origin=s, base_url=final)
        return forms, stem
    p = Path(s)
    if not p.exists():
        raise ConvertError(f"arquivo não encontrado: {p}")
    if p.suffix.lower() == ".pdf":
        return [extract_pdf(p)], p.stem
    if p.suffix.lower() in (".html", ".htm"):
        forms, _ = extract_html(p.read_text(encoding="utf-8", errors="replace"), origin=str(p))
        return forms, p.stem
    raise ConvertError(f"formato não suportado: {p.suffix} (use .html, .htm, .pdf ou URL)")


def _check(svc_dict: dict, tools) -> list[str]:
    svc = Service(**svc_dict)
    return lint_service(svc, tools)


def _existing_services(out_dir: Path) -> dict[str, tuple[str, Path]]:
    """id do serviço → (review, arquivo) dos YAML já presentes no destino."""
    out: dict[str, tuple[str, Path]] = {}
    if not out_dir.exists():
        return out
    for p in sorted(out_dir.glob("*.y*ml")):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("service"), str):
            out[raw["service"]] = (str(raw.get("review", "pending")), p)
    return out


def _blocked(sid: str, existing: dict[str, tuple[str, Path]], out_dir: Path) -> bool:
    """Não sobrescreve serviço aprovado nem colide com id usado por outro arquivo.

    Reconverter por cima de um `pending` gerado antes (mesmo arquivo) é permitido.
    """
    if sid not in existing:
        return False
    review, path = existing[sid]
    return review == "approved" or path.resolve() != (out_dir / f"{sid}.yaml").resolve()


def _rename(svc: dict, new: str) -> None:
    old = svc["service"]
    svc["service"] = new
    act = svc.get("action") or {}
    if act.get("idempotency_key"):
        act["idempotency_key"] = act["idempotency_key"].replace(f":{old}", f":{new}")
    if isinstance(act.get("args", {}).get("subject"), str):
        act["args"]["subject"] = act["args"]["subject"].replace(f"[{old}]", f"[{new}]")


def convert_file(src: str | Path, out_dir: Path, cfg=None, use_llm: bool = True) -> dict[str, Any]:
    """Converte um formulário legado em `service.yaml` (um por formulário relevante).

    Retorna `{"path": str | list[str], "slots": int, "todos": int, "services": [...]}`.
    """
    from ..tools import default_registry

    out_dir = Path(out_dir)
    forms, stem = extract_forms(src)
    if not forms:
        raise ConvertError(f"nenhum formulário relevante em {src} (busca/login/newsletter de 1 campo são ignorados)")
    tools = default_registry()
    llm_on = bool(use_llm and cfg is not None and getattr(cfg.llm, "provider", "fake") != "fake")
    results: list[dict] = []
    used: set[str] = set()
    existing = _existing_services(out_dir)
    for i, rf in enumerate(forms):
        fallback = stem if len(forms) == 1 else f"{stem}_{i + 1}"
        svc = build_service(rf, fallback, cfg)
        base, n = svc["service"], 2
        while svc["service"] in used or _blocked(svc["service"], existing, out_dir):
            new = f"{base}_{n}"
            if svc["service"] == base and base in existing:
                svc["source"]["todos"].append(f"já existia o serviço '{base}' em {existing[base][1].name} — gerado como '{new}' para não sobrescrever")
            _rename(svc, new)
            n += 1
        used.add(svc["service"])
        if llm_on:
            notes = refine_with_llm(svc, cfg)
            svc["source"]["llm_notes"] = notes
        svc["review"] = "pending"  # nunca vem aprovado
        errs = _check(svc, tools)
        if errs:
            raise ConvertError(f"serviço gerado não passou no lint ({svc['service']}): {'; '.join(errs)}")
        path = write_service(svc, out_dir)
        results.append({"path": str(path), "service": svc["service"], "slots": len(svc["slots"]),
                        "todos": len(svc["source"]["todos"])})
    return {
        "path": results[0]["path"] if len(results) == 1 else [r["path"] for r in results],
        "slots": sum(r["slots"] for r in results),
        "todos": sum(r["todos"] for r in results),
        "services": results,
    }
