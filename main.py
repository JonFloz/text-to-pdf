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
    version="1.3.1",
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
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(None, max_length=MAX_STR_LEN)
    tax_id: Optional[str] = Field(None, max_length=60)
    tax_id_label: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=300)
    email: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = Field(None, max_length=40)


class InvoiceRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field("FACTURA", min_length=1, max_length=60)
    invoice_number: str = Field(..., min_length=1, max_length=60)
    date: Optional[str] = Field(None, max_length=30)
    due_date: Optional[str] = Field(None, max_length=30)

    client_name: str = Field(..., min_length=1, max_length=MAX_STR_LEN)
    client_tax_id: Optional[str] = Field(None, max_length=60)
    client_tax_id_label: Optional[str] = Field(None, max_length=20)
    client_address: Optional[str] = Field(None, max_length=300)
    client_email: Optional[str] = Field(None, max_length=120)

    issuer: Optional[IssuerData] = None

    items: List[LineItem] = Field(..., min_length=1, max_length=MAX_ITEMS)

    tax_rate: float = Field(0.16, ge=0, le=1)
    tax_label: Optional[str] = Field(None, max_length=30)
    tax_included: bool = Field(False)

    currency: str = Field("USD", min_length=3, max_length=3)
    notes: Optional[str] = Field(None, max_length=MAX_NOTES_LEN)
    footer_text: Optional[str] = Field(None, max_length=300)

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


def _hrule(width, color, thickness):
    t = Table([[""]], colWidths=[width], rowHeights=[thickness])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


# ==================== GENERADOR ====================

COLOR_PRIMARY = colors.HexColor("#0f172a")
COLOR_ACCENT = colors.HexColor("#3b82f6")
COLOR_LIGHT = colors.HexColor("#f8fafc")
COLOR_BORDER = colors.HexColor("#e2e8f0")
COLOR_BORDER_STRONG = colors.HexColor("#cbd5e1")
COLOR_TEXT = colors.HexColor("#0f172a")
COLOR_TEXT_MUTED = colors.HexColor("#64748b")


