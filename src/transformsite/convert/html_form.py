"""Extração determinística de `<form>` HTML → campos brutos (`RawField`).

Nada de LLM aqui: só DOM. A inferência de tipo e a montagem do serviço ficam em `build.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

SKIP_INPUT_TYPES = {"submit", "button", "reset", "image"}
CSRF_RE = re.compile(r"csrf|token|nonce|authenticity|__requestverification|captcha", re.I)
COND_ATTRS = ("data-show-if", "data-depends-on", "data-visible-if", "data-condition")
JS_COND_ATTRS = ("onchange", "oninput", "onclick")


@dataclass
class RawField:
    name: str  # nome ORIGINAL do campo (o que o legado recebe)
    kind: str  # text, email, tel, date, datetime-local, number, select, radio, checkbox, textarea…
    label: str = ""
    label_source: str = "name"  # label | aria | placeholder | title | prev_text | legend | tooltip | name
    required: bool = False
    pattern: str | None = None
    maxlength: int | None = None
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, texto)
    group: str | None = None  # legend do fieldset
    when: tuple[str, str, str] | None = None  # (campo_original, op, valor)
    todos: list[str] = field(default_factory=list)
    multiple: bool = False


@dataclass
class RawForm:
    kind: str  # html | pdf_acroform | pdf_text
    origin: str
    action: str | None = None
    method: str = "post"
    title: str = ""
    fields: list[RawField] = field(default_factory=list)
    hidden: dict[str, str] = field(default_factory=dict)  # literais fixos (ex.: origem=site)
    todos: list[str] = field(default_factory=list)


def _txt(s: str | None) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s.replace("{", "(").replace("}", ")")


def clean_label(s: str) -> str:
    s = _txt(s)
    s = re.sub(r"\s*\*\s*$", "", s)
    s = re.sub(r"\s*\((obrigat[óo]rio|opcional)\)\s*$", "", s, flags=re.I)
    return s.rstrip(" :").strip()


def humanize(name: str) -> str:
    s = re.sub(r"\[(\w*)\]", r" \1", name)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"[_\-.]+", " ", s)
    return _txt(s).capitalize()


def _marked_required(text: str) -> bool:
    return "*" in (text or "") or bool(re.search(r"\(obrigat[óo]rio\)", text or "", re.I))


# ---------- rótulos ----------
def _label_for(soup: BeautifulSoup, el: Tag) -> tuple[str, str, bool]:
    """(rótulo, fonte, marcado_com_asterisco)."""
    eid = el.get("id")
    if eid:
        lab = soup.find("label", attrs={"for": eid})
        if lab:
            raw = lab.get_text(" ", strip=True)
            return clean_label(raw), "label", _marked_required(raw)
    parent_label = el.find_parent("label")
    if parent_label:
        parts = []
        for node in parent_label.descendants:
            if isinstance(node, NavigableString) and not node.find_parent(["select", "option", "textarea"]):
                parts.append(str(node))
        raw = " ".join(parts)
        if clean_label(raw):
            return clean_label(raw), "label", _marked_required(raw)
    if el.get("aria-label"):
        return clean_label(el["aria-label"]), "aria", False
    if el.get("aria-labelledby"):
        ref = soup.find(id=el["aria-labelledby"].split()[0])
        if ref:
            raw = ref.get_text(" ", strip=True)
            return clean_label(raw), "aria", _marked_required(raw)
    if el.get("placeholder"):
        return clean_label(el["placeholder"]), "placeholder", _marked_required(el["placeholder"])
    if el.get("title"):
        return clean_label(el["title"]), "title", False
    prev = _prev_text(el)
    if prev:
        return clean_label(prev), "prev_text", _marked_required(prev)
    return humanize(el.get("name") or ""), "name", False


def _prev_text(el: Tag) -> str:
    """Texto curto imediatamente antes do controle (ex.: `<p>Nome:</p><input>`)."""
    node = el.previous_sibling
    hops = 0
    while node is not None and hops < 4:
        hops += 1
        if isinstance(node, NavigableString):
            t = _txt(str(node))
        elif isinstance(node, Tag):
            if node.name in ("input", "select", "textarea", "br", "hr"):
                if node.name in ("br", "hr"):
                    node = node.previous_sibling
                    continue
                return ""
            t = _txt(node.get_text(" ", strip=True))
        else:
            t = ""
        if t:
            return t if len(t) <= 80 else ""
        node = node.previous_sibling
    return ""


def _option_label(soup: BeautifulSoup, el: Tag) -> str:
    eid = el.get("id")
    if eid:
        lab = soup.find("label", attrs={"for": eid})
        if lab:
            return clean_label(lab.get_text(" ", strip=True))
    parent = el.find_parent("label")
    if parent:
        return clean_label(parent.get_text(" ", strip=True))
    nxt = el.next_sibling
    if isinstance(nxt, NavigableString) and _txt(str(nxt)):
        return clean_label(str(nxt))
    return el.get("value", "")


# ---------- condicionais ----------
def _parse_cond(expr: str) -> tuple[str, str, str] | None:
    m = re.fullmatch(r"\s*([\w\-\[\]]+)\s*(==|!=|=)\s*['\"]?([^'\"]+?)['\"]?\s*", expr or "")
    if not m:
        return None
    op = "==" if m.group(2) in ("=", "==") else "!="
    return m.group(1), op, m.group(3)


def _condition(el: Tag) -> tuple[tuple[str, str, str] | None, bool]:
    """(condição detectada, há_js_condicional_não_detectável)."""
    node: Tag | None = el
    while node is not None and node.name != "form":
        for a in COND_ATTRS:
            if node.get(a):
                return _parse_cond(node[a]), node[a] is not None and _parse_cond(node[a]) is None
        node = node.parent
    # escondido sem regra declarada → provável JS
    node = el
    while node is not None and node.name != "form":
        style = (node.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or node.has_attr("hidden"):
            return None, True
        node = node.parent
    return None, False


def _legend(el: Tag) -> str | None:
    fs = el.find_parent("fieldset")
    if fs and fs.find("legend"):
        return clean_label(fs.find("legend").get_text(" ", strip=True))
    return None


def _is_honeypot(el: Tag) -> bool:
    name = (el.get("name") or "").lower()
    style = (el.get("style") or "").replace(" ", "").lower()
    wrap_style = ""
    p = el.parent
    if isinstance(p, Tag):
        wrap_style = (p.get("style") or "").replace(" ", "").lower()
    hidden = "display:none" in style or "display:none" in wrap_style or el.get("tabindex") == "-1"
    return hidden and bool(re.search(r"honeypot|hp_|website|url|bot", name)) and not any(el.get(a) for a in COND_ATTRS)


# ---------- extração ----------
def _base_field(soup, el: Tag, kind: str) -> RawField:
    label, src, star = _label_for(soup, el)
    f = RawField(name=el.get("name", ""), kind=kind, label=label, label_source=src)
    f.required = el.has_attr("required") or el.get("aria-required") == "true" or star
    f.pattern = el.get("pattern")
    ml = el.get("maxlength")
    f.maxlength = int(ml) if ml and str(ml).isdigit() else None
    f.group = _legend(el)
    cond, js = _condition(el)
    f.when = cond
    if js:
        f.todos.append(f"campo '{f.name}': lógica condicional não detectada (JS) — revisar se precisa de `when`")
    return f


def _radio_group_label(soup, radios: list[Tag]) -> tuple[str, str, bool]:
    first = radios[0]
    fs = first.find_parent("fieldset")
    if fs and fs.find("legend"):
        # fieldset dedicado ao grupo (só controles deste nome)
        names = {i.get("name") for i in fs.find_all(["input", "select", "textarea"]) if i.get("type") not in SKIP_INPUT_TYPES}
        if names == {first.get("name")}:
            raw = fs.find("legend").get_text(" ", strip=True)
            return clean_label(raw), "legend", _marked_required(raw)
    for attr in ("aria-label", "title"):
        if first.get(attr):
            return clean_label(first[attr]), "aria", False
    group = first.find_parent(attrs={"role": "radiogroup"})
    if group and group.get("aria-label"):
        return clean_label(group["aria-label"]), "aria", False
    # texto anterior ao container do grupo
    container = first.find_parent(["div", "p", "li", "td", "span"]) or first.parent
    candidates = [container]
    if first.find_parent("label"):
        candidates.insert(0, first.find_parent("label"))
    for c in candidates:
        prev = _prev_text(c) if isinstance(c, Tag) else ""
        if prev:
            return clean_label(prev), "prev_text", _marked_required(prev)
        # primeiro texto dentro do container, antes do primeiro rádio
        if isinstance(c, Tag):
            head = []
            for node in c.children:
                if node is first or (isinstance(node, Tag) and (node.find("input") or node.name in ("input", "label"))):
                    break
                head.append(node.get_text(" ", strip=True) if isinstance(node, Tag) else str(node))
            t = _txt(" ".join(head))
            if t and len(t) <= 80:
                return clean_label(t), "prev_text", _marked_required(t)
    return humanize(first.get("name", "")), "name", False


def extract_form(soup: BeautifulSoup, form: Tag, origin: str, base_url: str | None = None) -> RawForm:
    action = form.get("action")
    rf = RawForm(kind="html", origin=origin, method=(form.get("method") or "get").lower())
    if action and base_url:
        rf.action = urljoin(base_url, action)
    elif action and re.match(r"https?://", action):
        rf.action = action
    elif action:
        rf.action = action
        rf.todos.append(f"action relativo '{_txt(action)}' sem URL base — informar a URL absoluta do legado")
    else:
        rf.action = base_url or ""
        rf.todos.append("formulário sem `action` — conferir para onde o legado envia (assumido a própria página)")
    rf.title = _form_title(form)

    seen_groups: set[str] = set()
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        kind = (el.get("type") or "text").lower() if el.name == "input" else el.name
        if el.has_attr("disabled"):
            continue
        if kind == "hidden":
            if not name:
                continue
            if CSRF_RE.search(name):
                rf.todos.append(f"campo oculto '{name}' parece token dinâmico (CSRF/captcha) — o legado pode recusar o POST sem ele")
            else:
                rf.hidden[name] = _txt(el.get("value", ""))
            continue
        if kind in SKIP_INPUT_TYPES or not name:
            continue
        if kind == "password":
            rf.todos.append(f"campo de senha '{name}' ignorado — não coletar senha por chat")
            continue
        if kind == "file":
            rf.todos.append(f"anexo '{name}' ignorado — tipo `file` ainda não suportado")
            continue
        if _is_honeypot(el):
            continue
        if kind in ("radio", "checkbox"):
            if name in seen_groups:
                continue
            group = form.find_all("input", attrs={"type": re.compile(f"^{kind}$", re.I), "name": name})
            if kind == "radio" or len(group) > 1:
                seen_groups.add(name)
                f = _base_field(soup, group[0], "radio" if kind == "radio" else "checkbox_group")
                label, src, star = _radio_group_label(soup, group)
                f.label, f.label_source = label, src
                f.required = any(g.has_attr("required") for g in group) or star or f.required
                f.options = [(g.get("value", "on"), _option_label(soup, g)) for g in group]
                if kind == "checkbox":
                    f.multiple = True
                    f.todos.append(f"campo '{name}': caixas múltiplas viraram escolha única (enum) — revisar")
                rf.fields.append(f)
                continue
            f = _base_field(soup, el, "checkbox")
            f.options = [(el.get("value", "on"), f.label)]
            f.todos.append(f"campo '{name}': checkbox vira sim/não — o legado receberá True/False em vez de '{_txt(el.get('value', 'on'))}'")
            rf.fields.append(f)
            continue
        f = _base_field(soup, el, kind)
        if el.name == "select":
            f.multiple = el.has_attr("multiple")
            first = el.find("option")
            if f.label_source == "name" and first is not None and not (first.get("value") or "").strip():
                ph = clean_label(first.get_text(" ", strip=True))
                if ph and not re.fullmatch(r"[-\s]*(selecione|escolha)[\w\s.…-]*", ph, re.I):
                    f.label, f.label_source = ph, "placeholder"
            for opt in el.find_all("option"):
                val = opt.get("value")
                text = clean_label(opt.get_text(" ", strip=True))
                if val is None:
                    val = text
                if not str(val).strip() or opt.has_attr("disabled") and not str(val).strip():
                    continue
                f.options.append((str(val).strip(), text or str(val)))
            if f.multiple:
                f.todos.append(f"campo '{name}': seleção múltipla virou escolha única (enum) — revisar")
        rf.fields.append(f)
    return rf


def _form_title(form: Tag) -> str:
    for attr in ("aria-label", "data-title", "title"):
        if form.get(attr):
            return clean_label(form[attr])
    for h in form.find_all(["h1", "h2", "h3"], limit=1):
        return clean_label(h.get_text(" ", strip=True))
    prev = form.find_previous(["h1", "h2", "h3"])
    if prev:
        return clean_label(prev.get_text(" ", strip=True))
    leg = form.find("legend")
    if leg:
        return clean_label(leg.get_text(" ", strip=True))
    return ""


def is_relevant(form: Tag, rf: RawForm) -> bool:
    """Ignora busca, login e newsletter de 1 campo."""
    if form.get("role") == "search" or re.search(r"busca|search|pesquisa", form.get("action") or "", re.I):
        return False
    if form.find("input", attrs={"type": "password"}):
        return False
    return len(rf.fields) >= 2


def extract_html(html: str, origin: str, base_url: str | None = None) -> tuple[list[RawForm], str]:
    """Todos os formulários relevantes da página + título da página."""
    soup = BeautifulSoup(html, "html.parser")
    page_title = clean_label(soup.title.get_text()) if soup.title else ""
    out = []
    for form in soup.find_all("form"):
        rf = extract_form(soup, form, origin, base_url)
        if is_relevant(form, rf):
            if not rf.title:
                rf.title = page_title
            out.append(rf)
    return out, page_title
