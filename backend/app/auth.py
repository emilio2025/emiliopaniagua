"""Autenticación con Google (Sign in with Google / Google Identity Services).

El navegador obtiene un ID token (JWT firmado por Google) y lo envía como
`Authorization: Bearer <token>`. El backend verifica firma (JWKS de Google),
emisor, audiencia (GOOGLE_CLIENT_ID), expiración y correo verificado.

La sesión es opcional en la API: sin token, los pedidos siguen funcionando igual
que en Semana 9 (contrato público intacto). Con token válido, el pedido guarda
el nombre del cliente.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .security import ApiError

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
    return await verify_google_token(token, request.app.state.settings)


@router.get("/api/v1/auth/config")
async def auth_config(request: Request) -> JSONResponse:
    client_id = request.app.state.settings.google_client_id.strip()
    return JSONResponse(content={"google_enabled": bool(client_id), "google_client_id": client_id or None})


@router.get("/api/v1/auth/me")
async def auth_me(request: Request) -> JSONResponse:
    customer = await optional_customer(request)
    if customer is None:
        raise ApiError(401, "UNAUTHORIZED", "Sign in required")
    return JSONResponse(content={"name": customer.name, "email": customer.email})
