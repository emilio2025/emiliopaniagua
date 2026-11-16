import asyncio
import os
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth
from app.config import Settings
from app.security import ApiError

CLIENT_ID = "123-test.apps.googleusercontent.com"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKey:
    key = PRIVATE_KEY.public_key()


@pytest.fixture(autouse=True)
def fake_jwks(monkeypatch):
    monkeypatch.setattr(auth._jwks_client, "get_signing_key_from_jwt", lambda token: FakeKey())


def settings(client_id: str = CLIENT_ID) -> Settings:
    return Settings(
        database_url="postgresql://x@localhost/x",
        redis_url="redis://localhost:6379/0",
        lab_key="k" * 40,
        google_client_id=client_id,
    )


def token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1098765",
        "email": "emilio@example.com",
        "email_verified": True,
        "name": "Emilio  Paniagua",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256")


def verify(raw: str, cfg: Settings | None = None):
    return asyncio.run(auth.verify_google_token(raw, cfg or settings()))


def test_valid_google_token() -> None:
    customer = verify(token())
    assert customer.name == "Emilio Paniagua"
    assert customer.email == "emilio@example.com"
    assert customer.sub == "1098765"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "otro-cliente.apps.googleusercontent.com"},
        {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 600},
        {"email_verified": False},
    ],
)
def test_rejects_invalid_tokens(overrides) -> None:
    with pytest.raises(ApiError) as error:
        verify(token(**overrides))
    assert error.value.status_code == 401


def test_rejects_wrong_signature() -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(jwt.decode(token(), options={"verify_signature": False}), other, algorithm="RS256")
    with pytest.raises(ApiError):
        verify(forged)


def test_requires_configuration() -> None:
    with pytest.raises(ApiError) as error:
        verify(token(), settings(""))
    assert error.value.code == "AUTH_NOT_CONFIGURED"


@pytest.mark.skipif(
    not os.environ.get("FLASH_TEST_DATABASE_URL") or not os.environ.get("FLASH_TEST_REDIS_URL"),
    reason="requires FLASH_TEST_DATABASE_URL and FLASH_TEST_REDIS_URL",
)
def test_order_stores_customer_name() -> None:
    from fastapi.testclient import TestClient

    os.environ["DATABASE_URL"] = os.environ["FLASH_TEST_DATABASE_URL"]
    os.environ["REDIS_URL"] = os.environ["FLASH_TEST_REDIS_URL"]
    os.environ.setdefault("LAB_KEY", "integration-lab-key-" + "k" * 32)
    os.environ["FRONTEND_DIST"] = os.path.join(os.path.dirname(__file__), "missing-dist")
    from app.main import app

    app.dependency_overrides[auth.optional_customer] = lambda: auth.Customer("1098765", "e@example.com", "Emilio Paniagua")
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/orders",
                json={"items": [{"product_id": "arc-mouse", "quantity": 1}]},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            assert created.status_code == 201
            assert "customer" not in created.json()  # contrato público S09 intacto
            info = client.get(f"/api/v1/orders/{created.json()['id']}/customer")
            assert info.status_code == 200
            assert info.json()["customer_name"] == "Emilio Paniagua"
            assert "email" not in info.json()
    finally:
        app.dependency_overrides.clear()
