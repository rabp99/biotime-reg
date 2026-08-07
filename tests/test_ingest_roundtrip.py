"""
Prueba de extremo a extremo: terminal → base de datos → Horario360.

Verifica el punto de fallo más caro del proyecto: la conversión de zona horaria.
El terminal envía su hora de pared sin decir en qué zona está. Si se inserta tal
cual en `punch_time` (timestamptz), PostgreSQL la interpreta en la zona de la
sesión y todas las marcaciones quedan desplazadas 5 horas: las de las 07:00
aparecerían a las 02:00 y los reportes de Horario360 saldrían mal sin que nada
falle de forma visible.

Requiere la copia restaurada (contenedor biotime-pg). Se salta si no está.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.conftest import TEST_DSN, set_test_env, skip_if_db_unavailable

TEST_SN = "VDE2251700310"          # equipo real, terminal_tz = -5
TEST_EMP_CODE = "999999999"        # código inexistente: no colisiona con datos reales


def _run(coro):
    return asyncio.run(coro)


async def _cleanup() -> None:
    conn = await asyncpg.connect(TEST_DSN)
    try:
        await conn.execute(
            "DELETE FROM iclock_transaction WHERE emp_code = $1", TEST_EMP_CODE
        )
        # Todas las filas del equipo de prueba, no solo las de ATTLOG: las
        # pruebas también generan ATTPHOTO y otras tablas.
        await conn.execute(
            "DELETE FROM zk_raw_payload WHERE device_sn = $1", TEST_SN
        )
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def client():
    set_test_env(ALLOWED_SNS=TEST_SN)

    from app.main import app

    try:
        with TestClient(app) as test_client:
            test_client.get("/health").raise_for_status()
            yield test_client
    except BaseException as exc:
        skip_if_db_unavailable(exc)

    _run(_cleanup())


@pytest.fixture(autouse=True)
def limpio():
    _run(_cleanup())
    yield
    _run(_cleanup())


def _push(client, body: str):
    """Simula el POST que hace el terminal al entregar marcaciones."""
    return client.post(
        "/iclock/cdata",
        params={"SN": TEST_SN, "table": "ATTLOG", "Stamp": "9999999999"},
        content=body.encode("utf-8"),
    )


async def _fetch_utc(emp_code: str):
    conn = await asyncpg.connect(TEST_DSN)
    try:
        return await conn.fetch(
            """
            SELECT emp_code, punch_time, punch_state, verify_type, work_code,
                   terminal_sn, terminal_alias, area_alias, source, purpose,
                   is_attendance, sync_status, company_code, terminal_id
            FROM iclock_transaction WHERE emp_code = $1 ORDER BY punch_time
            """,
            emp_code,
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------

def test_terminal_recibe_ok(client):
    """Si la respuesta no es exactamente OK, el equipo reenvía el lote."""
    response = _push(client, f"{TEST_EMP_CODE}\t2026-08-07 07:00:00\t255\t15\t0\n")

    assert response.status_code == 200
    assert response.text.strip() == "OK"


def test_hora_local_del_equipo_se_guarda_como_utc(client):
    """07:00 en Lima (UTC-5) tiene que quedar como 12:00 UTC en la base."""
    _push(client, f"{TEST_EMP_CODE}\t2026-08-07 07:00:00\t255\t15\t0\n")

    rows = _run(_fetch_utc(TEST_EMP_CODE))

    assert len(rows) == 1
    punch_time = rows[0]["punch_time"]
    assert punch_time.utcoffset().total_seconds() == 0, "asyncpg devuelve UTC"
    assert punch_time.hour == 12, f"esperado 12:00 UTC, obtenido {punch_time}"
    assert (punch_time.year, punch_time.month, punch_time.day) == (2026, 8, 7)


def test_el_api_devuelve_la_misma_hora_que_marco_el_empleado(client):
    """El viaje completo: lo que el equipo envió es lo que Horario360 lee."""
    from app import security

    _push(client, f"{TEST_EMP_CODE}\t2026-08-07 07:00:00\t255\t15\t0\n")

    token = security.create_token("test", 1, get_settings().jwt_secret, 1)
    payload = client.get(
        "/iclock/api/transactions/",
        params={"emp_code": TEST_EMP_CODE},
        headers={"Authorization": f"JWT {token}"},
    ).json()

    assert payload["count"] == 1
    assert payload["data"][0]["punch_time"] == "2026-08-07 07:00:00"


def test_reenvio_no_duplica(client):
    """El terminal reenvía cuando no recibe OK a tiempo. No puede duplicar."""
    body = (
        f"{TEST_EMP_CODE}\t2026-08-07 07:00:00\t255\t15\t0\n"
        f"{TEST_EMP_CODE}\t2026-08-07 13:00:00\t255\t15\t0\n"
    )

    assert _push(client, body).text.strip() == "OK"
    assert _push(client, body).text.strip() == "OK"   # mismo lote otra vez
    assert _push(client, body).text.strip() == "OK"

    assert len(_run(_fetch_utc(TEST_EMP_CODE))) == 2


def test_campos_replican_los_de_biotime(client):
    """Los valores por defecto observados en las 56.891 marcaciones reales."""
    _push(client, f"{TEST_EMP_CODE}\t2026-08-07 07:00:00\t255\t15\t0\n")

    row = _run(_fetch_utc(TEST_EMP_CODE))[0]

    assert row["source"] == 1
    assert row["is_attendance"] == 1
    assert row["sync_status"] == 0
    assert row["company_code"] == "1"
    assert row["punch_state"] == "255"
    assert row["verify_type"] == 15
    assert row["work_code"] == "0"
    assert row["terminal_sn"] == TEST_SN
    assert row["terminal_alias"] == "BIO_07"      # resuelto desde iclock_terminal
    assert row["area_alias"] == "sistemas"        # resuelto desde personnel_area
    assert row["terminal_id"] is not None
    # purpose queda NULL porque este emp_code no existe en personnel_employee,
    # igual que en las 4.119 marcaciones huérfanas de los equipos activos.
    assert row["purpose"] is None


def test_lote_grande_con_lineas_corruptas(client):
    """Una línea ilegible no puede tumbar el lote entero."""
    body = "\n".join([
        f"{TEST_EMP_CODE}\t2026-08-07 07:00:0{i}\t255\t15\t0" for i in range(5)
    ] + [
        "basura sin tabuladores",
        f"{TEST_EMP_CODE}\tfecha-invalida\t255\t15\t0",
    ])

    assert _push(client, body).text.strip() == "OK"
    assert len(_run(_fetch_utc(TEST_EMP_CODE))) == 5


def test_attlog_guarda_el_cuerpo_crudo(client):
    """La red de seguridad: si el parser falla, el dato original sigue disponible.

    El terminal avanza su puntero al recibir OK y no reenvía. Sin esta copia,
    una línea que el parser no entienda se perdería sin dejar rastro.
    """
    cuerpo = f"{TEST_EMP_CODE}\t2026-08-07 07:00:00\t255\t15\t0\n"
    _push(client, cuerpo)

    async def leer():
        conn = await asyncpg.connect(TEST_DSN)
        try:
            return await conn.fetchrow(
                """
                SELECT body, body_bytes, parsed_rows, inserted_rows, skipped_lines
                FROM zk_raw_payload
                WHERE device_sn = $1 AND table_name = 'ATTLOG'
                ORDER BY id DESC LIMIT 1
                """,
                TEST_SN,
            )
        finally:
            await conn.close()

    fila = _run(leer())
    assert fila is not None
    assert fila["body"].decode("utf-8") == cuerpo
    assert fila["parsed_rows"] == 1
    assert fila["inserted_rows"] == 1
    assert fila["skipped_lines"] == 0


def test_attlog_registra_las_lineas_descartadas(client):
    """skipped_lines es lo que permite detectar que el firmware envía algo nuevo."""
    _push(client, (
        f"{TEST_EMP_CODE}\t2026-08-07 07:00:00\t255\t15\t0\n"
        "linea que el parser no entiende\n"
    ))

    async def leer():
        conn = await asyncpg.connect(TEST_DSN)
        try:
            return await conn.fetchrow(
                "SELECT parsed_rows, inserted_rows, skipped_lines FROM zk_raw_payload "
                "WHERE device_sn = $1 AND table_name = 'ATTLOG' ORDER BY id DESC LIMIT 1",
                TEST_SN,
            )
        finally:
            await conn.close()

    fila = _run(leer())
    assert fila["parsed_rows"] == 1
    assert fila["skipped_lines"] == 1


def test_fotos_no_ocupan_espacio_en_la_auditoria(client):
    """De ATTPHOTO se guarda la fila pero no el binario.

    No procesamos fotos: almacenarlas haría crecer la auditoría cientos de KB
    por marcación sin que sirvan para reprocesar nada.
    """
    client.post(
        "/iclock/cdata",
        params={"SN": TEST_SN, "table": "ATTPHOTO"},
        content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000,
    )

    async def leer():
        conn = await asyncpg.connect(TEST_DSN)
        try:
            return await conn.fetchrow(
                "SELECT body, body_bytes FROM zk_raw_payload "
                "WHERE device_sn = $1 AND table_name = 'ATTPHOTO' ORDER BY id DESC LIMIT 1",
                TEST_SN,
            )
        finally:
            await conn.close()

    fila = _run(leer())
    assert fila is not None, "debe quedar constancia de que la petición ocurrió"
    assert fila["body"] is None, "el binario no debe almacenarse"
    assert fila["body_bytes"] > 5000, "pero sí su tamaño"


def test_sn_desconocido_es_rechazado(client):
    """Sin este filtro, cualquiera en internet podría inyectar marcaciones."""
    response = client.post(
        "/iclock/cdata",
        params={"SN": "SN_FALSO_123", "table": "ATTLOG"},
        content=b"1\t2026-08-07 07:00:00\t255\t15\t0\n",
    )

    assert response.status_code == 403


def test_handshake_devuelve_configuracion(client):
    response = client.get("/iclock/cdata", params={"SN": TEST_SN, "options": "all"})
    lines = response.text.splitlines()

    assert response.status_code == 200
    assert lines[0] == f"GET OPTION FROM: {TEST_SN}"
    assert any(line.startswith("Stamp=") for line in lines)
    assert any(line.startswith("TransFlag=") for line in lines)


def test_getrequest_responde_ok(client):
    """Sin respuesta el equipo entra en reintentos y degrada también el envío."""
    response = client.get("/iclock/getrequest", params={"SN": TEST_SN})

    assert response.status_code == 200
    assert response.text.strip() == "OK"
