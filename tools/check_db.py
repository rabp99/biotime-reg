"""
Verificación previa al despliegue: comprueba que la aplicación puede usar la
base de datos que ya existe en el servidor.

Se ejecuta antes de arrancar los servicios, y de nuevo después de cualquier
cambio en la base. No modifica nada: la prueba de escritura se hace dentro de
una transacción que siempre se revierte.

Uso:
    python -m tools.check_db            # lee PG_DSN del .env
    python -m tools.check_db --dsn "postgresql://usuario:clave@127.0.0.1:5432/biotime"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import asyncpg

# Tablas de BioTime sobre las que trabaja la aplicación.
TABLAS_BIOTIME = ("iclock_transaction", "iclock_terminal", "personnel_employee", "auth_user")

# Objetos que crea sql/001_schema.sql.
TABLAS_PROPIAS = ("zk_raw_payload",)

OK, FALLO, AVISO = "  [OK]   ", "  [FALLO]", "  [AVISO]"


class Resultado:
    def __init__(self) -> None:
        self.errores = 0
        self.avisos = 0

    def ok(self, mensaje: str) -> None:
        print(f"{OK} {mensaje}")

    def fallo(self, mensaje: str) -> None:
        print(f"{FALLO} {mensaje}")
        self.errores += 1

    def aviso(self, mensaje: str) -> None:
        print(f"{AVISO} {mensaje}")
        self.avisos += 1


def seccion(titulo: str) -> None:
    print(f"\n--- {titulo} ---")


async def revisar(dsn: str, api_user: str) -> int:
    r = Resultado()

    seccion("Conexión")
    try:
        conn = await asyncpg.connect(dsn, timeout=10)
    except Exception as exc:
        print(f"{FALLO} No se pudo conectar: {exc}")
        print("\n  Revisar: servicio de PostgreSQL arrancado, puerto correcto en PG_DSN,")
        print("  usuario y clave válidos, y que pg_hba.conf permita la conexión.")
        return 1

    try:
        version, base, usuario = await conn.fetchrow(
            "SELECT version(), current_database(), current_user"
        )
        r.ok(f"Conectado a '{base}' como '{usuario}'")
        r.ok(version.split(",")[0])

        # ------------------------------------------------------------------
        seccion("Tablas de BioTime")
        for tabla in TABLAS_BIOTIME:
            existe = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", tabla)
            if existe:
                filas = await conn.fetchval(f'SELECT count(*) FROM "{tabla}"')
                r.ok(f"{tabla}: {filas:,} filas")
            else:
                r.fallo(f"{tabla}: NO EXISTE. ¿Es esta la base de BioTime?")

        # ------------------------------------------------------------------
        seccion("Objetos propios (sql/001_schema.sql)")
        for tabla in TABLAS_PROPIAS:
            existe = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", tabla)
            if existe:
                r.ok(f"{tabla}")
            else:
                r.fallo(f"{tabla}: falta. Ejecutar sql/001_schema.sql")

        indice = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'iclock_transaction_punch_time_idx'"
        )
        if indice:
            r.ok("Índice por punch_time (consultas por rango de fechas)")
        else:
            r.aviso("Sin iclock_transaction_punch_time_idx (opcional): las consultas solo por fecha irán lentas. Ver sql/003_index_punch_time.sql")

        # ------------------------------------------------------------------
        seccion("Deduplicación")
        unico = await conn.fetchval(
            """
            SELECT count(*) FROM pg_constraint
            WHERE conrelid = 'iclock_transaction'::regclass AND contype = 'u'
            """
        )
        if unico:
            r.ok("UNIQUE sobre iclock_transaction presente")
        else:
            r.fallo(
                "Sin restricción UNIQUE: los reenvíos de los terminales duplicarían marcaciones"
            )

        # ------------------------------------------------------------------
        seccion("Permisos de escritura")
        # Transacción que siempre se revierte: comprueba el permiso sin dejar rastro.
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                """
                INSERT INTO iclock_transaction
                    (emp_code, punch_time, punch_state, verify_type,
                     company_code, source, is_attendance, sync_status)
                VALUES ('__check__', now(), '255', 0, '1', 1, 1, 0)
                """
            )
            r.ok("INSERT en iclock_transaction permitido")

            await conn.execute(
                "INSERT INTO zk_raw_payload (method, path, body_bytes) VALUES ('CHECK','/check',0)"
            )
            r.ok("INSERT en zk_raw_payload permitido")

            await conn.execute("UPDATE iclock_terminal SET last_activity = last_activity")
            r.ok("UPDATE en iclock_terminal permitido")
        except Exception as exc:
            r.fallo(f"Sin permiso de escritura: {exc}")
        finally:
            await tx.rollback()
            r.ok("Prueba revertida: no se modificó nada")

        # ------------------------------------------------------------------
        seccion("Equipos registrados")
        equipos = await conn.fetch(
            """
            SELECT sn, alias, state, terminal_tz, real_ip,
                   (SELECT max(punch_time) FROM iclock_transaction t WHERE t.terminal_sn = i.sn) AS ultima
            FROM iclock_terminal i
            ORDER BY state DESC, sn
            """
        )
        if not equipos:
            r.fallo("No hay equipos en iclock_terminal")

        ahora = datetime.now(timezone.utc)
        activos = 0
        for e in equipos:
            if e["state"] != 1:
                continue
            activos += 1
            ultima = e["ultima"]
            if ultima is None:
                r.aviso(f"{e['sn']} ({e['alias']}): sin marcaciones registradas")
                continue
            atraso = ahora - ultima
            texto = f"{e['sn']} ({e['alias']}) tz={e['terminal_tz']} ip={e['real_ip']} última hace {atraso.days}d {atraso.seconds // 3600}h"
            if atraso > timedelta(hours=24):
                r.aviso(texto)
            else:
                r.ok(texto)

        if activos:
            r.ok(f"{activos} equipos activos")

        # ------------------------------------------------------------------
        seccion("Usuario del API")
        # El API valida contra auth_user de BioTime para que Horario360 siga
        # usando las mismas credenciales que hoy, sin tocar su configuración.
        usuario_api = await conn.fetchrow(
            "SELECT username, is_active, password IS NOT NULL AS tiene_clave "
            "FROM auth_user WHERE username = $1",
            api_user,
        )
        if not usuario_api:
            r.fallo(
                f"El usuario '{api_user}' no existe en auth_user. "
                "Horario360 no podría autenticarse."
            )
            activos = await conn.fetch(
                "SELECT username FROM auth_user WHERE is_active ORDER BY id"
            )
            if activos:
                print(f"    Usuarios activos disponibles: "
                      f"{', '.join(u['username'] for u in activos)}")
        elif not usuario_api["is_active"]:
            r.fallo(f"El usuario '{api_user}' está desactivado")
        elif not usuario_api["tiene_clave"]:
            r.fallo(f"El usuario '{api_user}' no tiene contraseña definida")
        else:
            r.ok(f"'{api_user}' existe y está activo")

        # ------------------------------------------------------------------
        seccion("Zona horaria")
        muestra = await conn.fetchrow(
            "SELECT punch_time FROM iclock_transaction ORDER BY id DESC LIMIT 1"
        )
        if muestra:
            utc = muestra["punch_time"].astimezone(timezone.utc)
            lima = muestra["punch_time"].astimezone(timezone(timedelta(hours=-5)))
            r.ok(f"Última marcación en UTC:  {utc:%Y-%m-%d %H:%M:%S}")
            r.ok(f"Misma marcación en Lima:  {lima:%Y-%m-%d %H:%M:%S}")
            print("\n  La hora de Lima debe parecer una hora laboral plausible.")
            print("  Si la razonable fuera la de UTC, cambiar API_RENDER_TZ a UTC en el .env.")
        else:
            r.aviso("Sin marcaciones para verificar la zona horaria")

    finally:
        await conn.close()

    # ----------------------------------------------------------------------
    print("\n" + "=" * 64)
    if r.errores:
        print(f"RESULTADO: {r.errores} fallo(s), {r.avisos} aviso(s). NO arrancar aún.")
        return 1
    if r.avisos:
        print(f"RESULTADO: correcto con {r.avisos} aviso(s). Revisarlos antes de continuar.")
        return 0
    print("RESULTADO: todo correcto. La base está lista.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", help="Cadena de conexión. Por defecto, PG_DSN del .env")
    parser.add_argument("--api-user", default="acruzado", help="Usuario con el que Horario360 se autentica")
    args = parser.parse_args()

    dsn = args.dsn
    if not dsn:
        from app.config import get_settings

        dsn = get_settings().pg_dsn

    # No imprimir la contraseña.
    visible = dsn
    if "@" in dsn:
        visible = dsn.split("://")[0] + "://***@" + dsn.split("@", 1)[1]
    print(f"Verificando: {visible}")

    sys.exit(asyncio.run(revisar(dsn, args.api_user)))


if __name__ == "__main__":
    main()
