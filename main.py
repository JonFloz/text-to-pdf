import io
import re
import unicodedata
from typing import List, Optional, Dict, Tuple
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, Field, field_validator, ConfigDict
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ==================== CONFIGURACIÓN ====================

MAX_ITEMS = 200
MAX_STR_LEN = 200
MAX_NOTES_LEN = 1000
MAX_AMOUNT = 1_000_000_000

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="PDF Generator API",
    description="Genera facturas PDF profesionales desde JSON. 52 monedas, impuestos configurables, soporte multi-país.",
    version="1.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ==================== FUENTES UNICODE ====================

FONT_NAME = "Helvetica"
FONT_BOLD = "Helvetica-Bold"


def _try_register_unicode_font() -> None:
    global FONT_NAME, FONT_BOLD
    import os
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("DejaVuSans", regular))
                pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bold))
                FONT_NAME = "DejaVuSans"
                FONT_BOLD = "DejaVuSans-Bold"
                return
            except Exception:
                continue


_try_register_unicode_font()

# ==================== CATÁLOGO DE MONEDAS ====================

CURRENCIES: Dict[str, Tuple[str, int, str]] = {
    "USD": ("$",    2, "prefix_us"), "CAD": ("C$",   2, "prefix_us"),
    "MXN": ("$",    2, "prefix_us"), "BRL": ("R$",   2, "prefix_us"),
    "ARS": ("$",    2, "prefix_us"), "CLP": ("$",    0, "prefix_us"),
    "COP": ("$",    2, "prefix_us"), "PEN": ("S/",   2, "prefix_us"),
    "UYU": ("$U",   2, "prefix_us"), "BOB": ("Bs.",  2, "prefix_us"),
    "PYG": ("₲",    0, "prefix_us"), "VES": ("Bs.S", 2, "prefix_us"),
    "DOP": ("RD$",  2, "prefix_us"), "GTQ": ("Q",    2, "prefix_us"),
    "CRC": ("₡",    2, "prefix_us"), "PAB": ("B/.",  2, "prefix_us"),
    "HNL": ("L",    2, "prefix_us"), "NIO": ("C$",   2, "prefix_us"),
    "EUR": ("€",    2, "suffix_eu"), "GBP": ("£",    2, "prefix_us"),
    "CHF": ("CHF",  2, "suffix"),    "SEK": ("kr",   2, "suffix"),
    "NOK": ("kr",   2, "suffix"),    "DKK": ("kr",   2, "suffix"),
    "PLN": ("zł",   2, "suffix_eu"), "CZK": ("Kč",   2, "suffix_eu"),
    "HUF": ("Ft",   0, "suffix_eu"), "RON": ("lei",  2, "suffix_eu"),
    "TRY": ("₺",    2, "prefix_us"), "RUB": ("₽",    2, "suffix_eu"),
    "UAH": ("₴",    2, "suffix_eu"), "JPY": ("¥",    0, "prefix_us"),
    "CNY": ("¥",    2, "prefix_us"), "KRW": ("₩",    0, "prefix_us"),
    "INR": ("₹",    2, "prefix_us"), "SGD": ("S$",   2, "prefix_us"),
    "HKD": ("HK$",  2, "prefix_us"), "TWD": ("NT$",  2, "prefix_us"),
    "THB": ("฿",    2, "prefix_us"), "MYR": ("RM",   2, "prefix_us"),
    "IDR": ("Rp",   0, "prefix_us"), "PHP": ("₱",    2, "prefix_us"),
    "VND": ("₫",    0, "suffix"),    "AED": ("AED",  2, "prefix_us"),
    "SAR": ("SAR",  2, "prefix_us"), "ILS": ("ILS",  2, "prefix_us"),
    "ZAR": ("R",    2, "prefix_us"), "NGN": ("₦",    2, "prefix_us"),
    "KES": ("KSh",  2, "prefix_us"), "EGP": ("E£",   2, "prefix_us"),
    "MAD": ("MAD",  2, "suffix"),    "AUD": ("A$",   2, "prefix_us"),
    "NZD": ("NZ$",  2, "prefix_us"),
}

