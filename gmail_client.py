"""
gmail_client.py — OAuth 2.0 con Gmail + lectura de emails con facturas.
"""

import os
import base64
import re
import httpx

from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from database import GmailToken


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

SCOPES = "https://www.googleapis.com/auth/gmail.readonly"


# Palabras que indican que un email puede ser una factura.
#
# IMPORTANTE:
# No usamos "has:attachment", porque también queremos encontrar
# facturas cuyo contenido está directamente en el cuerpo del email.
GMAIL_KEYWORDS = [
    "factura",
    "facturación",
    "facturacion",
    "invoice",
    "invoices",
    "receipt",
    "recibo",
]


# Palabras que normalmente NO queremos considerar facturas.
#
# Esto ayuda a evitar cosas como:
# "Reunión del próximo martes"
# "Information about your new Google Account"
GMAIL_EXCLUDE_KEYWORDS = [
    "reunión",
    "reunion",
    "meeting",
    "google account",
    "new google account",
    "welcome to google",
    "bienvenido a google",
    "verification",
    "verificación",
    "verify your",
    "confirm your account",
    "password",
    "contraseña",
    "security alert",
    "alerta de seguridad",
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def normalize_text(value: str) -> str:
    """
    Normaliza texto para poder buscar palabras sin depender de
    mayúsculas/minúsculas ni acentos.
    """

    if not value:
        return ""

    value = str(value).lower()

    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return value


def decode_base64url(data: str) -> bytes:
    """
    Gmail utiliza base64url.
    """

    if not data:
        return b""

    padding = "=" * (-len(data) % 4)

    return base64.urlsafe_b64decode(
        data + padding
    )


def decode_gmail_text(data: str) -> str:
    """
    Decodifica un cuerpo de texto de Gmail.
    """

    if not data:
        return ""

    try:
        raw = decode_base64url(data)

        return raw.decode(
            "utf-8",
            errors="replace"
        )

    except Exception:
        return ""


def html_to_text(html: str) -> str:
    """
    Conversión sencilla HTML -> texto.
    No necesitamos un parser HTML completo para extraer
    el contenido de una factura.
    """

    if not html:
        return ""

    text = html

    # Saltos de línea
    text = re.sub(
        r"<\s*(br|/p|/div|/tr|/li)\s*>",
        "\n",
        text,
        flags=re.IGNORECASE
    )

    # Eliminar scripts/styles
    text = re.sub(
        r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL
    )

    # Eliminar etiquetas
    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    # Entidades HTML comunes
    text = (
        text
        .replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )

    # Limpiar espacios
    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    text = re.sub(
        r"\n\s*\n+",
        "\n\n",
        text
    )

    return text.strip()


def extract_body_from_payload(payload: dict) -> str:
    """
    Extrae texto plano o HTML de un payload de Gmail.

    Gmail puede tener estructuras como:

    multipart/mixed
      multipart/alternative
        text/plain
        text/html
      application/pdf

    Por eso recorremos recursivamente todas las partes.
    """

    if not payload:
        return ""

    plain_parts = []
    html_parts = []

    def walk(part: dict):
        mime_type = (
            part.get("mimeType", "")
            or ""
        ).lower()

        body = part.get("body") or {}

        data = body.get("data")

        if data:

            decoded = decode_gmail_text(data)

            if mime_type == "text/plain":

                if decoded.strip():
                    plain_parts.append(decoded)

            elif mime_type == "text/html":

                if decoded.strip():
                    html_parts.append(decoded)

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)

    # Preferimos texto plano.
    if plain_parts:

        return "\n\n".join(
            plain_parts
        ).strip()

    # Si solo existe HTML, lo convertimos a texto.
    if html_parts:

        return "\n\n".join(
            html_to_text(x)
            for x in html_parts
        ).strip()

    return ""


def get_header(
    headers: list[dict],
    name: str
) -> str:
    """
    Obtiene un header de Gmail sin depender de mayúsculas/minúsculas.
    """

    wanted = name.lower()

    for header in headers or []:

        if (
            header.get("name", "")
            .lower()
            == wanted
        ):
            return header.get(
                "value",
                ""
            )

    return ""


def looks_like_invoice(
    subject: str,
    snippet: str = "",
    body: str = ""
) -> bool:
    """
    Decide si un email parece relacionado con una factura.

    Busca las palabras clave en:
      - asunto
      - snippet
      - cuerpo

    Y descarta algunos falsos positivos conocidos.
    """

    combined = normalize_text(
        f"{subject}\n{snippet}\n{body}"
    )

    if not combined:
        return False

    # Primero descartamos asuntos/contenidos claramente irrelevantes.
    for keyword in GMAIL_EXCLUDE_KEYWORDS:

        if normalize_text(keyword) in combined:
            return False

    # Después buscamos palabras de factura.
    for keyword in GMAIL_KEYWORDS:

        normalized_keyword = normalize_text(
            keyword
        )

        if normalized_keyword in combined:
            return True

    return False


