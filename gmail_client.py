"""
gmail_client.py — OAuth 2.0 con Gmail + lectura de emails con facturas.
"""

import os
import base64
import httpx
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database import GmailToken

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

SCOPES = "https://www.googleapis.com/auth/gmail.readonly"

GMAIL_KEYWORDS = ["factura", "invoice", "receipt"]


# ─── URLs de OAuth ────────────────────────────────────────────────────────────

def get_auth_url() -> str:
    params = (
        f"client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&access_type=offline"
        f"&prompt=consent"
    )

    return f"https://accounts.google.com/o/oauth2/v2/auth?{params}"


async def exchange_code_for_token(code: str) -> dict:
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


# ─── Gestión del token en DB ──────────────────────────────────────────────────

def save_token(db: Session, token_data: dict):
    record = db.query(GmailToken).first()

    expiry = None

    if "expires_in" in token_data:
        expiry = datetime.utcnow() + timedelta(
            seconds=token_data["expires_in"] - 60
        )

    if record:
        record.access_token = token_data["access_token"]

        if "refresh_token" in token_data:
            record.refresh_token = token_data["refresh_token"]

        record.token_expiry = expiry

    else:
        record = GmailToken(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", ""),
            token_expiry=expiry,
        )
        db.add(record)

    db.commit()


async def revoke_token(db: Session) -> bool:
    """
    Revoca el acceso de Gmail en Google y elimina
    el token almacenado en nuestra base de datos.
    """

    record = db.query(GmailToken).first()

    if not record:
        return True

    # Preferimos revocar el refresh token porque es el que
    # permite mantener el acceso a Gmail.
    token = record.refresh_token or record.access_token

    if token:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={
                        "token": token
                    },
                    headers={
                        "content-type": "application/x-www-form-urlencoded"
                    },
                )

                # 200 = revocado correctamente.
                # 400 = el token puede estar ya revocado/inválido.
                if resp.status_code not in (200, 400):
                    resp.raise_for_status()

        except Exception as e:
            print(
                f"WARNING: No se pudo revocar el token de Google: {e}"
            )

    # Aunque Google no responda correctamente, eliminamos
    # el token local para que la aplicación quede desconectada.
    db.delete(record)
    db.commit()

    return True


async def get_valid_access_token(db: Session) -> str | None:
    record = db.query(GmailToken).first()

    if not record:
        return None

    if record.token_expiry and datetime.utcnow() >= record.token_expiry:
        new_token = await refresh_access_token(
            record.refresh_token
        )

        save_token(db, new_token)

        return new_token["access_token"]

    return record.access_token


# ─── Lectura de emails ────────────────────────────────────────────────────────

async def list_invoice_emails(
    access_token: str,
    max_results: int = 20
) -> list[dict]:

    query_keywords = " OR ".join(
        f'"{kw}"'
        for kw in GMAIL_KEYWORDS
    )

    query = f"({query_keywords}) has:attachment"

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    async with httpx.AsyncClient() as client:

        resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={
                "q": query,
                "maxResults": max_results,
            },
        )

        resp.raise_for_status()

        messages = resp.json().get(
            "messages",
            []
        )

        results = []

        for msg in messages[:max_results]:

            detail = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}",
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

            d = detail.json()

            headers_list = d.get(
                "payload",
                {}
            ).get(
                "headers",
                []
            )

            hmap = {
                h["name"]: h["value"]
                for h in headers_list
            }

            results.append({
                "id": msg["id"],
                "subject": hmap.get(
                    "Subject",
                    "(sin asunto)"
                ),
                "from": hmap.get(
                    "From",
                    ""
                ),
                "date": hmap.get(
                    "Date",
                    ""
                ),
                "snippet": d.get(
                    "snippet",
                    ""
                ),
            })

    return results


# ─── Contenido del email ──────────────────────────────────────────────────────

async def get_email_content(
    access_token: str,
    message_id: str
) -> dict:

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    async with httpx.AsyncClient() as client:

        resp = await client.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            headers=headers,
            params={
                "format": "full"
            },
        )

        resp.raise_for_status()

        msg = resp.json()

        body_text = ""
        attachments = []

        body_text, attachments = await _parse_parts(
            client,
            msg.get("payload", {}),
            body_text,
            attachments,
            access_token,
            message_id,
        )

    return {
        "body_text": body_text,
        "attachments": attachments,
    }


async def _parse_parts(
    client,
    payload: dict,
    body_text: str,
    attachments: list,
    access_token: str,
    message_id: str,
):
    mime = payload.get(
        "mimeType",
        ""
    )

    parts = payload.get(
        "parts",
        []
    )

    # ─── Texto plano ─────────────────────────────────────────

    if mime == "text/plain" and not parts:

        data = payload.get(
            "body",
            {}
        ).get(
            "data",
            ""
        )

        if data:
            try:
                decoded = base64.urlsafe_b64decode(
                    data + "=="
                ).decode(
                    "utf-8",
                    errors="ignore"
                )

                body_text += decoded

            except Exception as e:
                print(
                    f"WARNING: Error decodificando texto: {e}"
                )

    # ─── Procesar partes ────────────────────────────────────

    for part in parts:

        filename = part.get(
            "filename",
            ""
        )

        part_mime = part.get(
            "mimeType",
            ""
        )

        body = part.get(
            "body",
            {}
        )

        # ─── PDF o imagen ───────────────────────────────────

        if filename and (
            part_mime == "application/pdf"
            or part_mime.startswith("image/")
        ):

            data = body.get(
                "data"
            )

            # Si Gmail no incluye directamente los datos,
            # utilizamos attachmentId para descargarlos.
            if not data and body.get("attachmentId"):

                attachment_id = body["attachmentId"]

                try:
                    attachment_resp = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}",
                        headers={
                            "Authorization": f"Bearer {access_token}"
                        },
                    )

                    attachment_resp.raise_for_status()

                    data = attachment_resp.json().get(
                        "data",
                        ""
                    )

                except Exception as e:
                    print(
                        f"WARNING: Error descargando adjunto "
                        f"{filename}: {e}"
                    )

            attachments.append({
                "filename": filename,
                "mime_type": part_mime,
                "data_b64": data or "",
            })

        # ─── Partes MIME anidadas ────────────────────────────

        elif part.get("parts"):

            body_text, attachments = await _parse_parts(
                client,
                part,
                body_text,
                attachments,
                access_token,
                message_id,
            )

    return body_text, attachments