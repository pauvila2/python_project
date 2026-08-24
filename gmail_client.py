"""
gmail_client.py
OAuth 2.0 con Gmail + lectura de emails + facturas
con o sin archivos adjuntos.
"""

import os
import base64
import re
import html
import httpx

from datetime import datetime, timedelta
from urllib.parse import urlencode
from sqlalchemy.orm import Session

from database import GmailToken


# ============================================================
# CONFIGURACIÓN
# ============================================================

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

SCOPES = "https://www.googleapis.com/auth/gmail.readonly"


INVOICE_KEYWORDS = [
    "factura",
    "facturación",
    "facturacion",
    "invoice",
    "invoices",
    "receipt",
    "recibo",
]


EXCLUDED_KEYWORDS = [
    "reunión",
    "reunion",
    "meeting",
    "information about your new google account",
    "new google account",
    "google account",
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


# ============================================================
# TEXTO
# ============================================================

def normalize_text(value: str) -> str:

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
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return value


def decode_base64url(data: str) -> bytes:

    if not data:
        return b""

    try:
        padding = "=" * (-len(data) % 4)

        return base64.urlsafe_b64decode(
            data + padding
        )

    except Exception:
        return b""


def decode_gmail_text(data: str) -> str:

    raw = decode_base64url(data)

    if not raw:
        return ""

    return raw.decode(
        "utf-8",
        errors="replace"
    )


def html_to_text(value: str) -> str:

    if not value:
        return ""

    value = re.sub(
        r"<\s*(br|/p|/div|/tr|/li)\s*/?\s*>",
        "\n",
        value,
        flags=re.IGNORECASE
    )

    value = re.sub(
        r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>",
        " ",
        value,
        flags=re.IGNORECASE | re.DOTALL
    )

    value = re.sub(
        r"<[^>]+>",
        " ",
        value
    )

    value = html.unescape(value)

    value = re.sub(
        r"[ \t]+",
        " ",
        value
    )

    value = re.sub(
        r"\n\s*\n+",
        "\n\n",
        value
    )

    return value.strip()


def get_header(
    headers: list[dict],
    wanted_name: str
) -> str:

    wanted = wanted_name.lower()

    for item in headers or []:

        name = (
            item.get("name", "")
            .strip()
            .lower()
        )

        if name == wanted:
            return item.get(
                "value",
                ""
            )

    return ""


# ============================================================
# CUERPO DEL EMAIL
# ============================================================

def extract_body_from_payload(
    payload: dict
) -> str:
    """
    Extrae el cuerpo completo del email.

    Soporta:
    - text/plain
    - text/html
    - multipart/alternative
    - multipart/mixed
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

            text = decode_gmail_text(data)

            if mime_type == "text/plain":

                if text.strip():
                    plain_parts.append(text)

            elif mime_type == "text/html":

                if text.strip():
                    html_parts.append(text)

        for child in (
            part.get("parts") or []
        ):
            walk(child)

    walk(payload)

    # Preferimos texto plano.
    if plain_parts:

        return "\n\n".join(
            plain_parts
        ).strip()

    # Si solo existe HTML.
    if html_parts:

        return "\n\n".join(
            html_to_text(x)
            for x in html_parts
        ).strip()

    return ""


# ============================================================
# DETECCIÓN DE FACTURAS
# ============================================================

def looks_like_invoice(
    subject: str = "",
    snippet: str = "",
    body: str = ""
) -> bool:

    subject_n = normalize_text(subject)
    snippet_n = normalize_text(snippet)
    body_n = normalize_text(body)

    # --------------------------------------------------------
    # Primero comprobamos si el asunto contiene "factura",
    # "invoice", etc.
    # --------------------------------------------------------

    has_invoice_subject = any(
        normalize_text(keyword) in subject_n
        for keyword in INVOICE_KEYWORDS
    )

    # --------------------------------------------------------
    # Si el asunto es claramente un correo que no queremos,
    # lo descartamos.
    # --------------------------------------------------------

    if not has_invoice_subject:

        for keyword in EXCLUDED_KEYWORDS:

            if normalize_text(keyword) in subject_n:
                return False

    # --------------------------------------------------------
    # Si el asunto contiene factura, aceptamos directamente.
    #
    # Esto incluye:
    #
    # Factura agosto - Ref. 7854
    # Factura FAC-000918 - Servicios profesionales agosto
    # Factura 2026-3317 - Servicios de mantenimiento
    # --------------------------------------------------------

    if has_invoice_subject:
        return True

    # --------------------------------------------------------
    # Si no aparece en asunto, comprobamos snippet y cuerpo.
    # --------------------------------------------------------

    combined = (
        snippet_n
        + "\n"
        + body_n
    )

    for keyword in INVOICE_KEYWORDS:

        if normalize_text(keyword) in combined:
            return True

    return False


# ============================================================
# ADJUNTOS
# ============================================================

def is_invoice_attachment(
    mime_type: str,
    filename: str
) -> bool:

    mime = (
        mime_type or ""
    ).lower()

    name = (
        filename or ""
    ).lower()

    if mime == "application/pdf":
        return True

    if mime.startswith("image/"):
        return True

    extensions = (
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".heic",
    )

    return name.endswith(
        extensions
    )


def payload_has_invoice_attachment(
    payload: dict
) -> bool:

    if not payload:
        return False

    found = False

    def walk(part: dict):

        nonlocal found

        if found:
            return

        mime = part.get(
            "mimeType",
            ""
        )

        filename = part.get(
            "filename",
            ""
        )

        if is_invoice_attachment(
            mime,
            filename
        ):
            found = True
            return

        for child in (
            part.get("parts") or []
        ):
            walk(child)

    walk(payload)

    return found


# ============================================================
# OAUTH
# ============================================================

def get_auth_url() -> str:

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }

    return (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode(params)
    )


async def exchange_code_for_token(
    code: str
) -> dict:

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    response.raise_for_status()

    return response.json()


async def refresh_access_token(
    refresh_token: str
) -> dict:

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

    response.raise_for_status()

    return response.json()


# ============================================================
# TOKENS
# ============================================================

def save_token(
    db: Session,
    token_data: dict
):

    record = (
        db.query(GmailToken)
        .first()
    )

    expiry = None

    if token_data.get("expires_in"):

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

        if token_data.get("refresh_token"):

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

            async with httpx.AsyncClient(
                timeout=15.0
            ) as client:

                response = await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={
                        "token": token
                    },
                    headers={
                        "content-type":
                        "application/x-www-form-urlencoded"
                    },
                )

                if response.status_code not in (
                    200,
                    400
                ):
                    response.raise_for_status()

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


# ============================================================
# LISTAR EMAILS
# ============================================================

async def list_invoice_emails(
    access_token: str,
    max_results: int = 50
) -> list[dict]:

    """
    Busca emails recientes.

    NO usamos has:attachment porque las facturas
    pueden estar directamente en el cuerpo del correo.
    """

    headers = {
        "Authorization":
        f"Bearer {access_token}"
    }

    query = "newer_than:1y"

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        response = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={
                "q": query,
                "maxResults": max_results,
                "includeSpamTrash": "false",
            },
        )

        response.raise_for_status()

        messages = (
            response.json()
            .get("messages", [])
        )

        results = []

        for message in messages:

            message_id = message.get("id")

            if not message_id:
                continue

            try:

                detail_response = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                    + message_id,
                    headers=headers,
                    params={
                        "format": "full"
                    },
                )

                detail_response.raise_for_status()

                detail = detail_response.json()

                payload = (
                    detail.get("payload")
                    or {}
                )

                message_headers = (
                    payload.get("headers")
                    or []
                )

                subject = get_header(
                    message_headers,
                    "Subject"
                )

                sender = get_header(
                    message_headers,
                    "From"
                )

                date = get_header(
                    message_headers,
                    "Date"
                )

                snippet = detail.get(
                    "snippet",
                    ""
                )

                body = extract_body_from_payload(
                    payload
                )

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
                    "has_attachment": (
                        payload_has_invoice_attachment(
                            payload
                        )
                    ),
                })

                if len(results) >= max_results:
                    break

            except Exception as e:

                print(
                    "WARNING: Error leyendo mensaje "
                    f"{message_id}: {e}"
                )

    return results


# ============================================================
# CONTENIDO COMPLETO DEL EMAIL
# ============================================================

async def get_email_content(
    access_token: str,
    message_id: str
) -> dict:

    headers = {
        "Authorization":
        f"Bearer {access_token}"
    }

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        response = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
            + message_id,
            headers=headers,
            params={
                "format": "full"
            },
        )

        response.raise_for_status()

        message = response.json()

        payload = (
            message.get("payload")
            or {}
        )

        message_headers = (
            payload.get("headers")
            or []
        )

        subject = get_header(
            message_headers,
            "Subject"
        )

        sender = get_header(
            message_headers,
            "From"
        )

        date = get_header(
            message_headers,
            "Date"
        )

        # ----------------------------------------------------
        # CUERPO DEL EMAIL
        # ----------------------------------------------------

        body = extract_body_from_payload(
            payload
        )

        # Último recurso: snippet.
        if not body.strip():

            body = message.get(
                "snippet",
                ""
            )

        # ----------------------------------------------------
        # ADJUNTOS
        # ----------------------------------------------------

        attachments = []

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

            data = part_body.get(
                "data"
            )

            attachment_id = part_body.get(
                "attachmentId"
            )

            if is_invoice_attachment(
                mime_type,
                filename
            ):

                data_b64 = ""

                if data:

                    data_b64 = data

                elif attachment_id:

                    try:

                        attachment_response = await client.get(
                            "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                            + message_id
                            + "/attachments/"
                            + attachment_id,
                            headers=headers,
                        )

                        attachment_response.raise_for_status()

                        attachment_json = (
                            attachment_response.json()
                        )

                        data_b64 = (
                            attachment_json.get(
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

        # Los dos nombres para que main.py
        # pueda utilizar el cuerpo.
        "body": body,
        "body_text": body,

        "attachments": attachments,
    }