"""
gmail_client.py — OAuth 2.0 con Gmail + lectura de emails con facturas.
"""

import os
import base64
import re
import httpx

from datetime import datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from database import GmailToken


# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

SCOPES = "https://www.googleapis.com/auth/gmail.readonly"


# Palabras que identifican una factura.
# Se comparan sin distinguir mayúsculas/minúsculas.
GMAIL_INVOICE_KEYWORDS = [
    "factura",
    "facturación",
    "facturacion",
    "invoice",
    "facture",
    "rechnung",
]


# Palabras que NO queremos considerar facturas.
GMAIL_EXCLUDED_KEYWORDS = [
    "reunión",
    "reunion",
    "meeting",
    "google account",
    "new google account",
    "welcome to google",
    "bienvenido a google",
    "verify your email",
    "verifica tu correo",
    "security alert",
    "alerta de seguridad",
    "password",
    "contraseña",
    "calendar",
    "calendario",
]


# ─────────────────────────────────────────────────────────────
# OAUTH
# ─────────────────────────────────────────────────────────────

def get_auth_url() -> str:
    """
    Genera la URL de autorización de Google.
    """

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


async def exchange_code_for_token(code: str) -> dict:
    """
    Intercambia el authorization code de Google por tokens.
    """

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


async def refresh_access_token(refresh_token: str) -> dict:
    """
    Renueva el access token utilizando el refresh token.
    """

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


# ─────────────────────────────────────────────────────────────
# TOKENS / BASE DE DATOS
# ─────────────────────────────────────────────────────────────

