"""
Control de acceso al API por IP de origen.

Horario360 corre en otro servidor (161.132.68.120) y el host del API tiene IP
pública, así que el puerto 8083 escucha en todas las interfaces. El firewall es
la barrera principal; esta comprobación en la aplicación es la segunda, para
que perder la regla —un reinstalado, una directiva de grupo— no deje el
endpoint de credenciales abierto a internet.

Estas pruebas fijan ese comportamiento: sin ellas, un refactor podría
desactivar la restricción sin que nada falle de forma visible.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.conftest import TEST_DSN, ClientIP, skip_if_db_unavailable

HORARIO360_IP = "161.132.68.120"
INTRUSO_IP = "203.0.113.9"


@contextmanager
def api_client(source_ip: str, allowed_ips: str):
    """Cliente del API visto desde `source_ip`, con la lista dada configurada."""
    os.environ["PG_DSN"] = TEST_DSN
    os.environ["JWT_SECRET"] = "secreto-de-prueba"
    os.environ["API_ALLOWED_IPS"] = allowed_ips
    get_settings.cache_clear()

    from app.main import api_app

    try:
        with TestClient(ClientIP(api_app, source_ip)) as client:
            client.get("/health").raise_for_status()
            yield client
    except BaseException as exc:
        skip_if_db_unavailable(exc)
    finally:
        os.environ.pop("API_ALLOWED_IPS", None)
        get_settings.cache_clear()


def _token() -> str:
    from app import security

    return security.create_token("test", 1, get_settings().jwt_secret, 1)


# ---------------------------------------------------------------------------

def test_horario360_puede_pedir_token():
    """Credenciales inválidas dan 400, no 403: la IP sí está autorizada."""
    with api_client(HORARIO360_IP, HORARIO360_IP) as client:
        response = client.post(
            "/jwt-api-token-auth/", json={"username": "nadie", "password": "x"}
        )

    assert response.status_code == 400


def test_ip_no_autorizada_no_puede_pedir_token():
    """Lo que de verdad protege: nadie más puede intentar adivinar credenciales."""
    with api_client(INTRUSO_IP, HORARIO360_IP) as client:
        response = client.post(
            "/jwt-api-token-auth/", json={"username": "acruzado", "password": "x"}
        )

    assert response.status_code == 403


def test_ip_no_autorizada_no_puede_consultar_marcaciones():
    """Se rechaza por IP antes de mirar el token: no revela si el token vale."""
    with api_client(INTRUSO_IP, HORARIO360_IP) as client:
        response = client.get(
            "/iclock/api/transactions/", headers={"Authorization": f"JWT {_token()}"}
        )

    assert response.status_code == 403


def test_token_valido_no_sirve_desde_ip_ajena():
    """Aunque se filtrara un token, desde otra IP no abre nada."""
    with api_client(HORARIO360_IP, HORARIO360_IP) as autorizado:
        token = _token()
        permitido = autorizado.get(
            "/iclock/api/transactions/",
            params={"page_size": 1},
            headers={"Authorization": f"JWT {token}"},
        )

    with api_client(INTRUSO_IP, HORARIO360_IP) as intruso:
        denegado = intruso.get(
            "/iclock/api/transactions/",
            params={"page_size": 1},
            headers={"Authorization": f"JWT {token}"},
        )

    assert permitido.status_code == 200
    assert denegado.status_code == 403


def test_lista_vacia_no_restringe():
    """Sin API_ALLOWED_IPS la comprobación se desactiva: necesario en desarrollo."""
    with api_client(INTRUSO_IP, "") as client:
        response = client.post(
            "/jwt-api-token-auth/", json={"username": "nadie", "password": "x"}
        )

    assert response.status_code == 400  # llega a validar credenciales, no 403


def test_health_no_requiere_autorizacion():
    """El healthcheck debe responder para poder monitorizar el servicio."""
    with api_client(INTRUSO_IP, HORARIO360_IP) as client:
        assert client.get("/health").status_code == 200
