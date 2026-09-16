# PDF Generator API

Genera facturas PDF profesionales desde JSON. Soporta **52 monedas**, **impuestos configurables** por país, y **campos fiscales flexibles** (RFC, CUIT, RUC, NIT, NIF, DNI, VAT, etc.).

## Endpoints

### `GET /health`
Estado del servicio.

### `GET /currencies`
Lista las 52 monedas soportadas con símbolo y decimales.

### `POST /generate/invoice`
Genera una factura PDF.

## Parámetros del request

### Factura
| Campo | Tipo | Req | Descripción |
|-------|------|-----|-------------|
| `invoice_number` | string | ✅ | Número único de factura |
| `title` | string | ❌ | Por defecto "FACTURA" |
| `date` | string | ❌ | DD/MM/YYYY (def: hoy) |
| `due_date` | string | ❌ | Fecha de vencimiento |
| `currency` | string | ❌ | ISO 4217 (def: USD) |

### Emisor (opcional)
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `issuer.name` | string | Nombre / razón social |
| `issuer.tax_id` | string | RFC, CUIT, NIF, VAT, etc. |
| `issuer.tax_id_label` | string | "RFC", "CUIT", "NIF"... |
| `issuer.address` | string | Dirección |
| `issuer.email` | string | Email de contacto |
| `issuer.phone` | string | Teléfono |

### Cliente
| Campo | Tipo | Req | Descripción |
|-------|------|-----|-------------|
| `client_name` | string | ✅ | Nombre / razón social |
| `client_tax_id` | string | ❌ | RFC, DNI, CUIT, NIT, RUC, VAT... |
| `client_tax_id_label` | string | ❌ | Etiqueta a mostrar: "RFC", "DNI"... |
| `client_address` | string | ❌ | Dirección |
| `client_email` | string | ❌ | Email |

### Items
| Campo | Tipo | Req | Descripción |
|-------|------|-----|-------------|
| `items[].description` | string | ✅ | Descripción del ítem |
| `items[].quantity` | number | ❌ | Cantidad (def: 1) |
| `items[].unit_price` | number | ✅ | Precio unitario |

### Impuestos (configurables por el cliente)

**El cliente de la API decide qué impuesto cobrar** enviando `tax_rate` y `tax_label`. La API no asume ningún país, no mantiene tabla de tasas, y no valida contra ninguna legislación. Solo calcula el monto y lo muestra en el PDF con la etiqueta que el cliente indique.

| Campo | Tipo | Req | Descripción |
|-------|------|-----|-------------|
| `tax_rate` | number | ❌ | Tasa decimal. `0.16` = 16%, `0.21` = 21%, `0.075` = 7.5%. **`0` o ausente = sin impuesto**. |
| `tax_label` | string | ❌ | Texto que se muestra en el PDF junto a la tasa. Si no se envía, se usa "Impuesto". |
| `tax_included` | bool | ❌ | Si `true`, los `unit_price` ya incluyen impuesto y no se suma al total. Por defecto `false`. |

### Ejemplos de tasas según el cliente (referencia, no impuestas por la API)

El cliente elige la que le corresponda. Estos son solo ejemplos comunes:

| País / Región | `tax_rate` | `tax_label` |
|---------------|-----------|-------------|
| México | `0.16` | `"IVA"` |
| España (general) | `0.21` | `"IVA"` |
| España (reducido) | `0.10` | `"IVA"` |
| Argentina | `0.21` | `"IVA"` |
| Colombia | `0.19` | `"IVA"` |
| Chile | `0.19` | `"IVA"` |
| Perú | `0.18` | `"IGV"` |
| India | `0.18` | `"GST"` |
| Reino Unido | `0.20` | `"VAT"` |
| Alemania | `0.19` | `"MwSt"` |
| Francia | `0.20` | `"TVA"` |
| Suecia | `0.25` | `"Moms"` |
| Australia | `0.10` | `"GST"` |
| Panamá | `0.07` | `"ITBMS"` |
| Canarias | `0.07` | `"IGIC"` |
| Exento / exportación | `0` (o omitir) | *(no se muestra)* |
| Tasa personalizada | `0.085` | `"Impuesto municipal"` |

**La API acepta cualquier valor entre `0` y `1`**, no solo los de la tabla. Esa lista es solo orientativa para el README.

### Otros
| Campo | Tipo | Descripción |
|-------|------|-------------|
| `notes` | string | Notas al pie de la factura |
| `footer_text` | string | Texto personalizado del footer |

## Ejemplos de uso

### México — IVA 16%
```json
{
  "invoice_number": "MX-001",
  "currency": "MXN",
  "client_name": "Cliente S.A. de C.V.",
  "client_tax_id": "ABC123456XYZ",
  "client_tax_id_label": "RFC",
  "items": [{"description": "Servicio", "quantity": 1, "unit_price": 1000}],
  "tax_rate": 0.16,
  "tax_label": "IVA"
}
