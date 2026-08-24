"""
gmail_client.py — OAuth 2.0 con Gmail + lectura de emails con facturas.
"""

import os
import base64
import httpx

from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from database import GmailToken


# ─── Configuración ────────────────────────────────────────────────────────────

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

SCOPES = "https://www.googleapis.com/auth/gmail.readonly"


# Palabras que consideramos relacionadas con facturas.
#
# La búsqueda principal se hace sobre el ASUNTO del email.
# Después volvemos a comprobar el asunto nosotros mismos.
INVOICE_SUBJECT_KEYWORDS = [
    "factura",
    "invoice",
    "receipt",
    "recibo",
    "bill",
    "facturación",
    "facturacion",
]


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

    return (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + params
    )


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


# ─── Gestión del token en DB ──────────────────────────────────────────────────

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

        # Google normalmente solamente devuelve
        # refresh_token durante la autorización inicial.
        if "refresh_token" in token_data:

            record.refresh_token = (
                token_data["refresh_token"]
            )

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
    el token almacenado en nuestra base de datos.
    """

    record = (
        db.query(GmailToken)
        .first()
    )

    if not record:

        return True

    # Preferimos revocar el refresh token porque es
    # el que permite mantener el acceso a Gmail.
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

                # 200 = revocado correctamente.
                #
                # 400 puede significar que el token ya
                # estaba revocado o ya no era válido.
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

    # Aunque Google no responda correctamente,
    # eliminamos el token local.
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

        new_token = (
            await refresh_access_token(
                record.refresh_token
            )
        )

        save_token(
            db,
            new_token
        )

        return new_token[
            "access_token"
        ]

    return record.access_token


# ─── Lectura de emails ────────────────────────────────────────────────────────

async def list_invoice_emails(
    access_token: str,
    max_results: int = 20
) -> list[dict]:

    """
    Busca emails que parezcan facturas.

    CRITERIOS:

    1. El email tiene que tener un adjunto.
    2. La palabra relacionada con factura tiene que
       aparecer en el ASUNTO.
    3. Volvemos a comprobar el asunto localmente.
    4. La comparación local no distingue mayúsculas
       y minúsculas.

    Ejemplos que deberían aparecer:

        Factura 1234
        Factura de agosto
        FACTURA 2026
        Invoice August
        INVOICE #123
        Recibo de compra

    Ejemplos que NO deberían aparecer:

        Reunión del próximo martes
        Information about your new Google Account
        Tu pedido ha sido enviado
        Gracias por contactar
    """

    headers = {
        "Authorization": (
            f"Bearer {access_token}"
        )
    }

    # ─────────────────────────────────────────────
    # BÚSQUEDA DE GMAIL
    # ─────────────────────────────────────────────
    #
    # subject: es importante.
    #
    # De esta manera no basta con que la palabra
    # "factura" aparezca en el cuerpo del correo.
    #
    query = (
        "has:attachment "
        "("
        'subject:factura OR '
        'subject:invoice OR '
        'subject:receipt OR '
        'subject:recibo OR '
        'subject:bill OR '
        'subject:facturación OR '
        'subject:facturacion'
        ")"
    )

    async with httpx.AsyncClient() as client:

        # ─────────────────────────────────────────
        # Obtener IDs de mensajes
        # ─────────────────────────────────────────

        resp = await client.get(
            "https://gmail.googleapis.com/"
            "gmail/v1/users/me/messages",
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

        # ─────────────────────────────────────────
        # Obtener metadata de cada email
        # ─────────────────────────────────────────

        for msg in messages:

            try:

                detail = await client.get(
                    "https://gmail.googleapis.com/"
                    f"gmail/v1/users/me/messages/"
                    f"{msg['id']}",
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

                data = detail.json()

                headers_list = (
                    data
                    .get("payload", {})
                    .get("headers", [])
                )

                # Convertimos los nombres de headers
                # a minúsculas para evitar problemas.
                header_map = {
                    h["name"].lower():
                    h["value"]
                    for h in headers_list
                }

                subject = header_map.get(
                    "subject",
                    "(sin asunto)"
                )

                sender = header_map.get(
                    "from",
                    ""
                )

                date = header_map.get(
                    "date",
                    ""
                )

                snippet = data.get(
                    "snippet",
                    ""
                )

                # ─────────────────────────────────
                # SEGUNDO FILTRO LOCAL
                # ─────────────────────────────────
                #
                # casefold() permite comparar sin
                # preocuparnos por mayúsculas/minúsculas.
                #
                # Ejemplo:
                #
                # FACTURA
                # Factura
                # factura
                #
                # todos funcionan.
                # ─────────────────────────────────

                subject_normalized = (
                    subject.casefold()
                )

                is_invoice = any(
                    keyword.casefold()
                    in subject_normalized
                    for keyword
                    in INVOICE_SUBJECT_KEYWORDS
                )

                if not is_invoice:

                    continue

                # ─────────────────────────────────
                # Añadir email válido
                # ─────────────────────────────────

                results.append({

                    "id": msg["id"],

                    "subject": subject,

                    "from": sender,

                    "date": date,

                    "snippet": snippet,

                })

                # Seguridad: no devolver más de
                # max_results resultados válidos.
                if len(results) >= max_results:

                    break

            except Exception as e:

                print(
                    "WARNING: No se pudo leer "
                    f"el email {msg.get('id')}: {e}"
                )

                continue

    return results


# ─── Contenido del email ──────────────────────────────────────────────────────

async def get_email_content(
    access_token: str,
    message_id: str
) -> dict:

    """
    Obtiene el contenido completo de un email,
    incluyendo texto y adjuntos.
    """

    headers = {
        "Authorization": (
            f"Bearer {access_token}"
        )
    }

    async with httpx.AsyncClient() as client:

        # ─────────────────────────────────────────
        # Obtener mensaje completo
        # ─────────────────────────────────────────

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

        message = resp.json()

        payload = message.get(
            "payload",
            {}
        )

        result = {

            "id": message.get(
                "id"
            ),

            "thread_id": message.get(
                "threadId"
            ),

            "snippet": message.get(
                "snippet",
                ""
            ),

            "headers": {},

            "text": "",

            "html": "",

            "attachments": [],

        }

        # ─────────────────────────────────────────
        # Headers
        # ─────────────────────────────────────────

        headers_list = payload.get(
            "headers",
            []
        )

        result["headers"] = {
            h["name"]: h["value"]
            for h in headers_list
        }

        # ─────────────────────────────────────────
        # Procesar partes del mensaje
        # ─────────────────────────────────────────

        await _process_message_part(
            client=client,
            headers=headers,
            part=payload,
            result=result,
        )

        return result


async def _process_message_part(
    client: httpx.AsyncClient,
    headers: dict,
    part: dict,
    result: dict
):

    """
    Recorre recursivamente las partes de un email.

    Detecta:

    - text/plain
    - text/html
    - PDF
    - imágenes
    - otros adjuntos
    """

    mime_type = (
        part.get(
            "mimeType",
            ""
        )
        or ""
    )

    body = part.get(
        "body",
        {}
    )

    filename = (
        part.get(
            "filename",
            ""
        )
        or ""
    )

    # ─────────────────────────────────────────
    # Texto plano
    # ─────────────────────────────────────────

    if mime_type == "text/plain":

        data = body.get(
            "data"
        )

        if data:

            try:

                decoded = base64.urlsafe_b64decode(
                    data + "="
                    * (
                        4
                        - len(data) % 4
                    )
                )

                result["text"] += (
                    decoded
                    .decode(
                        "utf-8",
                        errors="replace"
                    )
                )

            except Exception as e:

                print(
                    "WARNING: Error decodificando "
                    f"text/plain: {e}"
                )

    # ─────────────────────────────────────────
    # HTML
    # ─────────────────────────────────────────

    elif mime_type == "text/html":

        data = body.get(
            "data"
        )

        if data:

            try:

                decoded = base64.urlsafe_b64decode(
                    data + "="
                    * (
                        4
                        - len(data) % 4
                    )
                )

                result["html"] += (
                    decoded
                    .decode(
                        "utf-8",
                        errors="replace"
                    )
                )

            except Exception as e:

                print(
                    "WARNING: Error decodificando "
                    f"text/html: {e}"
                )

    # ─────────────────────────────────────────
    # Adjuntos
    # ─────────────────────────────────────────

    if filename:

        attachment_id = body.get(
            "attachmentId"
        )

        data_b64 = body.get(
            "data"
        )

        attachment_data = None

        # Algunas veces Gmail devuelve el contenido
        # directamente.
        if data_b64:

            attachment_data = data_b64

        # Otras veces devuelve únicamente attachmentId.
        elif attachment_id:

            try:

                attachment_resp = await client.get(
                    "https://gmail.googleapis.com/"
                    "gmail/v1/users/me/messages/"
                    f"{result['id']}/attachments/"
                    f"{attachment_id}",
                    headers=headers,
                )

                attachment_resp.raise_for_status()

                attachment_json = (
                    attachment_resp.json()
                )

                attachment_data = (
                    attachment_json
                    .get(
                        "data"
                    )
                )

            except Exception as e:

                print(
                    "WARNING: Error descargando "
                    f"adjunto {filename}: {e}"
                )

        if attachment_data:

            result["attachments"].append({

                "filename": filename,

                "mime_type": mime_type,

                "data_b64": attachment_data,

            })

    # ─────────────────────────────────────────
    # Partes internas
    # ─────────────────────────────────────────

    for child in part.get(
        "parts",
        []
    ):

        await _process_message_part(
            client=client,
            headers=headers,
            part=child,
            result=result,
        )