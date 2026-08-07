"""
Autenticación del API de consulta.

Dos piezas:

1. Verificación de contraseñas contra ``auth_user``, la tabla que BioTime ya
   tiene. Django guarda ``pbkdf2_sha256$<iteraciones>$<salt>$<hash_b64>`` y esa
   verificación se reimplementa aquí con la librería estándar. El objetivo es
   que Horario360 siga usando el mismo usuario y la misma clave que hoy: su
   ``.env`` no cambia en la migración.

2. Emisión y validación de tokens JWT, replicando ``/jwt-api-token-auth/``.
   Horario360 los envía como ``Authorization: JWT <token>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


# ---------------------------------------------------------------------------
# Contraseñas de Django
# ---------------------------------------------------------------------------

def verify_django_password(raw_password: str, encoded: str | None) -> bool:
    """Comprueba una contraseña contra el hash almacenado por Django.

    Solo se soporta ``pbkdf2_sha256``, que es lo que usa BioTime. Cualquier otro
    algoritmo devuelve False en lugar de arriesgar una comparación incorrecta.
    """
    if not encoded or not raw_password:
        return False

    parts = encoded.split("$")
    if len(parts) != 4:
        return False

    algorithm, iterations, salt, expected_b64 = parts
    if algorithm != "pbkdf2_sha256":
        return False

    try:
        rounds = int(iterations)
    except ValueError:
        return False

    derived = hashlib.pbkdf2_hmac(
        "sha256", raw_password.encode("utf-8"), salt.encode("utf-8"), rounds
    )
    actual_b64 = base64.b64encode(derived).decode("ascii")

    # compare_digest evita filtrar información por el tiempo de comparación.
    return hmac.compare_digest(actual_b64, expected_b64)


# ---------------------------------------------------------------------------
# JWT (HS256)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(message: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def create_token(username: str, user_id: int, secret: str, ttl_hours: int) -> str:
    """Emite un JWT HS256 equivalente al de BioTime."""
    now = int(time.time())
    header = {"typ": "JWT", "alg": "HS256"}
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": now + ttl_hours * 3600,
        "iat": now,
    }

    segments = [
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode()),
        _b64url_encode(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    segments.append(_b64url_encode(_sign(signing_input, secret)))

    return ".".join(segments)


def decode_token(token: str, secret: str) -> dict[str, Any] | None:
    """Valida firma y expiración. Devuelve el payload, o None si no es válido."""
    parts = token.split(".")
    if len(parts) != 3:
        return None

    header_b64, payload_b64, signature_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    try:
        expected = _sign(signing_input, secret)
        if not hmac.compare_digest(_b64url_decode(signature_b64), expected):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None

    return payload


def extract_bearer(authorization: str | None) -> str | None:
    """Extrae el token de la cabecera Authorization.

    Horario360 envía ``JWT <token>``; el manual de BioTime documenta además
    ``Token <token>``. Se aceptan ambos, y también el token pelado.
    """
    if not authorization:
        return None

    value = authorization.strip()
    for prefix in ("JWT ", "Token ", "Bearer "):
        if value.lower().startswith(prefix.lower()):
            return value[len(prefix):].strip()

    return value or None
