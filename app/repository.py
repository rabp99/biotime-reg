"""
Acceso a datos sobre las tablas que BioTime ya usa.

Las marcaciones van a ``iclock_transaction`` y los equipos a ``iclock_terminal``:
no se duplica el modelo, de modo que el histórico queda continuo y cualquier
consulta existente sigue funcionando.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import asyncpg

from app.protocol import AttlogRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Equipos
# ---------------------------------------------------------------------------

async def get_terminal(conn: asyncpg.Connection, sn: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT id, sn, alias, terminal_tz, language, log_stamp, op_log_stamp,
               capture_stamp, area_id
        FROM iclock_terminal
        WHERE sn = $1
        """,
        sn,
    )


async def ensure_terminal(
    conn: asyncpg.Connection, sn: str, source_ip: str | None, default_tz: int
) -> asyncpg.Record:
    """Devuelve el equipo, creándolo si es la primera vez que se ve.

    Los 4 terminales en producción ya están dados de alta, así que esto es una
    red de seguridad —por ejemplo si se repone un equipo— más que el camino
    habitual. Los NOT NULL de iclock_terminal se rellenan con los mismos valores
    que usa BioTime al registrar un equipo nuevo.
    """
    terminal = await get_terminal(conn, sn)
    if terminal is not None:
        return terminal

    logger.warning("Equipo no registrado, dando de alta: sn=%s ip=%s", sn, source_ip)
    await conn.execute(
        """
        INSERT INTO iclock_terminal (
            sn, alias, ip_address, real_ip, status, state, terminal_tz,
            heartbeat, transfer_mode, transfer_interval, transfer_time,
            is_attendance, is_registration, authentication, push_protocol,
            is_tft, lock_func, is_access, create_time, last_activity
        ) VALUES (
            $1, $1, COALESCE($2::inet, '0.0.0.0'::inet), $2::inet, 0, 1, $3,
            10, 1, 1, '00:00;14:05',
            1, 0, 0, '2.4.1',
            false, 0, 0, now(), now()
        )
        ON CONFLICT (sn) DO NOTHING
        """,
        sn,
        source_ip,
        default_tz,
    )

    terminal = await get_terminal(conn, sn)
    if terminal is None:  # carrera con otra petición del mismo equipo
        raise RuntimeError(f"No se pudo registrar el equipo {sn}")
    return terminal


async def touch_terminal(
    conn: asyncpg.Connection, sn: str, source_ip: str | None
) -> None:
    """Marca actividad del equipo. Alimenta el healthcheck de equipos caídos."""
    await conn.execute(
        """
        UPDATE iclock_terminal
        SET last_activity = now(),
            real_ip = COALESCE($2::inet, real_ip),
            state = 1
        WHERE sn = $1
        """,
        sn,
        source_ip,
    )


async def update_stamps(
    conn: asyncpg.Connection, sn: str, stamp: str | None, op_stamp: str | None,
    capture_stamp: str | None,
) -> None:
    """Persiste los punteros de sincronización que envía el equipo.

    Sin esto el terminal reenviaría siempre desde el principio.
    """
    await conn.execute(
        """
        UPDATE iclock_terminal
        SET log_stamp     = COALESCE($2, log_stamp),
            op_log_stamp  = COALESCE($3, op_log_stamp),
            capture_stamp = COALESCE($4, capture_stamp)
        WHERE sn = $1
        """,
        sn,
        stamp,
        op_stamp,
        capture_stamp,
    )


async def update_terminal_info(
    conn: asyncpg.Connection, sn: str, info: dict[str, str]
) -> None:
    """Actualiza firmware y contadores a partir del parámetro INFO."""
    def as_int(key: str) -> int | None:
        try:
            return int(info[key])
        except (KeyError, ValueError, TypeError):
            return None

    await conn.execute(
        """
        UPDATE iclock_terminal
        SET fw_ver            = COALESCE($2, fw_ver),
            user_count        = COALESCE($3, user_count),
            fp_count          = COALESCE($4, fp_count),
            transaction_count = COALESCE($5, transaction_count),
            push_time         = now()
        WHERE sn = $1
        """,
        sn,
        info.get("firmware"),
        as_int("user_count"),
        as_int("fp_count"),
        as_int("att_count"),
    )


# ---------------------------------------------------------------------------
# Auditoría de payloads crudos
# ---------------------------------------------------------------------------

