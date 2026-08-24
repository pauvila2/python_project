"""
claude_client.py — Extracción de datos de facturas con Gemini 1.5 Flash.
Incluye una capa de pseudonimización para texto plano.
Mantiene el mismo nombre de archivo para no tocar main.py ni nada más.
"""

import re
import os
import httpx
import json

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL   = "gemini-3.6-flash"
GEMINI_URL     = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

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


# ─── Llamada a Gemini ─────────────────────────────────────────────────────────

async def extract_invoice_data(parts: list) -> dict:
    """
    parts: lista de dicts en formato Gemini:
      - texto:  {"text": "..."}
      - imagen: {"inline_data": {"mime_type": "image/jpeg", "data": "<b64>"}}
      - pdf:    {"inline_data": {"mime_type": "application/pdf", "data": "<b64>"}}
    """
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "maxOutputTokens": 1000,
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            headers={"Content-Type": "application/json"},
            json=payload,
        )
    resp.raise_for_status()
    data = resp.json()

    raw = (
        data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
    )
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


async def extract_from_text(text: str) -> dict:
    tokenized, token_map = pseudonymize(text)
    extracted = await extract_invoice_data([{"text": tokenized}])
    return {k: reidentify(v, token_map) for k, v in extracted.items()}


async def extract_from_base64(b64data: str, mime_type: str) -> dict:
    # PDFs e imágenes usan inline_data en Gemini
    parts = [
        {"inline_data": {"mime_type": mime_type, "data": b64data}},
        {"text": "Extrae los datos de esta factura."},
    ]
    return await extract_invoice_data(parts)
