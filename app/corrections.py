"""
Corrección de códigos mal enrolados: reasigna marcaciones de un emp_code a otro.

Caso real: un trabajador está en Horario360 como 112345678 (identificador de
DNI + documento) pero en el equipo lo enrolaron como 1135. Sus marcaciones
quedan bajo 1135 y Horario360, que consulta por emp_code, no las ve. Aquí se
mueven al código correcto dentro de un rango de fechas.

Decisiones que condicionan el SQL:

- **Se actualizan emp_code, emp_id y purpose juntos.** El API filtra por la
  columna emp_code (más de la mitad de las filas no tiene emp_id), pero las tres
  columnas tienen que quedar coherentes con lo que habría escrito la ingesta:
  emp_id resuelto contra personnel_employee y purpose = 9 si resuelve, NULL si
  no. El código correcto NO tiene que existir en personnel_employee.

- **Nunca se viola el UNIQUE (company_code, emp_code, punch_time).** Si el
  código correcto ya tiene una marcación en el mismo instante (y misma empresa),
  la del código incorrecto se deja como está y se cuenta como conflicto.

- **Rangos en la zona del API.** start/end son fechas en API_RENDER_TZ
  (America/Lima), igual que los start_time/end_time de Horario360. punch_time es
  timestamptz y la sesión de la base de producción está en Asia/Hong_Kong, así
  que los límites se calculan aquí con zona explícita.

- **Concurrencia con advisory locks** sobre ambos códigos, en orden fijo. No se
  bloquea personnel_employee: el rol de corrección no tiene UPDATE ahí.

Todo corre sobre el pool de PG_FIX_DSN (``db.get_fix_pool``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

LOCK_NAMESPACE = "emp-code-correction:"


class NothingToMove(Exception):
    """No hay marcaciones del código origen en el rango que se puedan mover."""

    def __init__(self, in_range: int, conflicts: int):
        super().__init__("nothing_to_move")
        self.in_range = in_range
        self.conflicts = conflicts


class CorrectionNotFound(Exception):
    pass


class AlreadyUndone(Exception):
    pass


@dataclass(frozen=True)
class Range:
    start: date
    end: date

    def bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """[start 00:00, end + 1 día 00:00) en la zona dada."""
        lower = datetime.combine(self.start, time.min, tzinfo=tz)
        upper = datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=tz)
        return lower, upper

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def fmt(value: datetime | None, tz: ZoneInfo) -> str | None:
    """Mismo formato que punch_time en /iclock/api/transactions/."""
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if value else None


async def _lock_codes(conn: asyncpg.Connection, *codes: str) -> None:
    for code in sorted(set(codes)):
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", LOCK_NAMESPACE + code
        )


# ---------------------------------------------------------------------------
# Empleados
# ---------------------------------------------------------------------------

async def find_employee(conn: asyncpg.Connection, emp_code: str) -> asyncpg.Record | None:
    """Mismo criterio que la ingesta: primer empleado con ese emp_code."""
    return await conn.fetchrow(
        """
        SELECT e.id, e.emp_code, e.first_name, e.last_name, d.dept_name
        FROM personnel_employee e
        LEFT JOIN personnel_department d ON d.id = e.department_id
        WHERE e.emp_code = $1
        ORDER BY e.id
        LIMIT 1
        """,
        emp_code,
    )


def _employee_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "emp_code": row["emp_code"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "department": row["dept_name"],
    }


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

async def preview(
    conn: asyncpg.Connection, from_code: str, to_code: str, rng: Range | None, tz: ZoneInfo
) -> dict[str, Any]:
    from_employee = await find_employee(conn, from_code)
    to_employee = await find_employee(conn, to_code)

    totals = await conn.fetchrow(
        """
        SELECT count(*) AS total, min(punch_time) AS first_punch, max(punch_time) AS last_punch
        FROM iclock_transaction WHERE emp_code = $1
        """,
        from_code,
    )
    to_total = await conn.fetchval(
        "SELECT count(*) FROM iclock_transaction WHERE emp_code = $1", to_code
    )

    lower, upper = rng.bounds(tz) if rng else (None, None)
    in_range = await conn.fetchrow(
        """
        SELECT count(*) AS total, min(punch_time) AS first_punch, max(punch_time) AS last_punch,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM iclock_transaction c
                   WHERE c.emp_code = $2
                     AND c.punch_time = t.punch_time
                     AND c.company_code IS NOT DISTINCT FROM t.company_code
               )) AS conflicts
        FROM iclock_transaction t
        WHERE t.emp_code = $1
          AND ($3::timestamptz IS NULL OR t.punch_time >= $3)
          AND ($4::timestamptz IS NULL OR t.punch_time < $4)
        """,
        from_code, to_code, lower, upper,
    )
    terminals = await conn.fetch(
        """
        SELECT t.terminal_sn AS sn, max(t.terminal_alias) AS alias, count(*) AS count
        FROM iclock_transaction t
        WHERE t.emp_code = $1
          AND ($2::timestamptz IS NULL OR t.punch_time >= $2)
          AND ($3::timestamptz IS NULL OR t.punch_time < $3)
        GROUP BY t.terminal_sn
        ORDER BY count(*) DESC
        """,
        from_code, lower, upper,
    )

    return {
        "from": {
            "emp_code": from_code,
            "employee": _employee_dict(from_employee),
            "total_transactions": totals["total"],
            "first_punch": fmt(totals["first_punch"], tz),
            "last_punch": fmt(totals["last_punch"], tz),
            "range_transactions": in_range["total"],
            "range_first_punch": fmt(in_range["first_punch"], tz),
            "range_last_punch": fmt(in_range["last_punch"], tz),
            "terminals": [
                {"sn": r["sn"], "alias": r["alias"], "count": r["count"]} for r in terminals
            ],
        },
        "to": {
            "emp_code": to_code,
            "employee": _employee_dict(to_employee),
            "total_transactions": to_total,
        },
        "range": rng.as_dict() if rng else None,
        "conflicts": in_range["conflicts"],
    }


# ---------------------------------------------------------------------------
# Corrección
# ---------------------------------------------------------------------------

async def correct(
    conn: asyncpg.Connection,
    *,
    from_code: str,
    to_code: str,
    rng: Range,
    tz: ZoneInfo,
    requested_by: str,
    reference: str,
    client_ip: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Mueve las marcaciones y registra la corrección, en una transacción.

    Con ``dry_run`` se ejecuta todo y se revierte al final: los conteos son los
    reales, pero no queda nada escrito (tampoco el registro).
    """
    lower, upper = rng.bounds(tz)
    tx = conn.transaction()
    await tx.start()
    try:
        await _lock_codes(conn, from_code, to_code)

        to_employee = await find_employee(conn, to_code)
        to_emp_id = to_employee["id"] if to_employee else None

        in_range = await conn.fetchval(
            """
            SELECT count(*) FROM iclock_transaction
            WHERE emp_code = $1 AND punch_time >= $2 AND punch_time < $3
            """,
            from_code, lower, upper,
        )

        moved = await conn.fetch(
            """
            WITH src AS (
                SELECT id, emp_code, emp_id, purpose, punch_time, company_code
                FROM iclock_transaction
                WHERE emp_code = $1 AND punch_time >= $3 AND punch_time < $4
                FOR UPDATE
            ),
            movable AS (
                SELECT s.* FROM src s
                WHERE NOT EXISTS (
                    SELECT 1 FROM iclock_transaction c
                    WHERE c.emp_code = $2
                      AND c.punch_time = s.punch_time
                      AND c.company_code IS NOT DISTINCT FROM s.company_code
                )
            )
            UPDATE iclock_transaction t
            SET emp_code = $2,
                emp_id   = $5,
                purpose  = CASE WHEN $5::integer IS NULL THEN NULL ELSE 9 END
            FROM movable m
            WHERE t.id = m.id
            RETURNING t.id, m.emp_code AS old_emp_code, m.emp_id AS old_emp_id,
                      m.purpose AS old_purpose, t.punch_time
            """,
            from_code, to_code, lower, upper, to_emp_id,
        )

        if not moved:
            raise NothingToMove(in_range=in_range, conflicts=in_range)

        punch_times = [r["punch_time"] for r in moved]
        first_punch, last_punch = min(punch_times), max(punch_times)
        skipped = in_range - len(moved)

        correction_id = await conn.fetchval(
            """
            INSERT INTO zk_emp_code_correction (
                from_emp_code, to_emp_code, to_emp_id, range_start, range_end,
                moved, skipped_conflicts, first_punch, last_punch,
                requested_by, reference, client_ip
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::inet)
            RETURNING id
            """,
            from_code, to_code, to_emp_id, rng.start, rng.end,
            len(moved), skipped, first_punch, last_punch,
            requested_by, reference, client_ip,
        )
        await conn.execute(
            """
            INSERT INTO zk_emp_code_correction_item (
                correction_id, transaction_id, old_emp_code, old_emp_id, old_purpose, punch_time
            )
            SELECT $1, u.id, u.old_emp_code, u.old_emp_id, u.old_purpose, u.punch_time
            FROM unnest($2::integer[], $3::text[], $4::integer[], $5::smallint[], $6::timestamptz[])
                 AS u(id, old_emp_code, old_emp_id, old_purpose, punch_time)
            """,
            correction_id,
            [r["id"] for r in moved],
            [r["old_emp_code"] for r in moved],
            [r["old_emp_id"] for r in moved],
            [r["old_purpose"] for r in moved],
            punch_times,
        )
    except BaseException:
        await tx.rollback()
        raise

    if dry_run:
        await tx.rollback()
    else:
        await tx.commit()

    return {
        "correction_id": None if dry_run else correction_id,
        "dry_run": dry_run,
        "from": from_code,
        "to": to_code,
        "to_employee_id": to_emp_id,
        "range": rng.as_dict(),
        "moved": len(moved),
        "skipped_conflicts": skipped,
        "first_punch": fmt(first_punch, tz),
        "last_punch": fmt(last_punch, tz),
    }


