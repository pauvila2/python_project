"""
claude_client.py — Extracción de datos de facturas con Claude AI.
Incluye una capa de pseudonimización para texto plano.
"""

import re
import os
import httpx
import json

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Eres un motor de extraccion de datos de facturas. 
Analiza el contenido y devuelve UNICAMENTE un objeto JSON valido, sin texto adicional, 
sin backticks de markdown, con estas claves exactas:
  proveedor (string), nif_proveedor (string), fecha (string formato DD/MM/AAAA),
  concepto (string breve), base_imponible (numero), iva_porcentaje (numero),
  iva_importe (numero), total (numero), moneda (string, por defecto EUR).
Si un dato no aparece, usa null. No inventes datos que no esten en el contenido."""


# ─── Pseudonimización ─────────────────────────────────────────────────────────

def pseudonymize(text: str) -> tuple[str, dict]:
    token_map = {}
    counters = {"NIF": 0, "IBAN": 0, "EMAIL": 0, "TEL": 0}
    out = text

    patterns = [
        ("IBAN",  r"\bES\d{2}\s?\d{4}\s?\d{4}\s?\d{2}\s?\d{10}\b"),
        ("NIF",   r"\b[0-9]{8}[A-Za-z]\b"),
        ("NIF",   r"\b[A-Za-z][0-9]{7}[A-Za-z0-9]\b"),
        ("EMAIL", r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b"),
        ("TEL",   r"\b(?:\+34\s?)?[6789]\d{8}\b"),
    ]

    for ptype, pattern in patterns:
        def replacer(m, pt=ptype):
            match = m.group(0)
            for tok, val in token_map.items():
                if val == match:
                    return tok
            counters[pt] += 1
            tok = f"{pt}_{counters[pt]:02d}"
            token_map[tok] = match
            return tok
        out = re.sub(pattern, replacer, out)

    return out, token_map


def reidentify(value, token_map: dict):
    if not isinstance(value, str):
        return value
    for tok, real in token_map.items():
        value = value.replace(tok, real)
    return value


# ─── Llamada a Claude ─────────────────────────────────────────────────────────

async def extract_invoice_data(content_blocks: list) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": content_blocks}],
            },
        )
    resp.raise_for_status()
    data = resp.json()
    raw = "".join(b.get("text", "") for b in data.get("content", [])).strip()
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


async def extract_from_text(text: str) -> dict:
    tokenized, token_map = pseudonymize(text)
    extracted = await extract_invoice_data([{"type": "text", "text": tokenized}])
    return {k: reidentify(v, token_map) for k, v in extracted.items()}


async def extract_from_base64(b64data: str, mime_type: str) -> dict:
    if mime_type.startswith("image/"):
        block = {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64data}}
    else:
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64data}}

    return await extract_invoice_data([
        block,
        {"type": "text", "text": "Extrae los datos de esta factura."}
    ])
