"""
main.py — Backend de Expediente
FastAPI + PostgreSQL + Gmail OAuth + Holded + Claude AI
"""

import os
import time

from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse

from pydantic import BaseModel
from sqlalchemy.orm import Session

from typing import Optional

from database import (
    init_db,
    get_db,
    Expediente,
    GmailToken,
    Config,
)

import claude_client
import gmail_client
import holded_client


app = FastAPI(
    title="Expediente Backend",
    version="1.0.0",
)


FRONTEND_URL = os.getenv(
    "FRONTEND_URL",
    "https://web-production-050a84.up.railway.app",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()


# ─── Home ─────────────────────────────────────────────────────────────────────

@app.get("/")
def home():
    return FileResponse("static/index.html")


# ─── Gmail OAuth ──────────────────────────────────────────────────────────────

@app.get("/gmail/auth-url")
def gmail_auth_url():

    return {
        "url": gmail_client.get_auth_url()
    }


@app.get("/gmail/callback")
async def gmail_callback(
    code: str,
    db: Session = Depends(get_db),
):

    try:

        token_data = await gmail_client.exchange_code_for_token(
            code
        )

        gmail_client.save_token(
            db,
            token_data,
        )

    except Exception as e:

        raise HTTPException(
            status_code=400,
            detail=f"Error al obtener token de Gmail: {e}",
        )

    return RedirectResponse(
        url=f"{FRONTEND_URL}?gmail=conectado"
    )


@app.get("/gmail/status")
def gmail_status(
    db: Session = Depends(get_db),
):

    record = (
        db.query(GmailToken)
        .first()
    )

    connected = (
        record is not None
        and bool(record.refresh_token)
    )

    return {
        "connected": connected
    }


@app.delete("/gmail/disconnect")
def gmail_disconnect(
    db: Session = Depends(get_db),
):

    records = (
        db.query(GmailToken)
        .all()
    )

    for record in records:
        db.delete(record)

    db.commit()

    return {
        "disconnected": True
    }


# ─── Gmail: listar emails ─────────────────────────────────────────────────────

@app.get("/gmail/emails")
async def gmail_emails(
    db: Session = Depends(get_db),
):

    token = await gmail_client.get_valid_access_token(
        db
    )

    if not token:

        raise HTTPException(
            status_code=401,
            detail="Gmail no conectado.",
        )

    try:

        emails = await gmail_client.list_invoice_emails(
            token
        )

        return {
            "emails": emails
        }

    except Exception as e:

        print(
            f"ERROR LISTANDO GMAIL: "
            f"{type(e).__name__}: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Error leyendo Gmail: {e}",
        )


# ─── Gmail: procesar email ────────────────────────────────────────────────────

@app.post("/gmail/process/{message_id}")
async def gmail_process_email(
    message_id: str,
    db: Session = Depends(get_db),
):

    token = await gmail_client.get_valid_access_token(
        db
    )

    if not token:

        raise HTTPException(
            status_code=401,
            detail="Gmail no conectado.",
        )


    # ─────────────────────────────────────────────────────────────
    # 1. Obtener contenido COMPLETO del email
    # ─────────────────────────────────────────────────────────────

    try:

        content = await gmail_client.get_email_content(
            token,
            message_id,
        )

    except Exception as e:

        print(
            f"ERROR OBTENIENDO EMAIL {message_id}: "
            f"{type(e).__name__}: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Error leyendo email: {e}",
        )


    extracted = None


    # ─────────────────────────────────────────────────────────────
    # 2. Intentar primero PDFs / imágenes
    # ─────────────────────────────────────────────────────────────

    attachments = content.get(
        "attachments",
        [],
    )


    for att in attachments:

        mime_type = (
            att.get("mime_type", "")
            or ""
        ).lower()

        data = (
            att.get("data_b64", "")
            or ""
        )


        if not (
            mime_type == "application/pdf"
            or mime_type.startswith("image/")
        ):
            continue


        # Si es una referencia a un attachment externo,
        # gmail_client debería haberlo descargado.
        if data.startswith(
            "__attachment_id__"
        ):
            continue


        if not data:
            continue


        try:

            print(
                f"Procesando adjunto "
                f"{mime_type} del email {message_id}"
            )


            extracted = (
                await claude_client.extract_from_base64(
                    data,
                    mime_type,
                )
            )


            if extracted:
                break


        except Exception as e:

            print(
                f"ERROR IA ADJUNTO: "
                f"{type(e).__name__}: {e}"
            )

            continue


    # ─────────────────────────────────────────────────────────────
    # 3. Si no hay PDF, usar texto plano del email
    # ─────────────────────────────────────────────────────────────

    if not extracted:

        body_text = (
            content.get("body_text")
            or ""
        ).strip()


        if body_text:

            print(
                f"Extrayendo factura desde "
                f"cuerpo de email {message_id}"
            )


            try:

                extracted = (
                    await claude_client.extract_from_text(
                        body_text
                    )
                )


            except Exception as e:

                print(
                    f"ERROR IA TEXTO: "
                    f"{type(e).__name__}: {e}"
                )

                raise HTTPException(
                    status_code=500,
                    detail=f"Error extrayendo datos: {e}",
                )


    # ─────────────────────────────────────────────────────────────
    # 4. Último intento usando HTML si no había text/plain
    # ─────────────────────────────────────────────────────────────

    if not extracted:

        body_html = (
            content.get("body_html")
            or ""
        ).strip()


        if body_html:

            try:

                extracted = (
                    await claude_client.extract_from_text(
                        body_html
                    )
                )


            except Exception as e:

                print(
                    f"ERROR IA HTML: "
                    f"{type(e).__name__}: {e}"
                )

                raise HTTPException(
                    status_code=500,
                    detail=f"Error extrayendo datos del HTML: {e}",
                )


    # ─────────────────────────────────────────────────────────────
    # 5. No se pudo extraer nada
    # ─────────────────────────────────────────────────────────────

    if not extracted:

        raise HTTPException(
            status_code=422,
            detail=(
                "No se pudieron extraer datos "
                "de este email."
            ),
        )


    # Guardamos el ID de Gmail
    extracted["_gmail_message_id"] = message_id


    return {
        "extracted": extracted
    }


# ─── Extracción manual ────────────────────────────────────────────────────────

class TextInput(BaseModel):

    text: str


class FileInput(BaseModel):

    data_b64: str
    mime_type: str


@app.post("/extract/text")
async def extract_text(
    body: TextInput,
):

    if not body.text.strip():

        raise HTTPException(
            status_code=400,
            detail="El texto está vacío.",
        )


    try:

        result = (
            await claude_client.extract_from_text(
                body.text
            )
        )


        return {
            "extracted": result
        }


    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


@app.post("/extract/file")
async def extract_file(
    body: FileInput,
):

    try:

        result = (
            await claude_client.extract_from_base64(
                body.data_b64,
                body.mime_type,
            )
        )


        return {
            "extracted": result
        }


    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ─── Expedientes ──────────────────────────────────────────────────────────────

class ExpedienteInput(BaseModel):

    proveedor: str

    nif_proveedor: Optional[str] = None

    fecha: Optional[str] = None

    concepto: Optional[str] = None

    base_imponible: Optional[float] = None

    iva_porcentaje: Optional[float] = None

    iva_importe: Optional[float] = None

    total: Optional[float] = None

    moneda: Optional[str] = "EUR"

    gmail_message_id: Optional[str] = None


def exp_to_dict(
    e: Expediente,
) -> dict:

    return {

        "id": e.id,

        "folio": e.folio,

        "proveedor": e.proveedor,

        "nif_proveedor": e.nif_proveedor,

        "fecha": e.fecha,

        "concepto": e.concepto,

        "base_imponible": e.base_imponible,

        "iva_porcentaje": e.iva_porcentaje,

        "iva_importe": e.iva_importe,

        "total": e.total,

        "moneda": e.moneda,

        "estado": e.estado,

        "holded_id": e.holded_id,

        "gmail_message_id": e.gmail_message_id,

        "creado": (
            e.creado.isoformat()
            if e.creado
            else None
        ),
    }


@app.get("/expedientes")
def list_expedientes(
    db: Session = Depends(get_db),
):

    items = (
        db.query(Expediente)
        .order_by(
            Expediente.creado.desc()
        )
        .all()
    )


    return {

        "expedientes": [
            exp_to_dict(e)
            for e in items
        ]

    }


@app.post("/expedientes")
def create_expediente(
    body: ExpedienteInput,
    db: Session = Depends(get_db),
):

    count = (
        db.query(Expediente)
        .count()
    )


    exp_id = (
        f"exp_{int(time.time() * 1000)}"
    )


    folio = (
        f"EXP-{(count + 1):04d}"
    )


    exp = Expediente(

        id=exp_id,

        folio=folio,

        proveedor=body.proveedor,

        nif_proveedor=body.nif_proveedor,

        fecha=body.fecha,

        concepto=body.concepto,

        base_imponible=body.base_imponible,

        iva_porcentaje=body.iva_porcentaje,

        iva_importe=body.iva_importe,

        total=body.total,

        moneda=body.moneda or "EUR",

        gmail_message_id=body.gmail_message_id,
    )


    db.add(exp)

    db.commit()

    db.refresh(exp)


    return exp_to_dict(exp)


@app.get("/expedientes/{exp_id}")
def get_expediente(
    exp_id: str,
    db: Session = Depends(get_db),
):

    exp = (
        db.query(Expediente)
        .filter(
            Expediente.id == exp_id
        )
        .first()
    )


    if not exp:

        raise HTTPException(
            status_code=404,
            detail="Expediente no encontrado.",
        )


    return exp_to_dict(exp)


@app.delete("/expedientes/{exp_id}")
def delete_expediente(
    exp_id: str,
    db: Session = Depends(get_db),
):

    exp = (
        db.query(Expediente)
        .filter(
            Expediente.id == exp_id
        )
        .first()
    )


    if not exp:

        raise HTTPException(
            status_code=404,
            detail="Expediente no encontrado.",
        )


    db.delete(exp)

    db.commit()


    return {
        "deleted": True
    }


# ─── Holded ───────────────────────────────────────────────────────────────────

@app.post("/expedientes/{exp_id}/holded")
async def send_to_holded(
    exp_id: str,
    db: Session = Depends(get_db),
):

    exp = (
        db.query(Expediente)
        .filter(
            Expediente.id == exp_id
        )
        .first()
    )


    if not exp:

        raise HTTPException(
            status_code=404,
            detail="Expediente no encontrado.",
        )


    api_key = _get_holded_key(
        db
    )


    if not api_key:

        raise HTTPException(
            status_code=400,
            detail="API key de Holded no configurada.",
        )


    try:

        result = (
            await holded_client.create_purchase_invoice(
                api_key,
                exp_to_dict(exp),
            )
        )


        holded_doc_id = (

            result.get("id")

            or result.get("docId")

            or result.get(
                "data",
                {}
            ).get("id")

        )


        exp.holded_id = holded_doc_id

        exp.estado = "enviado_holded"


        db.commit()


        return {

            "holded_response": result,

            "holded_id": holded_doc_id,

        }


    except Exception as e:

        exp.estado = "error"

        db.commit()


        raise HTTPException(
            status_code=500,
            detail=f"Error al enviar a Holded: {e}",
        )


# ─── Configuración Holded ─────────────────────────────────────────────────────

def _get_holded_key(
    db: Session,
) -> str | None:

    cfg = (
        db.query(Config)
        .filter(
            Config.clave == "holded_api_key"
        )
        .first()
    )


    return (
        cfg.valor
        if cfg
        else None
    )


class HoldedKeyInput(BaseModel):

    api_key: str


@app.get("/config/holded")
def holded_config_status(
    db: Session = Depends(get_db),
):

    key = _get_holded_key(
        db
    )


    return {
        "configured": key is not None
    }


@app.post("/config/holded")
async def save_holded_key(
    body: HoldedKeyInput,
    db: Session = Depends(get_db),
):

    works = (
        await holded_client.test_connection(
            body.api_key
        )
    )


    if not works:

        raise HTTPException(
            status_code=400,
            detail="La API key de Holded no es válida.",
        )


    cfg = (
        db.query(Config)
        .filter(
            Config.clave == "holded_api_key"
        )
        .first()
    )


    if cfg:

        cfg.valor = body.api_key

    else:

        cfg = Config(
            clave="holded_api_key",
            valor=body.api_key,
        )

        db.add(cfg)


    db.commit()


    return {
        "saved": True
    }


@app.delete("/config/holded")
def delete_holded_key(
    db: Session = Depends(get_db),
):

    cfg = (
        db.query(Config)
        .filter(
            Config.clave == "holded_api_key"
        )
        .first()
    )


    if cfg:

        db.delete(cfg)

        db.commit()


    return {
        "deleted": True
    }


@app.get("/holded/test")
async def test_holded(
    db: Session = Depends(get_db),
):

    key = _get_holded_key(
        db
    )


    if not key:

        raise HTTPException(
            status_code=400,
            detail="API key no configurada.",
        )


    works = (
        await holded_client.test_connection(
            key
        )
    )


    return {
        "connected": works
    }