def generate_invoice_pdf(data: InvoiceRequest) -> bytes:
    buffer = io.BytesIO()

    MARGIN = 2.0 * cm
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

    PAGE_W = A4[0] - 2 * MARGIN

    # Grilla compartida: 45% / 55% con offset interno en la columna derecha
    LEFT_COL = PAGE_W * 0.45
    RIGHT_COL = PAGE_W * 0.55
    RIGHT_X_OFFSET = 10  # puntos de margen interno del bloque derecho

    # ---- Estilos ----
    styles = getSampleStyleSheet()

    s_title = ParagraphStyle(
        "Title", fontName=FONT_BOLD, fontSize=28, leading=32,
        textColor=COLOR_PRIMARY,
    )
    s_section = ParagraphStyle(
        "Section", fontName=FONT_BOLD, fontSize=7.5, leading=10,
        textColor=COLOR_TEXT_MUTED,
    )
    s_issuer_name = ParagraphStyle(
        "IssuerName", fontName=FONT_BOLD, fontSize=11, leading=14,
        textColor=COLOR_PRIMARY, spaceAfter=2,
    )
    s_body_sm = ParagraphStyle(
        "BodySm", fontName=FONT_NAME, fontSize=8.5, leading=11.5,
        textColor=COLOR_TEXT_MUTED, spaceAfter=0,
    )
    s_client_name = ParagraphStyle(
        "ClientName", fontName=FONT_BOLD, fontSize=11, leading=14,
        textColor=COLOR_PRIMARY, spaceAfter=2,
    )
    s_detail = ParagraphStyle(
        "Detail", fontName=FONT_NAME, fontSize=9, leading=12.5,
        textColor=COLOR_PRIMARY, spaceAfter=0,
    )
    s_notes = ParagraphStyle(
        "Notes", fontName=FONT_NAME, fontSize=9, leading=12.5,
        textColor=colors.HexColor("#334155"),
    )
    s_footer = ParagraphStyle(
        "Footer", fontName=FONT_NAME, fontSize=7.5, leading=10,
        textColor=COLOR_TEXT_MUTED, alignment=1,
    )
    s_th = ParagraphStyle(
        "TH", fontName=FONT_BOLD, fontSize=8.5, leading=11,
        textColor=colors.white,
    )
    s_th_c = ParagraphStyle("THC", parent=s_th, alignment=1)
    s_th_r = ParagraphStyle("THR", parent=s_th, alignment=2)
    s_cell = ParagraphStyle(
        "Cell", fontName=FONT_NAME, fontSize=9, leading=12,
        textColor=COLOR_PRIMARY,
    )
    s_cell_c = ParagraphStyle("CellC", parent=s_cell, alignment=1)
    s_cell_r = ParagraphStyle("CellR", parent=s_cell, alignment=2)
    s_total_label = ParagraphStyle(
        "TotalLabel", fontName=FONT_BOLD, fontSize=12, leading=15,
        textColor=colors.white, alignment=2,
    )
    s_total_value = ParagraphStyle(
        "TotalValue", fontName=FONT_BOLD, fontSize=13, leading=16,
        textColor=colors.white, alignment=2,
    )

    elements: list = []
    today = datetime.now().strftime("%d/%m/%Y")
    invoice_date = _normalize_date(data.date)

    # ================================================================
    # BLOQUE 1: HEADER (título izq + emisor der, mismo X que detalles)
    # ================================================================
    left_table = Table(
        [[Paragraph(_escape(data.title.upper()), s_title)]],
        colWidths=[LEFT_COL],
    )
    left_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    has_issuer = bool(
        data.issuer and any([
            data.issuer.name, data.issuer.tax_id, data.issuer.address,
            data.issuer.email, data.issuer.phone,
        ])
    )
    if has_issuer and data.issuer:
        iss = data.issuer
        issuer_rows = [[Paragraph("EMISOR", s_section)]]
        if iss.name:
            issuer_rows.append([Paragraph(_escape(iss.name), s_issuer_name)])
        if iss.tax_id:
            label_txt = iss.tax_id_label or "Tax ID"
            issuer_rows.append([Paragraph(f"{_escape(label_txt)}: {_escape(iss.tax_id)}", s_body_sm)])
        if iss.address:
            issuer_rows.append([Paragraph(_escape(iss.address), s_body_sm)])
        if iss.email:
            issuer_rows.append([Paragraph(_escape(iss.email), s_body_sm)])
        if iss.phone:
            issuer_rows.append([Paragraph(_escape(iss.phone), s_body_sm)])

        right_table = Table(issuer_rows, colWidths=[RIGHT_COL - RIGHT_X_OFFSET])
        right_table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
    else:
        right_table = Table([[""]], colWidths=[RIGHT_COL - RIGHT_X_OFFSET])

    header = Table(
        [[left_table, right_table]],
        colWidths=[LEFT_COL, RIGHT_COL],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), RIGHT_X_OFFSET),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header)
    elements.append(Spacer(1, 10))

    elements.append(_hrule(PAGE_W, COLOR_BORDER_STRONG, 0.6))
    elements.append(Spacer(1, 22))

    # ================================================================
    # BLOQUE 2: CLIENTE (izq) + DETALLES (der, mismo X que emisor)
    # ================================================================
    client_rows = [[Paragraph("FACTURAR A", s_section)]]
    client_rows.append([Paragraph(_escape(data.client_name), s_client_name)])
    if data.client_tax_id:
        tid_label = data.client_tax_id_label or "ID Fiscal"
        client_rows.append([Paragraph(f"{_escape(tid_label)}: {_escape(data.client_tax_id)}", s_body_sm)])
    if data.client_address:
        client_rows.append([Paragraph(_escape(data.client_address), s_body_sm)])
    if data.client_email:
        client_rows.append([Paragraph(_escape(data.client_email), s_body_sm)])

    client_table = Table(client_rows, colWidths=[LEFT_COL])
    client_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    detail_rows = [[Paragraph("DETALLES DE LA FACTURA", s_section)]]
    detail_items = [
        ("No.", data.invoice_number),
        ("Fecha", invoice_date),
    ]
    if data.due_date:
        detail_items.append(("Vencimiento", data.due_date))
    detail_items.append(("Moneda", data.currency))

    for lbl, val in detail_items:
        detail_rows.append([
            Paragraph(f"<b>{_escape(lbl)}:</b> {_escape(val)}", s_detail)
        ])

    detail_table = Table(detail_rows, colWidths=[RIGHT_COL - RIGHT_X_OFFSET])
    detail_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
    ]))

    info_row = Table(
        [[client_table, detail_table]],
        colWidths=[LEFT_COL, RIGHT_COL],
    )
    info_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), RIGHT_X_OFFSET),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))
    elements.append(info_row)
    elements.append(Spacer(1, 26))

    # ================================================================
    # BLOQUE 3: TABLA DE ITEMS
    # ================================================================
    table_data = [[
        Paragraph("#", s_th_c),
        Paragraph("Descripción", s_th),
        Paragraph("Cant.", s_th_c),
        Paragraph("P. Unit.", s_th_r),
        Paragraph("Total", s_th_r),
    ]]

    subtotal = 0.0
    for idx, item in enumerate(data.items, 1):
        line_total = round(item.quantity * item.unit_price, 2)
        subtotal += line_total
        qty_str = f"{item.quantity:g}"
        table_data.append([
            Paragraph(str(idx), s_cell_c),
            Paragraph(_escape(item.description), s_cell),
            Paragraph(qty_str, s_cell_c),
            Paragraph(_format_money(item.unit_price, data.currency), s_cell_r),
            Paragraph(_format_money(line_total, data.currency), s_cell_r),
        ])

    subtotal = round(subtotal, 2)

    show_tax = data.tax_rate > 0
    if data.tax_included:
        tax_amount = round(subtotal - (subtotal / (1 + data.tax_rate)), 2)
        base = round(subtotal - tax_amount, 2)
        total = subtotal
    else:
        tax_amount = round(subtotal * data.tax_rate, 2)
        base = subtotal
        total = round(subtotal + tax_amount, 2)

    tax_name = data.tax_label or "Impuesto"

    fixed_w = 1.1 * cm + 1.9 * cm + 2.6 * cm + 2.6 * cm
    desc_w = PAGE_W - fixed_w
    col_widths = [1.1 * cm, desc_w, 1.9 * cm, 2.6 * cm, 2.6 * cm]

    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_PRIMARY),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, COLOR_BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, COLOR_LIGHT]),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 4))

    # ================================================================
    # BLOQUE 4: TOTALES
    # ================================================================
    totals_w = PAGE_W * 0.42

    total_rows: list = []
    if show_tax and not data.tax_included:
        total_rows.append(("Subtotal", _format_money(subtotal, data.currency), False))
        total_rows.append((f"{tax_name} ({data.tax_rate*100:.2f}%)", _format_money(tax_amount, data.currency), False))
    elif show_tax and data.tax_included:
        total_rows.append(("Base imponible", _format_money(base, data.currency), False))
        total_rows.append((f"{tax_name} incluido ({data.tax_rate*100:.2f}%)", _format_money(tax_amount, data.currency), False))
    total_rows.append(("TOTAL", _format_money(total, data.currency), True))

    s_total_lbl = ParagraphStyle(
        "TotalLbl", fontName=FONT_NAME, fontSize=9.5, leading=12,
        textColor=COLOR_TEXT, alignment=2,
    )
    s_total_val = ParagraphStyle(
        "TotalVal", fontName=FONT_NAME, fontSize=9.5, leading=12,
        textColor=COLOR_TEXT, alignment=2,
    )

    rendered = []
    for lbl, val, is_total in total_rows:
        if is_total:
            rendered.append([
                Paragraph(_escape(lbl), s_total_label),
                Paragraph(_escape(val), s_total_value),
            ])
        else:
            rendered.append([
                Paragraph(_escape(lbl), s_total_lbl),
                Paragraph(_escape(val), s_total_val),
            ])

    totals_table = Table(rendered, colWidths=[totals_w * 0.55, totals_w * 0.45])
    style_cmds = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -2), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -2), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, COLOR_BORDER_STRONG),
    ]
    tr = len(rendered) - 1
    style_cmds.extend([
        ("BACKGROUND", (0, tr), (-1, tr), COLOR_PRIMARY),
        ("TOPPADDING", (0, tr), (-1, tr), 10),
        ("BOTTOMPADDING", (0, tr), (-1, tr), 10),
        ("LINEABOVE", (0, tr), (-1, tr), 0, colors.white),
    ])
    totals_table.setStyle(TableStyle(style_cmds))

    totals_wrapper = Table(
        [["", totals_table]],
        colWidths=[PAGE_W - totals_w, totals_w],
    )
    totals_wrapper.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    elements.append(totals_wrapper)

    # ================================================================
    # BLOQUE 5: NOTAS
    # ================================================================
    if data.notes:
        elements.append(Spacer(1, 24))
        notes_para = Paragraph(f"<b>Notas:</b> {_escape(data.notes)}", s_notes)
        notes_box = Table([[notes_para]], colWidths=[PAGE_W])
        notes_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_LIGHT),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ("LINEBEFORE", (0, 0), (0, -1), 3, COLOR_ACCENT),
        ]))
        elements.append(notes_box)

    # ================================================================
    # BLOQUE 6: FOOTER
    # ================================================================
    elements.append(Spacer(1, 28))
    elements.append(_hrule(PAGE_W, COLOR_BORDER, 0.4))
    elements.append(Spacer(1, 8))

    footer_text = data.footer_text or (
        f"Documento generado el {today} a las {datetime.now().strftime('%H:%M')}"
    )
    elements.append(Paragraph(_escape(footer_text), s_footer))

    doc.build(elements)
    return buffer.getvalue()


# ==================== ENDPOINTS ====================

@app.get("/")
def root():
    return {
        "service": "PDF Generator API",
        "version": "1.3.1",
        "docs": "/docs",
        "health": "/health",
        "currencies": "/currencies",
        "currencies_supported": SUPPORTED_CODES,
    }


@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "pdf-generator-api", "version": "1.3.1"}


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
                "X-Generated-By": "PDF-Generator-API/1.3.1",
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
