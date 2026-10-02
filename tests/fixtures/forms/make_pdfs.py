"""Gera os PDFs de teste do conversor (Fase 4) só com `pypdf` (sem reportlab).

- `pdf_inscricao_bolsa.pdf`  — AcroForm: texto, combo, checkbox, radio, /TU, required.
- `pdf_reembolso.pdf`        — AcroForm: texto (com /MaxLen), list box com pares [export, exibição], checkbox.
- `pdf_chapado_visita.pdf`   — PDF "chapado" (sem AcroForm): linhas "Nome: ______".

Uso: `.venv/bin/python tests/fixtures/forms/make_pdfs.py` (reescreve os .pdf ao lado deste script).
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

HERE = Path(__file__).parent
REQUIRED = 1 << 1
RADIO = 1 << 15
NO_TOGGLE_OFF = 1 << 14
COMBO = 1 << 17


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Doc:
    def __init__(self) -> None:
        self.w = PdfWriter()
        self.page = self.w.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        self.font_ref = self.w._add_object(font)
        self.page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): self.font_ref})}
        )
        self.lines: list[str] = []
        self.fields = ArrayObject()
        self.annots = ArrayObject()
        self.y = 740

    # ---------- texto ----------
    def text(self, s: str, size: int = 11, x: int = 50) -> None:
        self.lines.append(f"BT /F1 {size} Tf {x} {self.y} Td ({_esc(s)}) Tj ET")
        self.y -= size + 14

    # ---------- campos ----------
    def _widget(self, rect, extra: dict) -> DictionaryObject:
        d = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Rect"): ArrayObject([FloatObject(v) for v in rect]),
                NameObject("/P"): self.page.indirect_reference,
                NameObject("/F"): NumberObject(4),
            }
        )
        d.update(extra)
        return d

    def _add_field(self, d: DictionaryObject, widget: bool = True):
        ref = self.w._add_object(d)
        self.fields.append(ref)
        if widget:
            self.annots.append(ref)
        return ref

    def _ap(self, on: str) -> DictionaryObject:
        def stream(data: bytes):
            s = DecodedStreamObject()
            s.set_data(data)
            s.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                      NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(12), FloatObject(12)])})
            return self.w._add_object(s)

        return DictionaryObject(
            {NameObject("/N"): DictionaryObject({NameObject(f"/{on}"): stream(b"0 0 12 12 re f"), NameObject("/Off"): stream(b"")})}
        )

    def field_text(self, label: str, name: str, tooltip: str | None, required=False, maxlen: int | None = None):
        self.text(label)
        extra = {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject(name),
            NameObject("/Ff"): NumberObject(REQUIRED if required else 0),
            NameObject("/DA"): TextStringObject("/F1 10 Tf 0 g"),
        }
        if tooltip:
            extra[NameObject("/TU")] = TextStringObject(tooltip)
        if maxlen:
            extra[NameObject("/MaxLen")] = NumberObject(maxlen)
        self._add_field(self._widget((220, self.y + 20, 560, self.y + 38), extra))

    def field_choice(self, label: str, name: str, tooltip: str | None, options: list, combo=True, required=False):
        self.text(label)
        opts = ArrayObject()
        for o in options:
            if isinstance(o, tuple):
                opts.append(ArrayObject([TextStringObject(o[0]), TextStringObject(o[1])]))
            else:
                opts.append(TextStringObject(o))
        extra = {
            NameObject("/FT"): NameObject("/Ch"),
            NameObject("/T"): TextStringObject(name),
            NameObject("/Ff"): NumberObject((COMBO if combo else 0) | (REQUIRED if required else 0)),
            NameObject("/Opt"): opts,
            NameObject("/DA"): TextStringObject("/F1 10 Tf 0 g"),
        }
        if tooltip:
            extra[NameObject("/TU")] = TextStringObject(tooltip)
        self._add_field(self._widget((220, self.y + 20, 560, self.y + 38), extra))

    def field_checkbox(self, label: str, name: str, tooltip: str | None, required=False):
        self.text(label, x=70)
        extra = {
            NameObject("/FT"): NameObject("/Btn"),
            NameObject("/T"): TextStringObject(name),
            NameObject("/Ff"): NumberObject(REQUIRED if required else 0),
            NameObject("/V"): NameObject("/Off"),
            NameObject("/AS"): NameObject("/Off"),
            NameObject("/AP"): self._ap("Sim"),
        }
        if tooltip:
            extra[NameObject("/TU")] = TextStringObject(tooltip)
        self._add_field(self._widget((50, self.y + 22, 62, self.y + 34), extra))

    def field_radio(self, label: str, name: str, tooltip: str | None, states: list[tuple[str, str]], required=False):
        self.text(label)
        parent = DictionaryObject(
            {
                NameObject("/FT"): NameObject("/Btn"),
                NameObject("/T"): TextStringObject(name),
                NameObject("/Ff"): NumberObject(RADIO | NO_TOGGLE_OFF | (REQUIRED if required else 0)),
                NameObject("/V"): NameObject("/Off"),
            }
        )
        if tooltip:
            parent[NameObject("/TU")] = TextStringObject(tooltip)
        pref = self._add_field(parent, widget=False)
        kids = ArrayObject()
        for state, shown in states:
            self.text(f"( ) {shown}", x=70)
            kid = self._widget(
                (50, self.y + 22, 62, self.y + 34),
                {NameObject("/Parent"): pref, NameObject("/AS"): NameObject("/Off"), NameObject("/AP"): self._ap(state)},
            )
            kref = self.w._add_object(kid)
            kids.append(kref)
            self.annots.append(kref)
        parent[NameObject("/Kids")] = kids

    # ---------- saída ----------
    def save(self, path: Path, acroform: bool = True) -> None:
        content = DecodedStreamObject()
        content.set_data("\n".join(self.lines).encode("cp1252"))
        self.page[NameObject("/Contents")] = self.w._add_object(content)
        if acroform:
            self.page[NameObject("/Annots")] = self.annots
            self.w._root_object[NameObject("/AcroForm")] = DictionaryObject(
                {
                    NameObject("/Fields"): self.fields,
                    NameObject("/NeedAppearances"): BooleanObject(True),
                    NameObject("/DA"): TextStringObject("/F1 10 Tf 0 g"),
                    NameObject("/DR"): DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): self.font_ref})}),
                }
            )
        with path.open("wb") as f:
            self.w.write(f)


def make_bolsa() -> None:
    d = Doc()
    d.text("Ficha de Inscrição - Programa de Bolsas 2027", size=16)
    d.field_text("Nome completo:", "nome_completo", "Nome completo do candidato", required=True)
    d.field_text("CPF:", "cpf", "CPF do candidato (somente números)", required=True, maxlen=14)
    d.field_text("Data de nascimento:", "data_nasc", "Data de nascimento", required=True)
    d.field_text("E-mail:", "email", "E-mail para contato", required=True)
    d.field_text("Celular:", "celular", "Celular com DDD", required=False)
    d.field_choice("Curso pretendido:", "curso", "Curso pretendido",
                   ["Administração", "Direito", "Enfermagem", "Engenharia Civil"], combo=True, required=True)
    d.field_radio("Turno:", "turno", "Turno preferido", [("manha", "Manhã"), ("noite", "Noite")], required=True)
    d.field_text("Renda familiar mensal (R$):", "renda_familiar", "Renda familiar mensal em reais", required=True)
    d.field_checkbox("Declaro que as informações são verdadeiras", "declaracao", "Declaro que as informações são verdadeiras", required=True)
    d.save(HERE / "pdf_inscricao_bolsa.pdf")


def make_reembolso() -> None:
    d = Doc()
    d.text("Solicitação de Reembolso de Despesas", size=16)
    d.field_text("Nome do colaborador:", "Text1", "Nome do colaborador", required=True)
    d.field_text("Matrícula:", "Text2", "Matrícula funcional", required=True, maxlen=8)
    d.field_choice("Tipo de despesa:", "tipo_despesa", "Tipo de despesa",
                   [("transp", "Transporte"), ("alim", "Alimentação"), ("hosp", "Hospedagem"), ("outros", "Outros")],
                   combo=False, required=True)
    d.field_text("Valor total (R$):", "valor_total", "Valor total da despesa", required=True)
    d.field_text("Data da despesa:", "data_despesa", "Data da despesa", required=True)
    d.field_text("Observações:", "obs", None, required=False)
    d.field_checkbox("Anexei os comprovantes", "comprovantes_anexados", None, required=False)
    d.save(HERE / "pdf_reembolso.pdf")


def make_chapado() -> None:
    d = Doc()
    d.text("Formulário de Agendamento de Visita Técnica", size=16)
    d.text("Nome: ______________________________")
    d.text("Telefone: __________________  E-mail: ____________________")
    d.text("Endereço da obra: ______________________________")
    d.text("CEP: ____________")
    d.text("Data preferida: ___/___/______")
    d.save(HERE / "pdf_chapado_visita.pdf", acroform=False)


if __name__ == "__main__":
    make_bolsa()
    make_reembolso()
    make_chapado()
    print("ok:", ", ".join(p.name for p in sorted(HERE.glob("*.pdf"))))
