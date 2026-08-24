"""
database.py — SQLite con SQLAlchemy
Guarda expedientes, credenciales OAuth de Gmail y config de Holded.
"""
from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./expediente.db")

# Railway a veces da postgres://, SQLAlchemy necesita postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Expediente(Base):
    __tablename__ = "expedientes"
    id               = Column(String, primary_key=True)
    folio            = Column(String)
    proveedor        = Column(String)
    nif_proveedor    = Column(String, nullable=True)
    fecha            = Column(String, nullable=True)
    concepto         = Column(Text, nullable=True)
    base_imponible   = Column(Float, nullable=True)
    iva_porcentaje   = Column(Float, nullable=True)
    iva_importe      = Column(Float, nullable=True)
    total            = Column(Float, nullable=True)
    moneda           = Column(String, default="EUR")
    estado           = Column(String, default="validado")
    holded_id        = Column(String, nullable=True)
    gmail_message_id = Column(String, nullable=True)
    creado           = Column(DateTime, default=datetime.utcnow)


class GmailToken(Base):
    __tablename__ = "gmail_tokens"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    access_token  = Column(Text)
    refresh_token = Column(Text)
    token_expiry  = Column(DateTime, nullable=True)
    actualizado   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Config(Base):
    __tablename__ = "config"
    clave = Column(String, primary_key=True)
    valor = Column(Text)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
