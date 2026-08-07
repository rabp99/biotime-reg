"""Utilidades compartidas por las pruebas que necesitan la copia restaurada."""

from __future__ import annotations

import os

import pytest

from app.config import get_settings

TEST_DSN = os.environ.get(
    "TEST_PG_DSN", "postgresql://postgres:devlocal@127.0.0.1:55432/biotime"
)

#: Configuración base de las pruebas.
#:
#: Se fija TODA la configuración sensible de forma explícita, incluidas las
#: listas de control de acceso en vacío. Sin esto, las pruebas heredarían el
#: .env de producción —que restringe por IP— y fallarían con 403 en cualquier
#: máquina donde ese fichero exista, como el propio servidor. Una prueba no
#: puede depender de qué ficheros haya alrededor.
BASE_ENV = {
    "PG_DSN": TEST_DSN,
    "JWT_SECRET": "secreto-de-prueba",
    "ALLOWED_SNS": "",
    "ALLOWED_SOURCE_IPS": "",
    "API_ALLOWED_IPS": "",
    "API_RENDER_TZ": "America/Lima",
    "DEVICE_TZ_FALLBACK": "-5",
    "COMPANY_CODE": "1",
    "STORE_RAW_BODIES": "true",
}


def set_test_env(**overrides: str) -> None:
    """Aplica la configuración base de pruebas, con los cambios indicados.

    Las variables de entorno tienen prioridad sobre el .env en
    pydantic-settings, así que fijarlas aquí aísla las pruebas por completo.
    """
    for key, value in {**BASE_ENV, **overrides}.items():
        os.environ[key] = value
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def entorno_aislado():
    """Restaura el entorno tras cada prueba para no contaminar a la siguiente."""
    previo = {key: os.environ.get(key) for key in BASE_ENV}
    yield
    for key, value in previo.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


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