SUPPORTED_CODES = sorted(CURRENCIES.keys())


def _format_money(value: float, currency: str) -> str:
    symbol, decimals, style = CURRENCIES[currency]
    if style in ("suffix_eu", "prefix_eu"):
        int_part, dec_part = f"{value:,.{decimals}f}".split(".")
        int_part = int_part.replace(",", "X").replace(".", ",").replace("X", ".")
        number = int_part if decimals == 0 else f"{int_part},{dec_part}"
    else:
        number = f"{value:,.{decimals}f}"
    if style in ("prefix_us", "prefix_eu"):
        return f"{symbol}{number}"
    return f"{number} {symbol}"


# ==================== MODELOS ====================

class LineItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    description: str = Field(..., min_length=1, max_length=MAX_STR_LEN)
    quantity: float = Field(1, gt=0, le=100_000)
    unit_price: float = Field(..., ge=0, le=MAX_AMOUNT)

    @field_validator("description")
    @classmethod
    def clean_description(cls, v: str) -> str:
        v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
        return v.replace("\\n", " ").strip()


class IssuerData(BaseModel):
    """Datos opcionales del emisor de la factura."""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(None, max_length=MAX_STR_LEN)
    tax_id: Optional[str] = Field(None, max_length=60, description="RFC, CUIT, RUC, NIF, CIF, VAT, etc.")
    tax_id_label: Optional[str] = Field(None, max_length=20, description="Etiqueta del ID fiscal: RFC, CUIT, RUC...")
    address: Optional[str] = Field(None, max_length=300)
    email: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = Field(None, max_length=40)


class InvoiceRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field("FACTURA", min_length=1, max_length=60)
    invoice_number: str = Field(..., min_length=1, max_length=60)
    date: Optional[str] = Field(None, max_length=30)
    due_date: Optional[str] = Field(None, max_length=30, description="Fecha de vencimiento (opcional)")

    # Cliente
    client_name: str = Field(..., min_length=1, max_length=MAX_STR_LEN)
    client_tax_id: Optional[str] = Field(None, max_length=60, description="Identificación fiscal del cliente: RFC, DNI, CUIT, NIT, RUC, VAT, etc.")
    client_tax_id_label: Optional[str] = Field(None, max_length=20, description="Etiqueta para mostrar: RFC, DNI, CUIT, NIT, RUC, NIF, VAT...")
    client_address: Optional[str] = Field(None, max_length=300)
    client_email: Optional[str] = Field(None, max_length=120)

    # Emisor (opcional)
    issuer: Optional[IssuerData] = None

    # Items
    items: List[LineItem] = Field(..., min_length=1, max_length=MAX_ITEMS)

    # Impuestos
    tax_rate: float = Field(0.16, ge=0, le=1, description="Tasa de impuesto (0 = sin impuesto, 0.16 = 16%)")
    tax_label: Optional[str] = Field(None, max_length=30, description="Nombre del impuesto: IVA, GST, VAT, Moms, Consumption Tax, etc.")
    tax_included: bool = Field(False, description="Si es true, los precios ya incluyen impuesto (no se suma al total)")

    # Otros
    currency: str = Field("USD", min_length=3, max_length=3)
    notes: Optional[str] = Field(None, max_length=MAX_NOTES_LEN)
    footer_text: Optional[str] = Field(None, max_length=300, description="Texto adicional al pie")

    @field_validator("date", "due_date")
    @classmethod
    def validate_date(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                datetime.strptime(v, fmt)
                return v
            except ValueError:
                continue
        raise ValueError("La fecha debe ser DD/MM/YYYY, DD-MM-YYYY o YYYY-MM-DD")

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper().strip()
        if v not in CURRENCIES:
            raise ValueError(
                f"Moneda '{v}' no soportada. Usa una de: {', '.join(SUPPORTED_CODES)}"
            )
        return v

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.replace("\\n", "\n").strip() or None


# ==================== HELPERS ====================

def _safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\-.]", "_", name)
    return (name.strip("_") or "document")[:80]


