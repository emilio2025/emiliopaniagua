"""Autenticación: correo + contraseña (cuentas locales) y Google (Google Identity Services).

El navegador obtiene un ID token (JWT firmado por Google) y lo envía como
`Authorization: Bearer <token>`. El backend verifica firma (JWKS de Google),
emisor, audiencia (GOOGLE_CLIENT_ID), expiración y correo verificado.

La sesión es opcional en la API: sin token, los pedidos siguen funcionando igual
que en Semana 9 (contrato público intacto). Con token válido, el pedido guarda
el nombre del cliente.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .db import utcnow
from .models import UserAccount
from .security import ApiError, get_session

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
MAX_TOKEN_LENGTH = 4096

router = APIRouter()
_jwks_client = jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600, timeout=8)


@dataclass(frozen=True)
class Customer:
    sub: str
    email: str
    name: str


def _verify_sync(token: str, client_id: str) -> dict[str, Any]:
    signing_key = _jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        leeway=30,
    )


async def verify_google_token(token: str, settings: Settings) -> Customer:
    client_id = settings.google_client_id.strip()
    if not client_id:
        raise ApiError(401, "AUTH_NOT_CONFIGURED", "Google sign-in is not configured")
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise ApiError(401, "UNAUTHORIZED", "Invalid Google session")
    try:
        claims = await asyncio.to_thread(_verify_sync, token, client_id)
    except Exception:
        raise ApiError(401, "UNAUTHORIZED", "Invalid Google session") from None
    if claims.get("iss") not in GOOGLE_ISSUERS or claims.get("email_verified") is not True:
        raise ApiError(401, "UNAUTHORIZED", "Invalid Google session")
    email = str(claims.get("email") or "")[:254]
    name = str(claims.get("name") or claims.get("given_name") or email.split("@")[0] or "Cliente")
    return Customer(sub=str(claims["sub"])[:64], email=email, name=" ".join(name.split())[:120])


# ---------------------------------------------------------------- cuentas locales
LOCAL_ISSUER = "flash-local"
SESSION_SECONDS = 8 * 3600
PBKDF2_ITERATIONS = 310_000
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
# Hash ficticio para que un correo inexistente tarde lo mismo que una contraseña errónea.
_DUMMY_HASH = None


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iterations)
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except Exception:
        return False


def _session_key(settings: Settings) -> bytes:
    # Clave de firma derivada del secreto del servidor (LAB_KEY); nunca sale del backend.
    return hashlib.sha256(b"flash-session-v1:" + settings.lab_key_value().encode("utf-8")).digest()


def issue_session(settings: Settings, account: UserAccount) -> str:
    now = int(time.time())
    claims = {
        "iss": LOCAL_ISSUER,
        "sub": str(account.id),
        "email": account.email,
        "name": account.name,
        "iat": now,
        "exp": now + SESSION_SECONDS,
    }
    return jwt.encode(claims, _session_key(settings), algorithm="HS256")


def _verify_local(token: str, settings: Settings) -> Customer | None:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    if header.get("alg") != "HS256":
        return None
    try:
        claims = jwt.decode(
            token,
            _session_key(settings),
            algorithms=["HS256"],
            issuer=LOCAL_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.PyJWTError:
        raise ApiError(401, "UNAUTHORIZED", "Invalid session") from None
    return Customer(
        sub=f"local:{claims['sub']}"[:64],
        email=str(claims.get("email") or "")[:254],
        name=str(claims.get("name") or "Cliente")[:120],
    )


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(min_length=2, max_length=80)
    email: StrictStr = Field(min_length=5, max_length=254)
    password: StrictStr = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: StrictStr = Field(min_length=3, max_length=254)
    password: StrictStr = Field(min_length=1, max_length=128)


def _normalize_email(raw: str) -> str:
    return raw.strip().lower()


def _session_response(settings: Settings, account: UserAccount, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"token": issue_session(settings, account), "user": {"name": account.name, "email": account.email}},
    )


@router.post("/api/v1/auth/register")
async def register(payload: RegisterIn, request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    email = _normalize_email(payload.email)
    name = " ".join(payload.name.split())
    if not EMAIL_PATTERN.match(email):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid email")
    if len(name) < 2:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid name")
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    account = UserAccount(id=uuid.uuid4(), email=email, name=name, password_hash=password_hash, created_at=utcnow())
    try:
        async with session.begin():
            session.add(account)
    except IntegrityError:
        raise ApiError(409, "EMAIL_TAKEN", "Email already registered") from None
    return _session_response(request.app.state.settings, account, 201)


@router.post("/api/v1/auth/login")
async def login(payload: LoginIn, request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    global _DUMMY_HASH
    email = _normalize_email(payload.email)
    account = (
        await session.execute(select(UserAccount).where(func.lower(UserAccount.email) == email))
    ).scalar_one_or_none()
    if account is None:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = await asyncio.to_thread(hash_password, secrets.token_hex(8))
        await asyncio.to_thread(verify_password, payload.password, _DUMMY_HASH)
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    if not await asyncio.to_thread(verify_password, payload.password, account.password_hash):
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    return _session_response(request.app.state.settings, account, 200)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise ApiError(401, "UNAUTHORIZED", "Invalid Google session")
    return value.strip()


async def optional_customer(request: Request) -> Customer | None:
    """Dependencia: cliente autenticado si llega un token válido; None si no hay token."""
    token = _bearer(request)
    if token is None:
        return None
    settings = request.app.state.settings
    local = _verify_local(token, settings)
    if local is not None:
        return local
    return await verify_google_token(token, settings)


@router.get("/api/v1/auth/config")
async def auth_config(request: Request) -> JSONResponse:
    client_id = request.app.state.settings.google_client_id.strip()
    return JSONResponse(
        content={"google_enabled": bool(client_id), "google_client_id": client_id or None, "password_enabled": True}
    )


@router.get("/api/v1/auth/me")
async def auth_me(request: Request) -> JSONResponse:
    customer = await optional_customer(request)
    if customer is None:
        raise ApiError(401, "UNAUTHORIZED", "Sign in required")
    return JSONResponse(content={"name": customer.name, "email": customer.email})