def has_invoice_attachment(
    payload: dict
) -> bool:
    """
    Comprueba si el email contiene PDF o imagen.
    """

    if not payload:
        return False

    found = False

    def walk(part: dict):

        nonlocal found

        if found:
            return

        mime_type = (
            part.get("mimeType", "")
            or ""
        ).lower()

        filename = (
            part.get("filename", "")
            or ""
        ).lower()

        if (
            mime_type == "application/pdf"
            or mime_type.startswith("image/")
            or filename.endswith(".pdf")
            or filename.endswith(".jpg")
            or filename.endswith(".jpeg")
            or filename.endswith(".png")
            or filename.endswith(".webp")
        ):
            found = True
            return

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)

    return found


# ─────────────────────────────────────────────────────────────────────────────
# OAUTH
# ─────────────────────────────────────────────────────────────────────────────

def get_auth_url() -> str:

    params = (
        f"client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&access_type=offline"
        f"&prompt=consent"
    )

    return (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + params
    )


async def exchange_code_for_token(
    code: str
) -> dict:

    async with httpx.AsyncClient() as client:

        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    resp.raise_for_status()

    return resp.json()


async def refresh_access_token(
    refresh_token: str
) -> dict:

    async with httpx.AsyncClient() as client:

        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

    resp.raise_for_status()

    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN EN DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def save_token(
    db: Session,
    token_data: dict
):

    record = (
        db.query(GmailToken)
        .first()
    )

    expiry = None

    if "expires_in" in token_data:

        expiry = (
            datetime.utcnow()
            + timedelta(
                seconds=token_data["expires_in"] - 60
            )
        )

    if record:

        record.access_token = (
            token_data["access_token"]
        )

        if "refresh_token" in token_data:

            record.refresh_token = (
                token_data["refresh_token"]
            )

        record.token_expiry = expiry

    else:

        record = GmailToken(
            access_token=token_data[
                "access_token"
            ],
            refresh_token=token_data.get(
                "refresh_token",
                ""
            ),
            token_expiry=expiry,
        )

        db.add(record)

    db.commit()


async def revoke_token(
    db: Session
) -> bool:
    """
    Revoca el acceso de Gmail en Google y elimina
    el token almacenado localmente.
    """

    record = (
        db.query(GmailToken)
        .first()
    )

    if not record:
        return True

    token = (
        record.refresh_token
        or record.access_token
    )

    if token:

        try:

            async with httpx.AsyncClient() as client:

                resp = await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={
                        "token": token
                    },
                    headers={
                        "content-type":
                        "application/x-www-form-urlencoded"
                    },
                )

                if resp.status_code not in (
                    200,
                    400
                ):
                    resp.raise_for_status()

        except Exception as e:

            print(
                "WARNING: No se pudo revocar "
                f"el token de Google: {e}"
            )

    db.delete(record)

    db.commit()

    return True


async def get_valid_access_token(
    db: Session
) -> str | None:

    record = (
        db.query(GmailToken)
        .first()
    )

    if not record:
        return None

    if (
        record.token_expiry
        and datetime.utcnow()
        >= record.token_expiry
    ):

        new_token = await refresh_access_token(
            record.refresh_token
        )

        save_token(
            db,
            new_token
        )

        return new_token[
            "access_token"
        ]

    return record.access_token


# ─────────────────────────────────────────────────────────────────────────────
# LISTAR EMAILS DE GMAIL
# ─────────────────────────────────────────────────────────────────────────────

