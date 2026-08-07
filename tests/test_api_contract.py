"""
Verificación del contrato que consume Horario360.

No basta con que el API funcione: tiene que ser indistinguible del de BioTime.
Estas pruebas fijan lo que `app/Services/Biometric/ZKTecoDevice.php` da por
hecho, para que un cambio futuro no lo rompa en silencio.

Requieren la copia restaurada (contenedor biotime-pg). Se saltan si no está.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.conftest import TEST_DSN, set_test_env, skip_if_db_unavailable


@pytest.fixture(scope="module")
def client():
    set_test_env()

    from app.main import app

    try:
        with TestClient(app) as test_client:
            test_client.get("/health").raise_for_status()
            yield test_client
    except BaseException as exc:
        skip_if_db_unavailable(exc)


def test_health_lista_terminales(client):
    payload = client.get("/health").json()

    assert payload["status"] == "ok"
    assert len(payload["terminals"]) >= 1


def test_transactions_exige_autenticacion(client):
    assert client.get("/iclock/api/transactions/").status_code == 401
    assert client.get(
        "/iclock/api/transactions/", headers={"Authorization": "JWT invalido"}
    ).status_code == 401


def test_token_invalido_devuelve_400(client):
    response = client.post(
        "/jwt-api-token-auth/", json={"username": "nadie", "password": "x"}
    )

    assert response.status_code == 400
    assert "non_field_errors" in response.json()


def test_ingesta_no_captura_la_ruta_del_api(client):
    """El catch-all /iclock/{subpath} no debe tragarse /iclock/api/transactions/.

    Si lo hiciera, Horario360 recibiría "OK" en texto plano y su
    `$responseJson['data']` reventaría.
    """
    response = client.get("/iclock/api/transactions/")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


@pytest.fixture
def auth_headers(client):
    """Token emitido con un usuario real de auth_user, si hay alguno activo."""
    import asyncio

    import asyncpg

    async def fetch_user() -> str | None:
        conn = await asyncpg.connect(TEST_DSN)
        try:
            return await conn.fetchval(
                "SELECT username FROM auth_user WHERE is_active LIMIT 1"
            )
        finally:
            await conn.close()

    username = asyncio.run(fetch_user())
    if not username:
        pytest.skip("No hay usuarios activos en auth_user")

    # Se firma directamente: la contraseña real no está disponible en pruebas.
    from app import security

    token = security.create_token(username, 1, get_settings().jwt_secret, 1)
    return {"Authorization": f"JWT {token}"}


def test_forma_de_la_respuesta(client, auth_headers):
    """Las claves de nivel superior que ZKTecoDevice.php lee directamente."""
    payload = client.get(
        "/iclock/api/transactions/", params={"page_size": 2}, headers=auth_headers
    ).json()

    for key in ("count", "next", "previous", "msg", "code", "data"):
        assert key in payload, f"falta la clave {key!r} del contrato BioTime"

    assert isinstance(payload["data"], list)
    assert payload["code"] == 0


def test_campos_que_consume_horario360(client, auth_headers):
    payload = client.get(
        "/iclock/api/transactions/", params={"page_size": 1}, headers=auth_headers
    ).json()

    if not payload["data"]:
        pytest.skip("Sin marcaciones en la base de pruebas")

    record = payload["data"][0]
    # Leídos explícitamente en ZKTecoDevice.php. first_name y
    # punch_state_display no están en el manual PDF, pero el consumidor los usa.
    for field in ("emp_code", "punch_time", "terminal_sn", "first_name",
                  "punch_state_display"):
        assert field in record, f"Horario360 lee {field!r} y no está"


def test_punch_time_es_cadena_plana_sin_zona(client, auth_headers):
    """Formato 'Y-m-d H:i:s', ya convertido a hora local.

    Añadir el offset ISO (…-05:00) rompería el Carbon::parse de Horario360, que
    espera el mismo formato que emite BioTime hoy.
    """
    import re

    payload = client.get(
        "/iclock/api/transactions/", params={"page_size": 1}, headers=auth_headers
    ).json()

    if not payload["data"]:
        pytest.skip("Sin marcaciones en la base de pruebas")

    punch_time = payload["data"][0]["punch_time"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", punch_time), punch_time


def test_next_es_null_en_la_ultima_pagina(client, auth_headers):
    """Horario360 pagina con `while ($response['next'])`: sin null, bucle infinito."""
    payload = client.get(
        "/iclock/api/transactions/",
        params={"page_size": 1, "emp_code": "__no_existe__"},
        headers=auth_headers,
    ).json()

    assert payload["count"] == 0
    assert payload["next"] is None


def test_filtro_por_rango_de_fechas(client, auth_headers):
    """start_time/end_time en formato Y-m-d, como los envía Horario360."""
    payload = client.get(
        "/iclock/api/transactions/",
        params={"start_time": "2026-08-01", "end_time": "2026-08-03", "page_size": 500},
        headers=auth_headers,
    ).json()

    assert payload["count"] > 0, "se esperaban marcaciones del 1 y 2 de agosto"
    for record in payload["data"]:
        assert record["punch_time"][:10] in ("2026-08-01", "2026-08-02")