# ---------------------------------------------------------------------------
# Deshacer
# ---------------------------------------------------------------------------

async def undo(
    conn: asyncpg.Connection, correction_id: int, requested_by: str, tz: ZoneInfo
) -> dict[str, Any]:
    """Devuelve a su código original las filas que movió una corrección.

    Solo se restauran las filas que siguen con el código destino; si alguien las
    cambió después, se omiten. Tampoco se restaura una fila si el código original
    ya tiene otra marcación en el mismo instante (respetar el UNIQUE).
    """
    async with conn.transaction():
        correction = await conn.fetchrow(
            "SELECT * FROM zk_emp_code_correction WHERE id = $1 FOR UPDATE", correction_id
        )
        if correction is None:
            raise CorrectionNotFound()
        if correction["undone_at"] is not None:
            raise AlreadyUndone()

        await _lock_codes(conn, correction["from_emp_code"], correction["to_emp_code"])

        items = await conn.fetchval(
            "SELECT count(*) FROM zk_emp_code_correction_item WHERE correction_id = $1",
            correction_id,
        )
        restored = await conn.fetch(
            """
            UPDATE iclock_transaction t
            SET emp_code = i.old_emp_code,
                -- El empleado original pudo borrarse después; la FK no lo admitiría.
                emp_id   = (SELECT e.id FROM personnel_employee e WHERE e.id = i.old_emp_id),
                purpose  = i.old_purpose
            FROM zk_emp_code_correction_item i
            WHERE i.correction_id = $1
              AND t.id = i.transaction_id
              AND t.emp_code = $2
              AND NOT EXISTS (
                  SELECT 1 FROM iclock_transaction c
                  WHERE c.emp_code = i.old_emp_code
                    AND c.punch_time = t.punch_time
                    AND c.company_code IS NOT DISTINCT FROM t.company_code
              )
            RETURNING t.id, t.punch_time
            """,
            correction_id, correction["to_emp_code"],
        )
        await conn.execute(
            """
            UPDATE zk_emp_code_correction
            SET undone_at = now(), undone_by = $2, restored = $3
            WHERE id = $1
            """,
            correction_id, requested_by, len(restored),
        )

    punch_times = [r["punch_time"] for r in restored]
    return {
        "correction_id": correction_id,
        "restored": len(restored),
        "skipped": items - len(restored),
        "first_punch": fmt(min(punch_times), tz) if punch_times else None,
        "last_punch": fmt(max(punch_times), tz) if punch_times else None,
    }


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

def correction_dict(row: asyncpg.Record, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "id": row["id"],
        "from": row["from_emp_code"],
        "to": row["to_emp_code"],
        "to_employee_id": row["to_emp_id"],
        "range": {"start": row["range_start"].isoformat(), "end": row["range_end"].isoformat()},
        "moved": row["moved"],
        "skipped_conflicts": row["skipped_conflicts"],
        "first_punch": fmt(row["first_punch"], tz),
        "last_punch": fmt(row["last_punch"], tz),
        "requested_by": row["requested_by"],
        "reference": row["reference"],
        "created_at": fmt(row["created_at"], tz),
        "undone_at": fmt(row["undone_at"], tz),
        "undone_by": row["undone_by"],
        "restored": row["restored"],
    }


async def get_correction(conn: asyncpg.Connection, correction_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM zk_emp_code_correction WHERE id = $1", correction_id)


async def list_corrections(
    conn: asyncpg.Connection, from_code: str | None, to_code: str | None, limit: int
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT * FROM zk_emp_code_correction
        WHERE ($1::text IS NULL OR from_emp_code = $1)
          AND ($2::text IS NULL OR to_emp_code = $2)
        ORDER BY id DESC
        LIMIT $3
        """,
        from_code, to_code, limit,
    )