def _escape(text: str) -> str:
    return (
        str(text).replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def _normalize_date(v: Optional[str]) -> str:
    if not v:
        return datetime.now().strftime("%d/%m/%Y")
    return v


# ==================== GENERADOR ====================

# Paleta
COLOR_PRIMARY = colors.HexColor("#0f172a")   # azul muy oscuro
COLOR_ACCENT = colors.HexColor("#3b82f6")    # azul
COLOR_LIGHT = colors.HexColor("#f1f5f9")     # gris muy claro
COLOR_BORDER = colors.HexColor("#cbd5e1")    # gris borde
COLOR_TEXT_MUTED = colors.HexColor("#64748b")  # gris texto


def generate_invoice_pdf(data: InvoiceRequest) -> bytes:
    buffer = io.BytesIO()

    # Márgenes parejos
    MARGIN = 1.8 * cm
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=MARGIN,
        leftMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=f"{data.title} {data.invoice_number}",
        author=(data.issuer.name if data.issuer and data.issuer.name else data.client_name),
        creator="PDF Generator API",
    )

    page_width = A4[0] - 2 * MARGIN  # ancho útil

    # ---- Estilos ----
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "H1", parent=styles["Heading1"],
        fontName=FONT_BOLD, fontSize=26, leading=30,
        textColor=COLOR_PRIMARY, spaceAfter=0, spaceBefore=0,
    )
    label = ParagraphStyle(
        "Label", fontName=FONT_BOLD, fontSize=8.5, leading=11,
        textColor=COLOR_TEXT_MUTED, spaceAfter=1,
    )
    value = ParagraphStyle(
        "Value", fontName=FONT_NAME, fontSize=10.5, leading=13,
        textColor=COLOR_PRIMARY, spaceAfter=0,
    )
    value_bold = ParagraphStyle(
        "ValueBold", parent=value, fontName=FONT_BOLD,
    )
    small = ParagraphStyle(
        "Small", fontName=FONT_NAME, fontSize=8.5, leading=11,
        textColor=COLOR_TEXT_MUTED,
    )
    notes_body = ParagraphStyle(
        "NotesBody", fontName=FONT_NAME, fontSize=9.5, leading=13,
        textColor=colors.HexColor("#334155"),
    )
    footer_style = ParagraphStyle(
        "FooterStyle", fontName=FONT_NAME, fontSize=7.5, leading=10,
        textColor=COLOR_TEXT_MUTED, alignment=1,  # centrado
    )

    elements = []
    today = datetime.now().strftime("%d/%m/%Y")
    invoice_date = _normalize_date(data.date)

    # ==================== HEADER ====================
    # Bloque izquierdo: título + número de factura
    left_header = [
        [Paragraph(_escape(data.title.upper()), h1)],
    ]
    left_table = Table(left_header, colWidths=[page_width * 0.5])
    left_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    # Bloque derecho: emisor (si existe)
    right_lines = []
    if data.issuer:
        iss = data.issuer
        if iss.name:
            right_lines.append([Paragraph(f"<b>{_escape(iss.name)}</b>", value_bold)])
        if iss.tax_id:
            label_txt = iss.tax_id_label or "Tax ID"
            right_lines.append([Paragraph(f"{label_txt}: {_escape(iss.tax_id)}", small)])
        if iss.address:
            right_lines.append([Paragraph(_escape(iss.address), small)])
        if iss.email:
            right_lines.append([Paragraph(_escape(iss.email), small)])
        if iss.phone:
            right_lines.append([Paragraph(_escape(iss.phone), small)])

    if right_lines:
        right_table = Table(right_lines, colWidths=[page_width * 0.5])
        right_table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ]))
    else:
        right_table = Table([[Paragraph("", small)]], colWidths=[page_width * 0.5])

    header = Table([[left_table, right_table]], colWidths=[page_width * 0.5, page_width * 0.5])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header)
    elements.append(Spacer(1, 6))

    # Línea azul decorativa
    line = Table([[""]], colWidths=[page_width], rowHeights=[2])
    line.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(line)
    elements.append(Spacer(1, 18))

    # ==================== INFO FACTURA / CLIENTE (2 columnas) ====================
    # Columna izquierda: datos del cliente
    client_block = [[Paragraph("FACTURAR A", label)]]
    client_block.append([Paragraph(_escape(data.client_name), value_bold)])
    if data.client_tax_id:
        tid_label = data.client_tax_id_label or "ID Fiscal"
        client_block.append([Paragraph(f"{tid_label}: {_escape(data.client_tax_id)}", small)])
    if data.client_address:
        client_block.append([Paragraph(_escape(data.client_address), small)])
    if data.client_email:
        client_block.append([Paragraph(_escape(data.client_email), small)])

    # Columna derecha: datos de la factura
    invoice_block = [[Paragraph("DETALLES", label)]]
    invoice_block.append([Paragraph(f"<b>No.</b> {_escape(data.invoice_number)}", small)])
    invoice_block.append([Paragraph(f"<b>Fecha:</b> {_escape(invoice_date)}", small)])
    if data.due_date:
        invoice_block.append([Paragraph(f"<b>Vence:</b> {_escape(data.due_date)}", small)])
    invoice_block.append([Paragraph(f"<b>Moneda:</b> {data.currency}", small)])

    col_w = page_width * 0.5
    client_table = Table(client_block, colWidths=[col_w - 12])
    client_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    invoice_table = Table(invoice_block, colWidths=[col_w - 12])
    invoice_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    info_row = Table(
        [[client_table, invoice_table]],
        colWidths=[col_w, col_w],
    )
    info_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 12),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))
    elements.append(info_row)
    elements.append(Spacer(1, 22))

    # ==================== TABLA DE ITEMS ====================
    # Header
    table_data = [[
        Paragraph("<b>#</b>", ParagraphStyle("th", fontName=FONT_BOLD, fontSize=9, textColor=colors.white, alignment=1)),
        Paragraph("<b>Descripción</b>", ParagraphStyle("th2", fontName=FONT_BOLD, fontSize=9, textColor=colors.white)),
        Paragraph("<b>Cant.</b>", ParagraphStyle("th3", fontName=FONT_BOLD, fontSize=9, textColor=colors.white, alignment=1)),
        Paragraph("<b>P. Unit.</b>", ParagraphStyle("th4", fontName=FONT_BOLD, fontSize=9, textColor=colors.white, alignment=2)),
        Paragraph("<b>Total</b>", ParagraphStyle("th5", fontName=FONT_BOLD, fontSize=9, textColor=colors.white, alignment=2)),
    ]]

    subtotal = 0.0
    for idx, item in enumerate(data.items, 1):
        line_total = round(item.quantity * item.unit_price, 2)
        subtotal += line_total
        qty_str = f"{item.quantity:g}"  # 2.0 -> "2", 2.5 -> "2.5"
        table_data.append([
            Paragraph(str(idx), ParagraphStyle("c", fontName=FONT_NAME, fontSize=9.5, alignment=1)),
            Paragraph(_escape(item.description), ParagraphStyle("d", fontName=FONT_NAME, fontSize=9.5, leading=12)),
            Paragraph(qty_str, ParagraphStyle("q", fontName=FONT_NAME, fontSize=9.5, alignment=1)),
            Paragraph(_format_money(item.unit_price, data.currency), ParagraphStyle("u", fontName=FONT_NAME, fontSize=9.5, alignment=2)),
            Paragraph(_format_money(line_total, data.currency), ParagraphStyle("t", fontName=FONT_NAME, fontSize=9.5, alignment=2)),
        ])

    subtotal = round(subtotal, 2)

    # Impuestos
    show_tax = data.tax_rate > 0
    if data.tax_included:
        # Los precios ya incluyen impuesto: el total es el subtotal
        tax_amount = round(subtotal - (subtotal / (1 + data.tax_rate)), 2)
        base = round(subtotal - tax_amount, 2)
        total = subtotal
    else:
        tax_amount = round(subtotal * data.tax_rate, 2)
        base = subtotal
        total = round(subtotal + tax_amount, 2)

    tax_name = data.tax_label or "Impuesto"

    # Anchos: # (1.2cm) | desc (resto) | cant (2cm) | unit (3cm) | total (3cm)
    fixed_w = 1.2 * cm + 2 * cm + 3 * cm + 3 * cm
    desc_w = page_width - fixed_w
    col_widths = [1.2 * cm, desc_w, 2 * cm, 3 * cm, 3 * cm]

    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("TOPPADDING", (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 9),
        # Filas
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        # Bordes suaves entre filas
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, COLOR_BORDER),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, COLOR_PRIMARY),
        # Zebra striping muy sutil
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfc")]),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 12))

    # ==================== TOTALES (bloque derecho) ====================
    totals_rows = []
    if show_tax and not data.tax_included:
        totals_rows.append(("Subtotal", _format_money(subtotal, data.currency), False))
        totals_rows.append((f"{tax_name} ({data.tax_rate*100:.2f}%):", _format_money(tax_amount, data.currency), False))
        totals_rows.append(("TOTAL", _format_money(total, data.currency), True))
    elif show_tax and data.tax_included:
        totals_rows.append(("Base imponible", _format_money(base, data.currency), False))
        totals_rows.append((f"{tax_name} incluido ({data.tax_rate*100:.2f}%):", _format_money(tax_amount, data.currency), False))
        totals_rows.append(("TOTAL", _format_money(total, data.currency), True))
    else:
        totals_rows.append(("TOTAL", _format_money(total, data.currency), True))

    totals_data = []
    for label_txt, val, is_total in totals_data_style(totals_rows):
        totals_data.append([label_txt, val, is_total])

    # Construimos tabla: etiqueta + valor
    label_style = ParagraphStyle("tl", fontName=FONT_NAME, fontSize=9.5, alignment=2, textColor=COLOR_PRIMARY)
    label_style_b = ParagraphStyle("tlb", fontName=FONT_BOLD, fontSize=10.5, alignment=2, textColor=COLOR_PRIMARY)
    val_style = ParagraphStyle("tv", fontName=FONT_NAME, fontSize=9.5, alignment=2, textColor=COLOR_PRIMARY)
    val_style_b = ParagraphStyle("tvb", fontName=FONT_BOLD, fontSize=12, alignment=2, textColor=COLOR_PRIMARY)

    totals_table_rows = []
    for label_txt, val, is_total in totals_rows:
        lbl = Paragraph(_escape(label_txt), label_style_b if is_total else label_style)
        v = Paragraph(_escape(val), val_style_b if is_total else val_style)
        totals_table_rows.append([lbl, v])

    # Bloque de totales: ancho fijo, alineado a la derecha
    totals_width = page_width * 0.45
    totals_table = Table(totals_table_rows, colWidths=[totals_width * 0.55, totals_width * 0.45])
    style_cmds = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("LINEABOVE", (0, 0), (-1, 0), 0.4, COLOR_BORDER),
    ]
    # Fila TOTAL destacada
    total_row_idx = len(totals_table_rows) - 1
    style_cmds.append(("BACKGROUND", (0, total_row_idx), (-1, total_row_idx), COLOR_PRIMARY))
    style_cmds.append(("TEXTCOLOR", (0, total_row_idx), (-1, total_row_idx), colors.white))
    style_cmds.append(("TOPPADDING", (0, total_row_idx), (-1, total_row_idx), 9))
    style_cmds.append(("BOTTOMPADDING", (0, total_row_idx), (-1, total_row_idx), 9))
    totals_table.setStyle(TableStyle(style_cmds))

    # Reemplazamos el color de texto de la fila total (necesitamos re-crear los Paragraph)
    final_rows = []
    for i, (label_txt, val, is_total) in enumerate(totals_rows):
        if is_total:
            lbl = Paragraph(_escape(label_txt), ParagraphStyle("tlt", fontName=FONT_BOLD, fontSize=11, alignment=2, textColor=colors.white))
            v = Paragraph(_escape(val), ParagraphStyle("tvt", fontName=FONT_BOLD, fontSize=13, alignment=2, textColor=colors.white))
        else:
            lbl = Paragraph(_escape(label_txt), label_style)
            v = Paragraph(_escape(val), val_style)
        final_rows.append([lbl, v])

    totals_table = Table(final_rows, colWidths=[totals_width * 0.55, totals_width * 0.45])
    totals_table.setStyle(TableStyle(style_cmds))

    # Envolver en tabla para alinear a la derecha
    totals_wrapper = Table(
        [["", totals_table]],
        colWidths=[page_width - totals_width, totals_width],
    )
    totals_wrapper.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    elements.append(totals_wrapper)
    elements.append(Spacer(1, 20))

    # ==================== NOTAS ====================
    if data.notes:
        notes_para = Paragraph(f"<b>Notas:</b> {_escape(data.notes)}", notes_body)
        notes_box = Table([[notes_para]], colWidths=[page_width])
        notes_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_LIGHT),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LINEBEFORE", (0, 0), (0, -1), 3, COLOR_ACCENT),
        ]))
        elements.append(notes_box)
        elements.append(Spacer(1, 14))

    # ==================== FOOTER ====================
    elements.append(Spacer(1, 6))
    footer_line = Table([[""]], colWidths=[page_width], rowHeights=[0.4])
    footer_line.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(footer_line)
    elements.append(Spacer(1, 6))

    footer_text = data.footer_text or f"Documento generado el {today} a las {datetime.now().strftime('%H:%M')}"
    elements.append(Paragraph(_escape(footer_text), footer_style))

    doc.build(elements)
    return buffer.getvalue()


