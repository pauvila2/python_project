"""
holded_client.py — Integración con la API de Holded.
"""
import httpx

HOLDED_BASE = "https://api.holded.com/api/invoices/v1"


def _headers(api_key: str) -> dict:
    return {"key": api_key, "Content-Type": "application/json"}


async def test_connection(api_key: str) -> bool:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.holded.com/api/contacts/v1/contacts",
            headers=_headers(api_key),
            params={"limit": 1},
        )
    return resp.status_code == 200


async def create_purchase_invoice(api_key: str, expediente: dict) -> dict:
    import time
    from datetime import datetime

    fecha_ts = None
    if expediente.get("fecha"):
        try:
            dt = datetime.strptime(expediente["fecha"], "%d/%m/%Y")
            fecha_ts = int(dt.timestamp())
        except ValueError:
            pass

    subtotal = expediente.get("base_imponible") or 0
    iva_pct  = expediente.get("iva_porcentaje") or 21

    items = [{
        "name":  expediente.get("concepto") or "Factura",
        "units": 1,
        "price": subtotal,
        "tax":   iva_pct,
    }]

    payload = {
        "contactName": expediente.get("proveedor") or "Proveedor desconocido",
        "contactNif":  expediente.get("nif_proveedor") or "",
        "date":        fecha_ts or int(time.time()),
        "notes":       f"Importado automáticamente. NIF: {expediente.get('nif_proveedor', '')}",
        "items":       items,
        "currency":    expediente.get("moneda") or "EUR",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{HOLDED_BASE}/purchaseinvoices",
            headers=_headers(api_key),
            json=payload,
        )
    resp.raise_for_status()
    return resp.json()


async def get_contacts(api_key: str, search: str = "") -> list:
    params = {"limit": 20}
    if search:
        params["name"] = search
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.holded.com/api/contacts/v1/contacts",
            headers=_headers(api_key),
            params=params,
        )
    resp.raise_for_status()
    return resp.json()