async def list_invoice_emails(
    access_token: str,
    max_results: int = 20
) -> list[dict]:
    """
    Busca emails relacionados con facturas.

    IMPORTANTE:
    No usamos "has:attachment".

    De esta manera también aparecen emails como:

      Factura agosto - Ref. 7854
      Factura FAC-000918 - Servicios profesionales agosto
      Factura 2026-3317 - Servicios de mantenimiento

    aunque no tengan PDF.
    """

    # Buscamos primero por palabras en Gmail.
    #
    # No exigimos attachment.
    query_keywords = " OR ".join(
        f'"{kw}"'
        for kw in GMAIL_KEYWORDS
    )

    query = f"({query_keywords})"

    headers = {
        "Authorization":
        f"Bearer {access_token}"
    }

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={
                "q": query,
                "maxResults": max_results,
            },
        )

        resp.raise_for_status()

        messages = (
            resp.json()
            .get(
                "messages",
                []
            )
        )

        results = []

        for msg in messages:

            message_id = msg.get("id")

            if not message_id:
                continue

            detail = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                + message_id,
                headers=headers,
                params={
                    "format": "full",
                },
            )

            detail.raise_for_status()

            d = detail.json()

            payload = (
                d.get("payload")
                or {}
            )

            headers_list = (
                payload.get("headers")
                or []
            )

            subject = get_header(
                headers_list,
                "Subject"
            )

            sender = get_header(
                headers_list,
                "From"
            )

            date = get_header(
                headers_list,
                "Date"
            )

            snippet = d.get(
                "snippet",
                ""
            )

            body = extract_body_from_payload(
                payload
            )

            # Filtro adicional local.
            #
            # Gmail ya hizo una primera búsqueda,
            # pero aquí verificamos el contenido real.
            if not looks_like_invoice(
                subject,
                snippet,
                body
            ):
                continue

            results.append({
                "id": message_id,
                "subject": (
                    subject
                    or "(sin asunto)"
                ),
                "from": sender,
                "date": date,
                "snippet": snippet,
                "has_attachment": has_invoice_attachment(
                    payload
                ),
            })

            if len(results) >= max_results:
                break

    return results


# ─────────────────────────────────────────────────────────────────────────────
# OBTENER CONTENIDO COMPLETO DEL EMAIL
# ─────────────────────────────────────────────────────────────────────────────

async def get_email_content(
    access_token: str,
    message_id: str
) -> dict:
    """
    Obtiene el contenido completo de un email.

    Devuelve:

        {
            "id": "...",
            "subject": "...",
            "from": "...",
            "date": "...",
            "body": "...",
            "attachments": [...]
        }

    El body funciona tanto para emails:
      - text/plain
      - text/html
      - multipart/alternative
      - multipart/mixed

    Y los adjuntos PDF/imágenes se descargan.
    """

    headers = {
        "Authorization":
        f"Bearer {access_token}"
    }

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
            + message_id,
            headers=headers,
            params={
                "format": "full"
            },
        )

        resp.raise_for_status()

        message = resp.json()

        payload = (
            message.get("payload")
            or {}
        )

        headers_list = (
            payload.get("headers")
            or []
        )

        subject = get_header(
            headers_list,
            "Subject"
        )

        sender = get_header(
            headers_list,
            "From"
        )

        date = get_header(
            headers_list,
            "Date"
        )

        body = extract_body_from_payload(
            payload
        )

        attachments = []

        # ─────────────────────────────────────────────────────────
        # Recorrer adjuntos
        # ─────────────────────────────────────────────────────────

        async def walk_parts(
            part: dict
        ):

            mime_type = (
                part.get("mimeType", "")
                or ""
            )

            filename = (
                part.get("filename", "")
                or ""
            )

            part_body = (
                part.get("body")
                or {}
            )

            attachment_id = part_body.get(
                "attachmentId"
            )

            inline_data = part_body.get(
                "data"
            )

            is_pdf = (
                mime_type.lower()
                == "application/pdf"
            )

            is_image = mime_type.lower().startswith(
                "image/"
            )

            is_supported = (
                is_pdf
                or is_image
                or filename.lower().endswith(
                    ".pdf"
                )
                or filename.lower().endswith(
                    ".jpg"
                )
                or filename.lower().endswith(
                    ".jpeg"
                )
                or filename.lower().endswith(
                    ".png"
                )
                or filename.lower().endswith(
                    ".webp"
                )
            )

            if is_supported:

                data_b64 = ""

                # Caso 1:
                # Gmail devuelve el contenido directamente.
                if inline_data:

                    data_b64 = inline_data

                # Caso 2:
                # Gmail devuelve attachmentId.
                elif attachment_id:

                    try:

                        att_resp = await client.get(
                            "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                            + message_id
                            + "/attachments/"
                            + attachment_id,
                            headers=headers,
                        )

                        att_resp.raise_for_status()

                        att_json = (
                            att_resp.json()
                        )

                        data_b64 = (
                            att_json.get(
                                "data",
                                ""
                            )
                        )

                    except Exception as e:

                        print(
                            "WARNING: Error descargando "
                            f"adjunto {filename}: {e}"
                        )

                if data_b64:

                    attachments.append({
                        "filename": filename,
                        "mime_type": mime_type,
                        "data_b64": data_b64,
                    })

            for child in (
                part.get("parts")
                or []
            ):

                await walk_parts(
                    child
                )

        await walk_parts(
            payload
        )

    return {
        "id": message_id,
        "subject": subject,
        "from": sender,
        "date": date,
        "body": body,
        "attachments": attachments,
    }