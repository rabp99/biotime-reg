"""Pool de conexiones a PostgreSQL."""

from __future__ import annotations

import asyncio

import asyncpg

from app.config import get_settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=settings.pg_dsn,
            min_size=2,
            max_size=10,
            # El servidor sirve a 4 terminales y a Horario360: statements
            # preparados cacheados sin límite no aportan y complican los
            # despliegues con pgbouncer si algún día se añade.
            statement_cache_size=0,
        )
    return _pool


async def close_pool() -> None:
    global _pool, _fix_pool
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _fix_pool is not None:
        await _fix_pool.close()
        _fix_pool = None


# ---------------------------------------------------------------------------
# Pool de corrección (PG_FIX_DSN)
#
# Se crea bajo demanda, en la primera petición de corrección, y no en el
# arranque: si el rol de corrección está mal configurado, el API que consume
# Horario360 tiene que seguir levantando igual.
# ---------------------------------------------------------------------------

_fix_pool: asyncpg.Pool | None = None
_fix_pool_lock = asyncio.Lock()


async def get_fix_pool() -> asyncpg.Pool:
    global _fix_pool
    if _fix_pool is not None:
        return _fix_pool

    async with _fix_pool_lock:
        if _fix_pool is None:
            settings = get_settings()
            if not settings.pg_fix_dsn:
                raise RuntimeError("PG_FIX_DSN no está configurado")
            _fix_pool = await asyncpg.create_pool(
                dsn=settings.pg_fix_dsn,
                min_size=0,
                max_size=3,
                statement_cache_size=0,
                # Una corrección toca como mucho unos miles de filas; si tarda
                # más de un minuto algo va mal y es mejor abortar que bloquear
                # la ingesta.
                server_settings={"statement_timeout": "60000"},
            )
    return _fix_pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("El pool no está inicializado; falta llamar a init_pool()")
    return _pool
