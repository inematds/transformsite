"""Extração de formulários PDF.

1. AcroForm (campos de verdade) via `pypdf`: texto, checkbox → sim/não, radio/combo/lista → enum,
   `/TU` (tooltip) como rótulo, `/Ff` bit 2 = obrigatório, `/MaxLen`.
2. PDF "chapado" (sem AcroForm): extrai o texto e procura linhas tipo `Nome: ______` (confiança baixa).

PDFs escaneados (imagem) não têm texto: ficam sem campos e com TODO (OCR fora do escopo).
"""

from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import IndirectObject

from .html_form import RawField, RawForm, clean_label, humanize

FF_REQUIRED = 1 << 1
FF_RADIO = 1 << 15
FF_PUSHBUTTON = 1 << 16
FF_COMBO = 1 << 17
FF_MULTISELECT = 1 << 21


def _r(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def _states(field) -> list[str]:
    """Estados 'ligados' de um botão (checkbox/radio), lendo /AP /N do campo e dos filhos."""
    out: list[str] = []
    widgets = [field] + [_r(k) for k in (field.get("/Kids") or [])]
    for w in widgets:
        ap = _r(w.get("/AP")) if w.get("/AP") else None
        n = _r(ap.get("/N")) if ap and ap.get("/N") else None
        if n is None:
            continue
        for k in n.keys():
            s = str(k).lstrip("/")
            if s != "Off" and s not in out:
                out.append(s)
    return out


def _iter_fields(fields, prefix: str = ""):
    """Percorre a árvore /Fields → campos terminais (nome completo, dicionário, herdados)."""
    for ref in fields or []:
        f = _r(ref)
        t = f.get("/T")
        name = f"{prefix}.{t}" if prefix and t else (str(t) if t else prefix)
        kids = [_r(k) for k in (f.get("/Kids") or [])]
        named_kids = [k for k in kids if k.get("/T")]
        if named_kids:
            yield from _iter_fields(f.get("/Kids"), name)
        else:
            yield name, f


def _inherited(f, key):
    node = f
    while node is not None:
        if key in node:
            return _r(node[key])
        node = _r(node.get("/Parent")) if node.get("/Parent") else None
    return None


def _field_from_acro(name: str, f) -> RawField | None:
    ft = str(_inherited(f, "/FT") or "")
    ff = int(_inherited(f, "/Ff") or 0)
    tu = f.get("/TU")
    label = clean_label(str(tu)) if tu else humanize(name)
    src = "tooltip" if tu else "name"
    rf = RawField(name=name, kind="text", label=label, label_source=src, required=bool(ff & FF_REQUIRED))
    if f.get("/MaxLen"):
        rf.maxlength = int(f["/MaxLen"])
    if ft == "/Tx":
        rf.kind = "textarea" if ff & (1 << 12) else "text"
        return rf
    if ft == "/Btn":
        if ff & FF_PUSHBUTTON:
            return None
        states = _states(f)
        if ff & FF_RADIO:
            rf.kind = "radio"
            rf.options = [(s, humanize(s)) for s in states]
        else:
            rf.kind = "checkbox"
            rf.options = [(states[0] if states else "Yes", label)]
            rf.todos.append(f"campo '{name}': checkbox vira sim/não — preencher o PDF com o estado '{rf.options[0][0]}'")
        return rf
    if ft == "/Ch":
        rf.kind = "select"
        rf.multiple = bool(ff & FF_MULTISELECT)
        for o in _r(f.get("/Opt")) or []:
            o = _r(o)
            if isinstance(o, (list, tuple)) and len(o) == 2:
                rf.options.append((str(_r(o[0])), clean_label(str(_r(o[1])))))
            else:
                rf.options.append((str(o), clean_label(str(o))))
        if not ff & FF_COMBO:
            rf.todos.append(f"campo '{name}': lista (list box) tratada como escolha única")
        return rf
    rf.todos.append(f"campo '{name}': tipo PDF {ft or '?'} desconhecido — tratado como texto")
    return rf


def _title(reader: PdfReader, path: Path) -> str:
    meta = reader.metadata
    if meta and meta.title:
        return clean_label(str(meta.title))
    try:
        first = (reader.pages[0].extract_text() or "").strip().splitlines()
        if first and len(first[0]) <= 90:
            return clean_label(first[0])
    except Exception:  # noqa: BLE001 — PDF malformado: cai no nome do arquivo
        pass
    return humanize(path.stem)


LINE_FIELD = re.compile(r"([A-Za-zÀ-ÿ][\wÀ-ÿ /().º-]{0,40}?)\s*:\s*(?:_{3,}|\.{5,}|\[\s*\])[_/.\s]*")


def _text_fields(text: str) -> list[RawField]:
    out: list[RawField] = []
    seen: set[str] = set()
    for m in LINE_FIELD.finditer(text):
        label = clean_label(m.group(1))
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append(RawField(name=label, kind="text", label=label, label_source="pdf_text", required=True))
    return out


def extract_pdf(path: Path) -> RawForm:
    reader = PdfReader(str(path))
    title = _title(reader, path)
    acro = _r(reader.trailer["/Root"].get("/AcroForm")) if reader.trailer["/Root"].get("/AcroForm") else None
    fields = _r(acro.get("/Fields")) if acro and acro.get("/Fields") else None
    if fields:
        rf = RawForm(kind="pdf_acroform", origin=str(path), title=title, method="email")
        for name, f in _iter_fields(fields):
            fld = _field_from_acro(name, f)
            if fld:
                rf.fields.append(fld)
        rf.todos.append("geração do PDF preenchido não implementada — hoje os dados vão por e-mail em texto (`email.enviar`)")
        return rf
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    rf = RawForm(kind="pdf_text", origin=str(path), title=title, method="email")
    rf.fields = _text_fields(text)
    rf.todos.append("PDF sem AcroForm: campos deduzidos do texto ('Rótulo: ____') — confiança baixa, revisar tipos e obrigatoriedade")
    if not text.strip():
        rf.todos.append("PDF sem texto (provavelmente escaneado) — OCR não implementado; descrever os campos à mão")
    rf.todos.append("geração do PDF preenchido não implementada — hoje os dados vão por e-mail em texto (`email.enviar`)")
    return rf
