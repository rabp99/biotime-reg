"""Pool de conexiones a PostgreSQL."""

from __future__ import annotations

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
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("El pool no está inicializado; falta llamar a init_pool()")
    return _pool
