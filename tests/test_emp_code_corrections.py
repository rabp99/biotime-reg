"""
Corrección de códigos mal enrolados (/api/emp-code-corrections/).

Es la única parte de la aplicación que modifica marcaciones, así que se fija:
acceso (IP, interruptor, token), que solo se mueve el rango pedido, que nunca se
viola el UNIQUE (company_code, emp_code, punch_time), que dry_run no deja
rastro, que el undo restaura lo movido y que lo movido aparece en
/iclock/api/transactions/ con el código correcto.

Requiere una base con el esquema de BioTime y sql/004_emp_code_correction.sql
(la copia restaurada, o tests/fixtures/minimal_schema.sql). Se salta si no está.
Los datos son sintéticos, con códigos ZZ<aleatorio>, y se borran al terminar.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.conftest import TEST_DSN, ClientIP, set_test_env, skip_if_db_unavailable

HORARIO360_IP = "161.132.68.120"
INTRUSO_IP = "203.0.113.9"
LIMA = timezone(timedelta(hours=-5))


def lima(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LIMA)


def run(coro):
    return asyncio.run(coro)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(TEST_DSN)


# ---------------------------------------------------------------------------
# Datos sintéticos
# ---------------------------------------------------------------------------

class Seed:
    """Códigos únicos por prueba y helpers para sembrar y limpiar."""

    def __init__(self):
        tag = uuid.uuid4().hex[:8].upper()
        self.wrong = f"ZZ{tag}W"      # código mal enrolado
        self.right = f"ZZ{tag}R"      # código correcto
        self.codes = (self.wrong, self.right)

    async def employee(self, emp_code: str) -> int:
        conn = await _connect()
        try:
            return await conn.fetchval(
                """
                INSERT INTO personnel_employee
                    (status, emp_code, first_name, last_name, enable_payroll, is_active, company_id)
                VALUES (0, $1, 'PRUEBA', 'CORRECCION', false, true,
                        (SELECT min(id) FROM personnel_company))
                RETURNING id
                """,
                emp_code,
            )
        finally:
            await conn.close()

    async def punches(self, emp_code: str, *local_times: str, emp_id: int | None = None) -> list[int]:
        conn = await _connect()
        try:
            ids = []
            for text in local_times:
                ids.append(await conn.fetchval(
                    """
                    INSERT INTO iclock_transaction
                        (emp_code, punch_time, punch_state, verify_type, terminal_sn,
                         terminal_alias, company_code, emp_id, purpose, source, upload_time)
                    VALUES ($1, $2, '255', 1, 'ZZTEST', 'PRUEBA', '1', $3,
                            CASE WHEN $3::integer IS NULL THEN NULL ELSE 9 END, 1, now())
                    RETURNING id
                    """,
                    emp_code, lima(text), emp_id,
                ))
            return ids
        finally:
            await conn.close()

    async def rows(self, ids: list[int]) -> dict[int, asyncpg.Record]:
        conn = await _connect()
        try:
            records = await conn.fetch(
                "SELECT id, emp_code, emp_id, purpose FROM iclock_transaction WHERE id = ANY($1::int[])",
                ids,
            )
            return {r["id"]: r for r in records}
        finally:
            await conn.close()

    async def cleanup(self) -> None:
        conn = await _connect()
        try:
            codes = list(self.codes)
            await conn.execute(
                """
                DELETE FROM zk_emp_code_correction_item WHERE correction_id IN (
                    SELECT id FROM zk_emp_code_correction
                    WHERE from_emp_code = ANY($1::text[]) OR to_emp_code = ANY($1::text[]))
                """,
                codes,
            )
            await conn.execute(
                """
                DELETE FROM zk_emp_code_correction
                WHERE from_emp_code = ANY($1::text[]) OR to_emp_code = ANY($1::text[])
                """,
                codes,
            )
            await conn.execute("DELETE FROM iclock_transaction WHERE emp_code = ANY($1::text[])", codes)
            await conn.execute("DELETE FROM personnel_employee WHERE emp_code = ANY($1::text[])", codes)
        finally:
            await conn.close()


@pytest.fixture
def seed():
    data = Seed()
    try:
        run(data.cleanup())  # también comprueba que la base y las tablas existen
    except BaseException as exc:
        skip_if_db_unavailable(exc)
    yield data
    run(data.cleanup())


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

@contextmanager
def api_client(*, enabled: bool = True, source_ip: str = HORARIO360_IP, allowed_ips: str = HORARIO360_IP):
    set_test_env(
        CORRECTIONS_ENABLED="true" if enabled else "false",
        API_ALLOWED_IPS=allowed_ips,
    )

    from app.main import api_app

    try:
        with TestClient(ClientIP(api_app, source_ip)) as client:
            client.get("/health").raise_for_status()
            yield client
    except BaseException as exc:
        skip_if_db_unavailable(exc)


def auth() -> dict[str, str]:
    from app import security

    return {"Authorization": f"JWT {security.create_token('test', 1, get_settings().jwt_secret, 1)}"}


def body(seed: Seed, **overrides) -> dict:
    return {
        "from": seed.wrong,
        "to": seed.right,
        "start": "2026-03-01",
        "end": "2026-03-31",
        "requested_by": "admin@horario360",
        "reference": "employee:1",
        **overrides,
    }


BASE = "/api/emp-code-corrections"


# ---------------------------------------------------------------------------
# Acceso
# ---------------------------------------------------------------------------

def test_apagado_responde_404(seed):
    with api_client(enabled=False) as client:
        assert client.get(f"{BASE}/preview", params={"from": seed.wrong, "to": seed.right},
                          headers=auth()).status_code == 404
        assert client.post(f"{BASE}/", json=body(seed), headers=auth()).status_code == 404


def test_ip_ajena_rechazada_antes_que_todo(seed):
    with api_client(source_ip=INTRUSO_IP) as client:
        response = client.post(f"{BASE}/", json=body(seed), headers=auth())

    assert response.status_code == 403


def test_sin_token_401(seed):
    with api_client() as client:
        assert client.post(f"{BASE}/", json=body(seed)).status_code == 401
        assert client.get(f"{BASE}/preview", params={"from": seed.wrong, "to": seed.right}).status_code == 401


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides, field", [
    ({"to": None}, "to"),
    ({"from": "11 35"}, "from"),
    ({"from": "X" * 21}, "from"),
    ({"start": "2026-02-30"}, "start"),
    ({"start": "2026-04-01"}, "end"),
    ({"end": None}, "end"),
    ({"requested_by": ""}, "requested_by"),
    ({"reference": "x" * 151}, "reference"),
    ({"dry_run": "si"}, "dry_run"),
])
def test_validacion(seed, overrides, field):
    with api_client() as client:
        response = client.post(f"{BASE}/", json=body(seed, **overrides), headers=auth())

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert field in error["fields"]


def test_origen_igual_a_destino(seed):
    with api_client() as client:
        response = client.post(f"{BASE}/", json=body(seed, to=seed.wrong), headers=auth())

    assert response.status_code == 422
    assert "to" in response.json()["error"]["fields"]


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview(seed):
    run(seed.punches(seed.wrong, "2026-02-27 08:00:00", "2026-03-02 08:00:00",
                     "2026-03-02 17:00:00", "2026-03-31 23:30:00"))
    right_id = run(seed.employee(seed.right))
    run(seed.punches(seed.right, "2026-03-02 17:00:00", emp_id=right_id))  # conflicto

    with api_client() as client:
        response = client.get(
            f"{BASE}/preview",
            params={"from": seed.wrong, "to": seed.right, "start": "2026-03-01", "end": "2026-03-31"},
            headers=auth(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["from"]["employee"] is None
    assert payload["from"]["total_transactions"] == 4
    assert payload["from"]["first_punch"] == "2026-02-27 08:00:00"
    assert payload["from"]["last_punch"] == "2026-03-31 23:30:00"
    # 23:30 del 31 en Lima es 1 de abril en UTC (y en Hong Kong): tiene que entrar.
    assert payload["from"]["range_transactions"] == 3
    assert payload["from"]["range_first_punch"] == "2026-03-02 08:00:00"
    assert payload["from"]["terminals"] == [{"sn": "ZZTEST", "alias": "PRUEBA", "count": 3}]
    assert payload["to"]["employee"]["id"] == right_id
    assert payload["to"]["total_transactions"] == 1
    assert payload["conflicts"] == 1
    assert payload["range"] == {"start": "2026-03-01", "end": "2026-03-31"}


def test_preview_sin_rango(seed):
    run(seed.punches(seed.wrong, "2026-02-27 08:00:00", "2026-03-02 08:00:00"))

    with api_client() as client:
        payload = client.get(f"{BASE}/preview", params={"from": seed.wrong, "to": seed.right},
                             headers=auth()).json()

    assert payload["range"] is None
    assert payload["from"]["range_transactions"] == 2


# ---------------------------------------------------------------------------
# Corrección
# ---------------------------------------------------------------------------

def test_mueve_solo_el_rango_y_salta_conflictos(seed):
    fuera, dentro_1, conflicto, dentro_2 = run(seed.punches(
        seed.wrong, "2026-02-28 23:59:59", "2026-03-01 00:00:00", "2026-03-02 17:00:00",
        "2026-03-31 23:59:59",
    ))
    right_id = run(seed.employee(seed.right))
    run(seed.punches(seed.right, "2026-03-02 17:00:00", emp_id=right_id))

    with api_client() as client:
        response = client.post(f"{BASE}/", json=body(seed), headers=auth())

    assert response.status_code == 201, response.text
    result = response.json()
    assert result["moved"] == 2
    assert result["skipped_conflicts"] == 1
    assert result["to_employee_id"] == right_id
    assert result["first_punch"] == "2026-03-01 00:00:00"
    assert result["last_punch"] == "2026-03-31 23:59:59"
    assert isinstance(result["correction_id"], int)

    rows = run(seed.rows([fuera, dentro_1, conflicto, dentro_2]))
    assert rows[fuera]["emp_code"] == seed.wrong
    assert rows[conflicto]["emp_code"] == seed.wrong
    for moved in (dentro_1, dentro_2):
        assert rows[moved]["emp_code"] == seed.right
        assert rows[moved]["emp_id"] == right_id
        assert rows[moved]["purpose"] == 9


def test_destino_sin_empleado_deja_emp_id_nulo(seed):
    wrong_id = run(seed.employee(seed.wrong))
    (punch,) = run(seed.punches(seed.wrong, "2026-03-10 08:00:00", emp_id=wrong_id))

    with api_client() as client:
        response = client.post(f"{BASE}/", json=body(seed), headers=auth())

    assert response.status_code == 201, response.text
    assert response.json()["to_employee_id"] is None
    row = run(seed.rows([punch]))[punch]
    assert row["emp_code"] == seed.right
    assert row["emp_id"] is None
    assert row["purpose"] is None


def test_lo_movido_sale_en_el_api_de_horario360(seed):
    run(seed.punches(seed.wrong, "2026-03-10 08:00:00", "2026-03-10 17:00:00"))

    with api_client() as client:
        client.post(f"{BASE}/", json=body(seed), headers=auth()).raise_for_status()
        listado = client.get(
            "/iclock/api/transactions/",
            params={"emp_code": seed.right, "start_time": "2026-03-10", "end_time": "2026-03-11"},
            headers=auth(),
        ).json()

    assert listado["count"] == 2
    assert [r["punch_time"] for r in listado["data"]] == ["2026-03-10 08:00:00", "2026-03-10 17:00:00"]


def test_dry_run_no_deja_rastro(seed):
    (punch,) = run(seed.punches(seed.wrong, "2026-03-10 08:00:00"))

    with api_client() as client:
        response = client.post(f"{BASE}/", json=body(seed, dry_run=True), headers=auth())
        historial = client.get(BASE, params={"from": seed.wrong}, headers=auth()).json()

    assert response.status_code == 200
    assert response.json()["moved"] == 1
    assert response.json()["correction_id"] is None
    assert run(seed.rows([punch]))[punch]["emp_code"] == seed.wrong
    assert historial["data"] == []


def test_nada_que_mover(seed):
    run(seed.punches(seed.wrong, "2026-05-10 08:00:00"))

    with api_client() as client:
        response = client.post(f"{BASE}/", json=body(seed), headers=auth())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "nothing_to_move"


def test_todo_en_conflicto(seed):
    run(seed.punches(seed.wrong, "2026-03-10 08:00:00"))
    run(seed.punches(seed.right, "2026-03-10 08:00:00"))

    with api_client() as client:
        response = client.post(f"{BASE}/", json=body(seed), headers=auth())

    assert response.status_code == 422
    assert "ya existen" in response.json()["error"]["message"]


def test_repetir_solo_mueve_lo_nuevo(seed):
    """El equipo sigue enviando el código incorrecto hasta que lo reenrolan."""
    run(seed.punches(seed.wrong, "2026-03-10 08:00:00"))

    with api_client() as client:
        assert client.post(f"{BASE}/", json=body(seed), headers=auth()).json()["moved"] == 1
        run(seed.punches(seed.wrong, "2026-03-11 08:00:00"))
        assert client.post(f"{BASE}/", json=body(seed), headers=auth()).json()["moved"] == 1


# ---------------------------------------------------------------------------
# Registro y undo
# ---------------------------------------------------------------------------

def test_undo_restaura_y_no_se_repite(seed):
    wrong_id = run(seed.employee(seed.wrong))
    right_id = run(seed.employee(seed.right))
    a, b = run(seed.punches(seed.wrong, "2026-03-10 08:00:00", "2026-03-10 17:00:00", emp_id=wrong_id))

    with api_client() as client:
        created = client.post(f"{BASE}/", json=body(seed), headers=auth()).json()
        correction_id = created["correction_id"]
        assert run(seed.rows([a]))[a]["emp_id"] == right_id

        detail = client.get(f"{BASE}/{correction_id}", headers=auth()).json()
        assert detail["moved"] == 2 and detail["undone_at"] is None

        undone = client.post(f"{BASE}/{correction_id}/undo",
                             json={"requested_by": "admin@horario360"}, headers=auth())
        again = client.post(f"{BASE}/{correction_id}/undo",
                            json={"requested_by": "admin@horario360"}, headers=auth())
        detail = client.get(f"{BASE}/{correction_id}", headers=auth()).json()

    assert undone.status_code == 200, undone.text
    assert undone.json()["restored"] == 2
    assert undone.json()["skipped"] == 0
    rows = run(seed.rows([a, b]))
    for row in rows.values():
        assert row["emp_code"] == seed.wrong
        assert row["emp_id"] == wrong_id
        assert row["purpose"] == 9
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_undone"
    assert detail["restored"] == 2 and detail["undone_by"] == "admin@horario360"


def test_undo_inexistente_404(seed):
    with api_client() as client:
        response = client.post(f"{BASE}/999999999/undo", json={"requested_by": "x"}, headers=auth())

    assert response.status_code == 404


def test_historial(seed):
    run(seed.punches(seed.wrong, "2026-03-10 08:00:00"))

    with api_client() as client:
        client.post(f"{BASE}/", json=body(seed), headers=auth()).raise_for_status()
        listado = client.get(BASE, params={"to": seed.right}, headers=auth()).json()

    assert len(listado["data"]) == 1
    item = listado["data"][0]
    assert item["from"] == seed.wrong
    assert item["range"] == {"start": "2026-03-01", "end": "2026-03-31"}
    assert item["requested_by"] == "admin@horario360"