def totals_data_style(rows):
    """Helper que devuelve las filas tal cual (para claridad)."""
    return rows


# ==================== ENDPOINTS ====================

@app.get("/")
def root():
    return {
        "service": "PDF Generator API",
        "version": "1.2.0",
        "docs": "/docs",
        "health": "/health",
        "currencies": "/currencies",
        "currencies_supported": SUPPORTED_CODES,
    }


@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "pdf-generator-api", "version": "1.2.0"}


@app.get("/currencies")
def list_currencies():
    return {
        "count": len(CURRENCIES),
        "currencies": [
            {"code": code, "symbol": sym, "decimals": dec}
            for code, (sym, dec, _style) in sorted(CURRENCIES.items())
        ],
    }


@app.post(
    "/generate/invoice",
    status_code=200,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "PDF generado correctamente"},
        400: {"description": "Datos inválidos"},
        422: {"description": "Validación fallida"},
        429: {"description": "Demasiadas peticiones"},
        500: {"description": "Error interno"},
    },
)
@limiter.limit("120/minute")
def create_invoice(request: Request, data: InvoiceRequest):
    try:
        pdf_bytes = generate_invoice_pdf(data)
        filename = f"invoice_{_safe_filename(data.invoice_number)}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(pdf_bytes)),
                "X-Generated-By": "PDF-Generator-API/1.2.0",
                "X-Currency": data.currency,
                "Cache-Control": "no-store",
            },
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Error generando el PDF. Verifica los datos e intenta de nuevo.",
        )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