def save_token(
    db: Session,
    token_data: dict
):
    """
    Guarda o actualiza los tokens de Gmail.
    """

    record = db.query(GmailToken).first()

    expiry = None

    if "expires_in" in token_data:

        expiry = (
            datetime.utcnow()
            + timedelta(
                seconds=token_data["expires_in"] - 60
            )
        )

    if record:

        record.access_token = token_data["access_token"]

        if token_data.get("refresh_token"):
            record.refresh_token = token_data["refresh_token"]

        record.token_expiry = expiry

    else:

        record = GmailToken(
            access_token=token_data["access_token"],
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

    record = db.query(GmailToken).first()

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

                # Google puede devolver 400 si el token
                # ya estaba revocado o no era válido.
                if resp.status_code not in (200, 400):
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
    """
    Devuelve un access token válido.
    Si está caducado, intenta renovarlo.
    """

    record = db.query(GmailToken).first()

    if not record:
        return None

    if (
        record.token_expiry
        and datetime.utcnow() >= record.token_expiry
    ):

        if not record.refresh_token:
            return None

        new_token = await refresh_access_token(
            record.refresh_token
        )

        save_token(
            db,
            new_token
        )

        return new_token["access_token"]

    return record.access_token


# ─────────────────────────────────────────────────────────────
# HELPERS PARA EMAILS
# ─────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """
    Normaliza texto para buscar palabras sin distinguir
    mayúsculas/minúsculas ni algunos acentos.
    """

    if not text:
        return ""

    text = text.lower()

    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def looks_like_invoice(
    subject: str,
    snippet: str = "",
    sender: str = ""
) -> bool:
    """
    Determina si un email parece ser una factura.

    No requiere PDF.

    Puede detectar facturas cuyo contenido está únicamente
    en el cuerpo del email.
    """

    subject_n = normalize_text(subject)
    snippet_n = normalize_text(snippet)
    sender_n = normalize_text(sender)

    combined = (
        subject_n
        + " "
        + snippet_n
        + " "
        + sender_n
    )

    # Primero descartamos mensajes claramente irrelevantes.
    for excluded in GMAIL_EXCLUDED_KEYWORDS:

        if normalize_text(excluded) in combined:
            return False

    # El asunto es la señal principal.
    for keyword in GMAIL_INVOICE_KEYWORDS:

        if normalize_text(keyword) in subject_n:
            return True

    # Si no aparece en asunto, permitimos que aparezca
    # en el contenido del email.
    for keyword in GMAIL_INVOICE_KEYWORDS:

        if normalize_text(keyword) in snippet_n:
            return True

    return False


def decode_base64url(data: str) -> str:
    """
    Decodifica contenido base64url utilizado por Gmail.
    """

    if not data:
        return ""

    try:

        padding = "=" * (
            -len(data) % 4
        )

        raw = base64.urlsafe_b64decode(
            data + padding
        )

        return raw.decode(
            "utf-8",
            errors="replace"
        )

    except Exception:

        return ""


def clean_html(html: str) -> str:
    """
    Convierte HTML básico del email en texto.
    """

    if not html:
        return ""

    html = re.sub(
        r"<br\s*/?>",
        "\n",
        html,
        flags=re.IGNORECASE
    )

    html = re.sub(
        r"</p\s*>",
        "\n",
        html,
        flags=re.IGNORECASE
    )

    html = re.sub(
        r"<[^>]+>",
        " ",
        html
    )

    html = html.replace(
        "&nbsp;",
        " "
    )

    html = html.replace(
        "&amp;",
        "&"
    )

    html = html.replace(
        "&lt;",
        "<"
    )

    html = html.replace(
        "&gt;",
        ">"
    )

    html = re.sub(
        r"[ \t]+",
        " ",
        html
    )

    html = re.sub(
        r"\n\s*\n+",
        "\n",
        html
    )

    return html.strip()


def get_header(
    headers: list[dict],
    name: str
) -> str:

    name_lower = name.lower()

    for h in headers:

        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")

    return ""


def has_attachments(
    payload: dict
) -> bool:
    """
    Comprueba recursivamente si el email tiene adjuntos.
    """

    if not payload:
        return False

    filename = payload.get(
        "filename",
        ""
    )

    body = payload.get(
        "body",
        {}
    )

    if filename and body.get("attachmentId"):
        return True

    for part in payload.get(
        "parts",
        []
    ):

        if has_attachments(part):
            return True

    return False


# ─────────────────────────────────────────────────────────────
# LISTAR EMAILS DE FACTURAS
# ─────────────────────────────────────────────────────────────

async def list_invoice_emails(
    access_token: str,
    max_results: int = 20
) -> list[dict]:
    """
    Lista emails que parecen facturas.

    IMPORTANTE:

    No utilizamos:

        has:attachment

    porque muchas facturas llegan directamente
    en el cuerpo del email sin PDF.

    Primero pedimos emails recientes y después
    comprobamos asunto + snippet.
    """

    headers = {
        "Authorization": (
            f"Bearer {access_token}"
        )
    }

    # Buscamos términos generales en Gmail.
    # NO exigimos adjunto.
    #
    # Gmail hace la primera criba y nuestro código
    # hace después la criba definitiva.
    gmail_query = (
        'newer_than:1y '
        '(factura OR invoice OR facturación OR facturacion)'
    )

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        resp = await client.get(
            "https://gmail.googleapis.com/"
            "gmail/v1/users/me/messages",
            headers=headers,
            params={
                "q": gmail_query,
                "maxResults": max_results * 3,
            },
        )

        resp.raise_for_status()

        messages = resp.json().get(
            "messages",
            []
        )

        results = []

        for msg in messages:

            if len(results) >= max_results:
                break

            message_id = msg.get("id")

            if not message_id:
                continue

            try:

                detail = await client.get(
                    "https://gmail.googleapis.com/"
                    f"gmail/v1/users/me/messages/{message_id}",
                    headers=headers,
                    params={
                        "format": "metadata",
                        "metadataHeaders": [
                            "Subject",
                            "From",
                            "Date",
                        ],
                    },
                )

                detail.raise_for_status()

            except Exception as e:

                print(
                    "WARNING: Error leyendo "
                    f"email {message_id}: {e}"
                )

                continue

            data = detail.json()

            payload = data.get(
                "payload",
                {}
            )

            headers_list = payload.get(
                "headers",
                []
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

            snippet = data.get(
                "snippet",
                ""
            )

            # Segunda criba.
            if not looks_like_invoice(
                subject,
                snippet,
                sender
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
                "has_attachment": has_attachments(
                    payload
                ),
            })

    return results


# ─────────────────────────────────────────────────────────────
# OBTENER CONTENIDO COMPLETO DEL EMAIL
# ─────────────────────────────────────────────────────────────

async def get_email_content(
    access_token: str,
    message_id: str
) -> dict:
    """
    Descarga el contenido completo de un email.

    Devuelve:

        {
            subject,
            from,
            date,
            text,
            html,
            attachments
        }

    Las facturas pueden estar:
    - en texto
    - en HTML
    - en PDF
    - en imagen
    """

    headers = {
        "Authorization": (
            f"Bearer {access_token}"
        )
    }

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:

        resp = await client.get(
            "https://gmail.googleapis.com/"
            "gmail/v1/users/me/messages/"
            f"{message_id}",
            headers=headers,
            params={
                "format": "full"
            },
        )

        resp.raise_for_status()

        data = resp.json()

        payload = data.get(
            "payload",
            {}
        )

        headers_list = payload.get(
            "headers",
            []
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

        text_parts = []
        html_parts = []

        attachments = []

        # -----------------------------------------------------
        # Recorrer todas las partes del email
        # -----------------------------------------------------

        async def walk_parts(
            part: dict
        ):

            mime_type = (
                part.get("mimeType")
                or ""
            )

            filename = (
                part.get("filename")
                or ""
            )

            body = part.get(
                "body",
                {}
            )

            # -------------------------------------------------
            # Texto / HTML
            # -------------------------------------------------

            encoded_data = body.get(
                "data"
            )

            if encoded_data:

                decoded = decode_base64url(
                    encoded_data
                )

                if mime_type == "text/plain":

                    if decoded.strip():
                        text_parts.append(
                            decoded
                        )

                elif mime_type == "text/html":

                    if decoded.strip():
                        html_parts.append(
                            decoded
                        )

            # -------------------------------------------------
            # Adjunto
            # -------------------------------------------------

            attachment_id = body.get(
                "attachmentId"
            )

            if (
                filename
                and attachment_id
                and (
                    mime_type == "application/pdf"
                    or mime_type.startswith(
                        "image/"
                    )
                )
            ):

                try:

                    attachment_resp = (
                        await client.get(
                            "https://gmail.googleapis.com/"
                            "gmail/v1/users/me/messages/"
                            f"{message_id}/attachments/"
                            f"{attachment_id}",
                            headers=headers,
                        )
                    )

                    attachment_resp.raise_for_status()

                    attachment_data = (
                        attachment_resp
                        .json()
                        .get(
                            "data",
                            ""
                        )
                    )

                    attachments.append({
                        "filename": filename,
                        "mime_type": mime_type,
                        "data_b64": attachment_data,
                    })

                except Exception as e:

                    print(
                        "WARNING: Error descargando "
                        f"adjunto {filename}: {e}"
                    )

            # -------------------------------------------------
            # Partes internas
            # -------------------------------------------------

            for child in part.get(
                "parts",
                []
            ):

                await walk_parts(child)

        # -----------------------------------------------------
        # Algunos emails simples tienen el body directamente
        # -----------------------------------------------------

        await walk_parts(payload)

        text = "\n".join(
            text_parts
        ).strip()

        html = "\n".join(
            html_parts
        ).strip()

        # Si solo existe HTML, generamos también una versión
        # de texto para que Gemini pueda trabajar con ella.
        html_as_text = clean_html(html)

        if not text and html_as_text:
            text = html_as_text

        return {
            "subject": subject,
            "from": sender,
            "date": date,
            "text": text,
            "html": html,
            "attachments": attachments,
        }