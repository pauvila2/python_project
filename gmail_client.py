"""
gmail_client.py — OAuth 2.0 con Gmail + lectura de emails.
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


# ─── Intercambio OAuth ────────────────────────────────────────────────────────

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


# ─── Renovación del access token ──────────────────────────────────────────────

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

def save_token(
    db: Session,
    token_data: dict
):

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
    Revoca el acceso de Gmail en Google
    y elimina el token almacenado localmente.
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

                # Google devuelve 200 si se revoca.
                # 400 puede significar que ya era inválido.
                if resp.status_code not in (200, 400):
                    resp.raise_for_status()

        except Exception as e:

            print(
                "WARNING: No se pudo revocar "
                f"el token de Google: {e}"
            )

    # Aunque Google falle al revocar,
    # eliminamos siempre el token local.
    db.delete(record)

    db.commit()

    return True


async def get_valid_access_token(
    db: Session
) -> str | None:

    record = db.query(GmailToken).first()

    if not record:
        return None

    if (
        record.token_expiry
        and datetime.utcnow()
        >= record.token_expiry
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


# ─── Listar emails ────────────────────────────────────────────────────────────

async def list_invoice_emails(
    access_token: str,
    max_results: int = 20
) -> list[dict]:
    """
    Devuelve los últimos emails de Gmail.

    No filtra por palabras como "factura" y no exige
    que tengan adjuntos. Esto permite que la aplicación
    vea también los emails de prueba.

    El botón "Procesar" decidirá después qué email
    contiene realmente una factura.
    """

    headers = {
        "Authorization":
        f"Bearer {access_token}"
    }

    async with httpx.AsyncClient() as client:

        # ── Obtener IDs de los últimos emails ───────────────

        resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={
                "maxResults": max_results,
            },
        )

        resp.raise_for_status()

        messages = resp.json().get(
            "messages",
            []
        )

        results = []

        # ── Obtener información de cada email ───────────────

        for msg in messages[:max_results]:

            message_id = msg.get("id")

            if not message_id:
                continue

            detail = await client.get(
                (
                    "https://gmail.googleapis.com/gmail/v1/"
                    f"users/me/messages/{message_id}"
                ),
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

            headers_list = (
                d.get("payload", {})
                .get("headers", [])
            )

            hmap = {
                h["name"]: h["value"]
                for h in headers_list
            }

            results.append({
                "id": message_id,

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


# ─── Obtener contenido completo de un email ───────────────────────────────────

async def get_email_content(
    access_token: str,
    message_id: str
) -> dict:
    """
    Obtiene el contenido completo de un email,
    incluyendo texto y archivos adjuntos.
    """

    headers = {
        "Authorization":
        f"Bearer {access_token}"
    }

    async with httpx.AsyncClient() as client:

        resp = await client.get(
            (
                "https://gmail.googleapis.com/gmail/v1/"
                f"users/me/messages/{message_id}"
            ),
            headers=headers,
            params={
                "format": "full"
            },
        )

        resp.raise_for_status()

        message = resp.json()

        payload = message.get(
            "payload",
            {}
        )

        result = {
            "id": message.get("id"),
            "thread_id": message.get("threadId"),
            "snippet": message.get("snippet", ""),
            "headers": {},
            "text": "",
            "html": "",
            "attachments": [],
        }

        # ── Headers ─────────────────────────────────────────

        headers_list = payload.get(
            "headers",
            []
        )

        for header in headers_list:

            name = header.get(
                "name",
                ""
            )

            value = header.get(
                "value",
                ""
            )

            result["headers"][name] = value

        # ── Recorrer partes del email ───────────────────────

        await _process_parts(
            client,
            headers,
            message_id,
            payload,
            result
        )

        return result


async def _process_parts(
    client: httpx.AsyncClient,
    headers: dict,
    message_id: str,
    part: dict,
    result: dict
):
    """
    Recorre recursivamente las partes MIME del email.
    Extrae texto, HTML y adjuntos.
    """

    mime_type = part.get(
        "mimeType",
        ""
    )

    filename = part.get(
        "filename",
        ""
    )

    body = part.get(
        "body",
        {}
    )

    data = body.get(
        "data"
    )

    attachment_id = body.get(
        "attachmentId"
    )

    # ── Texto / HTML ────────────────────────────────────────

    if data and mime_type in (
        "text/plain",
        "text/html",
    ):

        try:

            decoded = base64.urlsafe_b64decode(
                data + "=" * (
                    -len(data) % 4
                )
            ).decode(
                "utf-8",
                errors="replace"
            )

            if mime_type == "text/plain":

                result["text"] += decoded

            else:

                result["html"] += decoded

        except Exception as e:

            print(
                "WARNING: Error decodificando "
                f"parte {mime_type}: {e}"
            )

    # ── Adjuntos ────────────────────────────────────────────

    if filename:

        attachment_data = None

        # El contenido puede venir directamente
        # dentro del body.
        if data:

            try:

                attachment_data = (
                    base64.urlsafe_b64decode(
                        data + "=" * (
                            -len(data) % 4
                        )
                    )
                )

            except Exception as e:

                print(
                    "WARNING: Error decodificando "
                    f"adjunto {filename}: {e}"
                )

        # O Gmail puede devolver un attachmentId.
        elif attachment_id:

            try:

                resp = await client.get(
                    (
                        "https://gmail.googleapis.com/gmail/v1/"
                        f"users/me/messages/{message_id}/"
                        f"attachments/{attachment_id}"
                    ),
                    headers=headers,
                )

                resp.raise_for_status()

                attachment_json = resp.json()

                attachment_b64 = (
                    attachment_json.get(
                        "data",
                        ""
                    )
                )

                if attachment_b64:

                    attachment_data = (
                        base64.urlsafe_b64decode(
                            attachment_b64
                            + "=" * (
                                -len(attachment_b64) % 4
                            )
                        )
                    )

            except Exception as e:

                print(
                    "WARNING: Error descargando "
                    f"adjunto {filename}: {e}"
                )

        if attachment_data is not None:

            result["attachments"].append({
                "filename": filename,
                "mime_type": mime_type,
                "data_b64": base64.b64encode(
                    attachment_data
                ).decode("ascii"),
            })

    # ── Partes hijas ─────────────────────────────────────────

    for child in part.get(
        "parts",
        []
    ):

        await _process_parts(
            client,
            headers,
            message_id,
            child,
            result
        )