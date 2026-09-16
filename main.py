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
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
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
    description="Genera PDFs profesionales desde JSON. Facturas, certificados y reportes.",
    version="1.1.0",
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
# Formato: código ISO -> (símbolo, decimales, estilo)
# estilo: "prefix_us"  -> $1,234.56   (USD, MXN, etc.)
#         "suffix_eu"  -> 1.234,56 €  (EUR, y algunos europeos)
#         "prefix_eu"  -> €1.234,56   (variante)
#         "suffix"     -> 1,234.56 kr (nórdicos)
#         "suffix_in"  -> ₹1,234.56   (India usa separador de miles distinto, simplificado)

CURRENCIES: Dict[str, Tuple[str, int, str]] = {
    # --- América ---
    "USD": ("$",    2, "prefix_us"),
    "CAD": ("C$",   2, "prefix_us"),
    "MXN": ("$",    2, "prefix_us"),
    "BRL": ("R$",   2, "prefix_us"),
    "ARS": ("$",    2, "prefix_us"),
    "CLP": ("$",    0, "prefix_us"),
    "COP": ("$",    2, "prefix_us"),
    "PEN": ("S/",   2, "prefix_us"),
    "UYU": ("$U",   2, "prefix_us"),
    "BOB": ("Bs.",  2, "prefix_us"),
    "PYG": ("₲",    0, "prefix_us"),
    "VES": ("Bs.S", 2, "prefix_us"),
    "DOP": ("RD$",  2, "prefix_us"),
    "GTQ": ("Q",    2, "prefix_us"),
    "CRC": ("₡",    2, "prefix_us"),
    "PAB": ("B/.",  2, "prefix_us"),
    "HNL": ("L",    2, "prefix_us"),
    "NIO": ("C$",   2, "prefix_us"),
    # --- Europa ---
    "EUR": ("€",    2, "suffix_eu"),
    "GBP": ("£",    2, "prefix_us"),
    "CHF": ("CHF",  2, "suffix"),
    "SEK": ("kr",   2, "suffix"),
    "NOK": ("kr",   2, "suffix"),
    "DKK": ("kr",   2, "suffix"),
    "PLN": ("zł",   2, "suffix_eu"),
    "CZK": ("Kč",   2, "suffix_eu"),
    "HUF": ("Ft",   0, "suffix_eu"),
    "RON": ("lei",  2, "suffix_eu"),
    "TRY": ("₺",    2, "prefix_us"),
    "RUB": ("₽",    2, "suffix_eu"),
    "UAH": ("₴",    2, "suffix_eu"),
    # --- Asia / Medio Oriente ---
    "JPY": ("¥",    0, "prefix_us"),
    "CNY": ("¥",    2, "prefix_us"),
    "KRW": ("₩",    0, "prefix_us"),
    "INR": ("₹",    2, "prefix_us"),
    "SGD": ("S$",   2, "prefix_us"),
    "HKD": ("HK$",  2, "prefix_us"),
    "TWD": ("NT$",  2, "prefix_us"),
    "THB": ("฿",    2, "prefix_us"),
    "MYR": ("RM",   2, "prefix_us"),
    "IDR": ("Rp",   0, "prefix_us"),
    "PHP": ("₱",    2, "prefix_us"),
    "VND": ("₫",    0, "suffix"),
    "AED": ("AED",  2, "prefix_us"),
    "SAR": ("SAR",  2, "prefix_us"),
    "ILS": ("ILS",  2, "prefix_us"),   # ₪ no está en DejaVu → usamos ISO
    # --- África / Oceanía ---
    "ZAR": ("R",    2, "prefix_us"),
    "NGN": ("₦",    2, "prefix_us"),
    "KES": ("KSh",  2, "prefix_us"),
    "EGP": ("E£",   2, "prefix_us"),
    "MAD": ("MAD",  2, "suffix"),
    "AUD": ("A$",   2, "prefix_us"),
    "NZD": ("NZ$",  2, "prefix_us"),
}

SUPPORTED_CODES = sorted(CURRENCIES.keys())


def _format_money(value: float, currency: str) -> str:
    symbol, decimals, style = CURRENCIES[currency]
    # Separadores según estilo
    if style in ("suffix_eu", "prefix_eu"):
        int_part, dec_part = f"{value:,.{decimals}f}".split(".")
        int_part = int_part.replace(",", "X").replace(".", ",").replace("X", ".")
        number = int_part if decimals == 0 else f"{int_part},{dec_part}"
    else:
        # prefix_us, suffix, suffix_in → formato anglosajón
        number = f"{value:,.{decimals}f}"

    if style == "prefix_us":
        return f"{symbol}{number}"
    if style == "prefix_eu":
        return f"{symbol}{number}"
    # suffix / suffix_eu
    return f"{number} {symbol}"


# ==================== MODELOS ====================

class LineItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    description: str = Field(..., min_length=1, max_length=MAX_STR_LEN)
    quantity: int = Field(1, ge=1, le=100_000)
    unit_price: float = Field(..., ge=0, le=MAX_AMOUNT)

    @field_validator("description")
    @classmethod
    def clean_description(cls, v: str) -> str:
        v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
        return v.replace("\\n", " ").strip()


class InvoiceRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field("FACTURA", min_length=1, max_length=60)
    client_name: str = Field(..., min_length=1, max_length=MAX_STR_LEN)
    client_id: Optional[str] = Field(None, max_length=60)
    invoice_number: str = Field(..., min_length=1, max_length=60)
    date: Optional[str] = Field(None, max_length=30)
    items: List[LineItem] = Field(..., min_length=1, max_length=MAX_ITEMS)
    tax_rate: float = Field(0.16, ge=0, le=1)
    tax_label: Optional[str] = Field(None, max_length=30)
    notes: Optional[str] = Field(None, max_length=MAX_NOTES_LEN)
    currency: str = Field("USD", min_length=3, max_length=3, description=f"Código ISO 4217. Soportadas: {', '.join(SUPPORTED_CODES)}")

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: Optional[str]) -> str:
        if not v:
            return datetime.now().strftime("%d/%m/%Y")
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                datetime.strptime(v, fmt)
                return v
            except ValueError:
                continue
        raise ValueError("date debe ser DD/MM/YYYY, DD-MM-YYYY o YYYY-MM-DD")

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
    """ASCII puro, seguro para Content-Disposition."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\-.]", "_", name)
    return (name.strip("_") or "document")[:80]


def _escape(text: str) -> str:
    """Escapa caracteres reservados de Paragraph (XML-like)."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# ==================== GENERADOR ====================

def generate_invoice_pdf(data: InvoiceRequest) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"{data.title} {data.invoice_number}",
        author=data.client_name,
        creator="PDF Generator API",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Heading1"],
        fontName=FONT_BOLD,
        fontSize=22,
        textColor=colors.HexColor("#1a1a2e"),
        spaceAfter=24,
    )
    normal = ParagraphStyle(
        "NormalCustom", parent=styles["Normal"],
        fontName=FONT_NAME, fontSize=10, leading=13,
    )
    notes_style = ParagraphStyle(
        "Notes", parent=normal, fontSize=9, textColor=colors.HexColor("#333333"),
    )
    footer_style = ParagraphStyle(
        "Footer", parent=normal, fontSize=8, textColor=colors.grey,
    )

    elements = []
    elements.append(Paragraph(_escape(data.title), title_style))
    elements.append(Spacer(1, 12))

    # --- Info cliente ---
    info_rows = [
        ["Cliente:", _escape(data.client_name)],
        ["ID Cliente:", _escape(data.client_id) if data.client_id else "N/A"],
        ["No. Factura:", _escape(data.invoice_number)],
        ["Fecha:", _escape(data.date or "")],
        ["Moneda:", data.currency],
    ]
    info_table = Table(info_rows, colWidths=[3 * cm, 12 * cm])
    info_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), FONT_BOLD),
        ("FONTNAME", (1, 0), (1, -1), FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 18))

    # --- Tabla items ---
    table_data = [["#", "Descripción", "Cant.", "P. Unit.", "Total"]]
    subtotal = 0.0
    for idx, item in enumerate(data.items, 1):
        line_total = round(item.quantity * item.unit_price, 2)
        subtotal += line_total
        table_data.append([
            str(idx),
            Paragraph(_escape(item.description), normal),
            str(item.quantity),
            _format_money(item.unit_price, data.currency),
            _format_money(line_total, data.currency),
        ])

    subtotal = round(subtotal, 2)
    tax_amount = round(subtotal * data.tax_rate, 2)
    total = round(subtotal + tax_amount, 2)

    tax_label = data.tax_label or (f"IVA ({data.tax_rate*100:.0f}%)" if data.tax_rate > 0 else "Impuesto")

    table_data.append(["", "", "", "Subtotal:", _format_money(subtotal, data.currency)])
    if data.tax_rate > 0:
        table_data.append(["", "", "", f"{tax_label}:", _format_money(tax_amount, data.currency)])
    table_data.append(["", "", "", "TOTAL:", _format_money(total, data.currency)])

    col_widths = [1.2 * cm, 7.3 * cm, 2 * cm, 3 * cm, 3 * cm]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16213e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("FONTNAME", (0, 1), (-1, -4), FONT_NAME),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -4), 0.4, colors.grey),
        ("LINEBELOW", (0, -3), (-1, -3), 1, colors.HexColor("#16213e")),
        ("FONTNAME", (3, -3), (3, -1), FONT_BOLD),
        ("FONTNAME", (4, -1), (4, -1), FONT_BOLD),
        ("FONTSIZE", (3, -1), (4, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(items_table)

    if data.notes:
        elements.append(Spacer(1, 18))
        elements.append(Paragraph(f"<b>Notas:</b> {_escape(data.notes)}", notes_style))

    elements.append(Spacer(1, 30))
    elements.append(Paragraph(
        f"Documento generado automáticamente el {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        footer_style,
    ))

    doc.build(elements)
    return buffer.getvalue()


# ==================== ENDPOINTS ====================

@app.get("/")
def root():
    return {
        "service": "PDF Generator API",
        "version": "1.1.0",
        "docs": "/docs",
        "health": "/health",
        "currencies_supported": SUPPORTED_CODES,
    }


@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "pdf-generator-api", "version": "1.1.0"}


@app.get("/currencies")
def list_currencies():
    """Lista todas las monedas soportadas con símbolo y decimales."""
    return {
        "count": len(CURRENCIES),
        "currencies": [
            {"code": code, "symbol": sym, "decimals": dec}
            for code, (sym, dec, _style) in sorted(CURRENCIES.items())
        ],
    }


# ⚠️ Orden de decoradores: @app.post ARRIBA, @limiter.limit ABAJO (slowapi lo requiere)
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
                "X-Generated-By": "PDF-Generator-API/1.1.0",
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