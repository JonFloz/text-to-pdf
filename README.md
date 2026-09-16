# PDF Generator API

Genera facturas en PDF desde JSON. **47 monedas soportadas** con formato regional correcto.

## Endpoints

### GET /health
Estado del servicio.

### GET /currencies
Lista todas las monedas soportadas.

### POST /generate/invoice
Genera una factura PDF.

**Body:**
- `client_name` (req): Nombre del cliente
- `invoice_number` (req): Número de factura
- `items` (req): Array de items (máx 200)
  - `description`, `quantity`, `unit_price`
- `currency` (opc, def USD): Código ISO 4217
- `tax_rate` (opc, def 0.16): 0.16 = 16%
- `tax_label` (opc, def "IVA"): Nombre del impuesto local (GST, Moms, VAT…)
- `date`, `title`, `notes`, `client_id` (opc)

**Respuesta:** `application/pdf` binario.

## Errores comunes

| Código | Motivo |
|--------|--------|
| 422 | Moneda no soportada / campo inválido |
| 429 | Rate limit (120/min por IP) |
| 500 | Error interno (reintentar) |

## Ejemplo curl

```bash
curl -X POST "https://TU-URL.onrender.com/generate/invoice" \
  -H "X-RapidAPI-Key: TU_KEY" \
  -H "Content-Type: application/json" \
  -d '{"client_name":"Juan Pérez","invoice_number":"001","currency":"MXN","items":[{"description":"Servicio","quantity":1,"unit_price":1500}]}' \
  --output factura.pdf