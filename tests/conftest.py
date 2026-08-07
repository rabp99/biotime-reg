"""Utilidades compartidas por las pruebas que necesitan la copia restaurada."""

from __future__ import annotations

import os

import pytest

TEST_DSN = os.environ.get(
    "TEST_PG_DSN", "postgresql://postgres:devlocal@127.0.0.1:55432/biotime"
)


def skip_if_db_unavailable(exc: BaseException) -> None:
    """Salta la prueba solo si el fallo es de conectividad con la base.

    Deliberadamente estrecho: un ``except Exception`` genérico convertiría
    cualquier error de programación —un TypeError, un nombre mal escrito— en un
    "entorno no disponible", y la prueba pasaría a estado saltado sin que nadie
    revisara por qué.
    """
    import asyncpg

    connectivity_errors = (
        OSError,
        ConnectionError,
        asyncpg.PostgresConnectionError,
        asyncpg.InvalidCatalogNameError,
        asyncpg.UndefinedTableError,
    )
    if isinstance(exc, connectivity_errors):
        pytest.skip(f"Base de pruebas no disponible: {exc}")
    raise exc


class ClientIP:
    """Envoltorio ASGI que fija la IP de origen que ve la aplicación.

    ``TestClient`` no permite configurarla en esta versión, y el control de
    acceso del API se apoya en ``request.client.host``. Las peticiones que no
    son HTTP (lifespan) pasan sin tocar, para que el pool de conexiones se
    inicialice con normalidad.
    """

    def __init__(self, app, ip: str, port: int = 50000):
        self.app = app
        self.ip = ip
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(self.ip, self.port))
        await self.app(scope, receive, send)