async def insert_raw(
    conn: asyncpg.Connection,
    *,
    device_sn: str | None,
    source_ip: str | None,
    method: str,
    path: str,
    query: str | None,
    table_name: str | None,
    body: bytes | None,
    body_bytes: int,
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO zk_raw_payload
            (device_sn, source_ip, method, path, query, table_name, body, body_bytes)
        VALUES ($1, $2::inet, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        device_sn, source_ip, method, path, query, table_name, body, body_bytes,
    )
    return row["id"]


async def update_raw_result(
    conn: asyncpg.Connection, raw_id: int, parsed: int, inserted: int, skipped: int
) -> None:
    await conn.execute(
        """
        UPDATE zk_raw_payload
        SET parsed_rows = $2, inserted_rows = $3, skipped_lines = $4
        WHERE id = $1
        """,
        raw_id, parsed, inserted, skipped,
    )


# ---------------------------------------------------------------------------
# Marcaciones
# ---------------------------------------------------------------------------

def to_utc(naive_local: datetime, tz_offset_hours: int) -> datetime:
    """Convierte la hora local del terminal a UTC.

    El equipo envía su hora de pared sin indicar zona. Insertar ese valor tal
    cual en una columna ``timestamptz`` haría que PostgreSQL lo interprete en la
    zona de la sesión, desplazando todas las marcaciones. Aquí se aplica el
    offset del propio equipo (``iclock_terminal.terminal_tz``, hoy -5).
    """
    return naive_local.replace(tzinfo=timezone(timedelta(hours=tz_offset_hours)))


async def insert_transactions(
    conn: asyncpg.Connection,
    *,
    sn: str,
    records: Sequence[AttlogRecord],
    tz_offset_hours: int,
    company_code: str,
) -> int:
    """Inserta marcaciones, ignorando las que ya existen.

    La deduplicación se apoya en el UNIQUE (company_code, emp_code, punch_time)
    que BioTime ya define. Es imprescindible: si el servidor tarda en responder
    "OK", el terminal reenvía el lote completo.

    Devuelve cuántas filas eran realmente nuevas.
    """
    if not records:
        return 0

    emp_codes = [r.pin for r in records]
    punch_times = [to_utc(r.punch_time, tz_offset_hours) for r in records]
    punch_states = [str(r.status) if r.status is not None else "255" for r in records]
    verify_types = [r.verify_mode if r.verify_mode is not None else 0 for r in records]
    work_codes = [r.work_code for r in records]

    rows = await conn.fetch(
        """
        INSERT INTO iclock_transaction (
            emp_code, punch_time, punch_state, verify_type, work_code,
            terminal_sn, terminal_alias, area_alias, upload_time,
            source, purpose, is_attendance, sync_status, company_code,
            emp_id, terminal_id
        )
        SELECT
            u.emp_code,
            u.punch_time,
            u.punch_state,
            u.verify_type,
            u.work_code,
            $1,
            t.alias,
            (SELECT a.area_name FROM personnel_area a WHERE a.id = t.area_id),
            now(),
            1,                                        -- source: equipo
            CASE WHEN e.id IS NOT NULL THEN 9 END,    -- purpose: NULL si no resuelve empleado
            1,                                        -- is_attendance
            0,                                        -- sync_status
            $2,
            e.id,
            t.id
        FROM unnest($3::text[], $4::timestamptz[], $5::text[], $6::int[], $7::text[])
             AS u(emp_code, punch_time, punch_state, verify_type, work_code)
        LEFT JOIN iclock_terminal t ON t.sn = $1
        LEFT JOIN LATERAL (
            SELECT pe.id FROM personnel_employee pe
            WHERE pe.emp_code = u.emp_code
            LIMIT 1
        ) e ON true
        ON CONFLICT (company_code, emp_code, punch_time) DO NOTHING
        RETURNING id
        """,
        sn, company_code, emp_codes, punch_times, punch_states, verify_types, work_codes,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Consulta para Horario360
# ---------------------------------------------------------------------------

async def query_transactions(
    conn: asyncpg.Connection,
    *,
    emp_code: str | None,
    terminal_sn: str | None,
    start: datetime | None,
    end: datetime | None,
    offset: int,
    limit: int,
) -> tuple[int, list[asyncpg.Record]]:
    """Consulta paginada replicando los filtros de BioTime.

    ``start`` y ``end`` son límites absolutos ya calculados por el llamante:
    ``punch_time >= start`` y ``punch_time < end``.

    El orden es por ``id`` para que la paginación sea estable: ordenar por
    ``punch_time`` daría resultados no deterministas entre páginas cuando varias
    marcaciones comparten el mismo instante.
    """
    conditions: list[str] = []
    params: list[Any] = []

    def add(condition_template: str, value: Any) -> None:
        params.append(value)
        conditions.append(condition_template.format(n=len(params)))

    if emp_code:
        add("t.emp_code = ${n}", emp_code)
    if terminal_sn:
        add("t.terminal_sn = ${n}", terminal_sn)
    if start is not None:
        add("t.punch_time >= ${n}", start)
    if end is not None:
        add("t.punch_time < ${n}", end)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = await conn.fetchval(
        f"SELECT count(*) FROM iclock_transaction t {where}", *params
    )

    rows = await conn.fetch(
        f"""
        SELECT t.id, t.emp_code, t.punch_time, t.punch_state, t.verify_type,
               t.work_code, t.terminal_sn, t.terminal_alias, t.area_alias,
               t.longitude, t.latitude, t.gps_location, t.mobile, t.source,
               t.purpose, t.crc, t.is_attendance, t.reserved, t.upload_time,
               t.sync_status, t.sync_time, t.emp_id, t.terminal_id,
               e.first_name, e.last_name
        FROM iclock_transaction t
        LEFT JOIN personnel_employee e ON e.id = t.emp_id
        {where}
        ORDER BY t.id
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """,
        *params, limit, offset,
    )
    return total, rows


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

async def get_auth_user(conn: asyncpg.Connection, username: str) -> asyncpg.Record | None:
    """Usuario de BioTime. Permite que Horario360 conserve sus credenciales."""
    return await conn.fetchrow(
        "SELECT id, username, password, is_active FROM auth_user WHERE username = $1",
        username,
    )
