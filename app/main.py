"""
Servidor de marcaciones: reemplaza a BioTime.

Dos responsabilidades, deliberadamente en puertos distintos:

  puerto 83   /iclock/cdata, /iclock/getrequest, ...  ← 4 terminales SenseFace
  puerto 8083 /iclock/api/transactions/, /jwt-...     → Horario360

El 83 tiene que estar abierto a internet para que los equipos entreguen datos.
El API no: separarlo deja el endpoint de credenciales fuera de ese puerto y
permite migrar consumo e ingesta en fases independientes.

Ejecución:
    python -m app.main            # ambos, cada uno en su puerto
    python -m app.main ingest     # solo ingesta
    python -m app.main api        # solo API
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import router as api_router
from app.config import get_settings
from app.corrections_api import router as corrections_router
from app.db import close_pool, get_pool, init_pool
from app.ingest import router as ingest_router

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _warn_if_api_exposed() -> None:
    """Avisa si el API escucha en todas las interfaces sin lista de IPs.

    El host tiene IP pública, así que 0.0.0.0 sin restricción deja el endpoint
    de credenciales alcanzable desde internet: justo lo que separar los puertos
    pretendía evitar.
    """
    settings = get_settings()
    if settings.api_host in ("0.0.0.0", "::") and not settings.api_allowed_ip_set:
        logger.warning(
            "API escuchando en %s:%d sin API_ALLOWED_IPS. El endpoint de "
            "credenciales queda accesible desde cualquier origen que alcance "
            "el puerto. Configurar API_ALLOWED_IPS con la IP de Horario360.",
            settings.api_host, settings.api_port,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


async def _health_payload() -> JSONResponse:
    """Vida del servicio: BD accesible y actividad reciente de cada equipo.

    Devuelve la última marcación por terminal, que es la señal que de verdad
    importa: BioTime dejó de ingerir el 2026-08-02 y nadie se enteró.
    """
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            terminals = await conn.fetch(
                """
                SELECT sn, alias, last_activity,
                       (SELECT max(punch_time) FROM iclock_transaction t
                        WHERE t.terminal_sn = i.sn) AS last_punch
                FROM iclock_terminal i
                WHERE state = 1
                ORDER BY sn
                """
            )
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)

    return JSONResponse({
        "status": "ok",
        "terminals": [
            {
                "sn": row["sn"],
                "alias": row["alias"],
                "last_activity": row["last_activity"].isoformat() if row["last_activity"] else None,
                "last_punch": row["last_punch"].isoformat() if row["last_punch"] else None,
            }
            for row in terminals
        ],
    })


def _build(name: str, *routers) -> FastAPI:
    application = FastAPI(title=name, lifespan=lifespan)
    for router in routers:
        application.include_router(router)
    application.get("/health")(_health_payload)
    return application


#: Solo ingesta. Es lo que escucha en el puerto público 83.
ingest_app = _build("Ingesta ADMS ZKTeco", ingest_router)

#: Solo consulta (y corrección de códigos). Puerto restringido a Horario360.
api_app = _build("API de marcaciones (contrato BioTime 8.5)", api_router, corrections_router)

#: Ambos en un solo proceso. Útil en desarrollo y para pruebas de extremo a
#: extremo. El API va primero: el catch-all /iclock/{subpath:path} de la ingesta
#: capturaría /iclock/api/transactions/ y devolvería "OK" en texto plano.
app = _build("Servidor de marcaciones ZKTeco", api_router, corrections_router, ingest_router)


async def _serve_both() -> None:
    import uvicorn

    settings = get_settings()
    servers = [
        uvicorn.Server(uvicorn.Config(
            ingest_app, host=settings.host, port=settings.port,
            log_level=settings.log_level.lower(),
        )),
        uvicorn.Server(uvicorn.Config(
            api_app, host=settings.api_host, port=settings.api_port,
            log_level=settings.log_level.lower(),
        )),
    ]
    logger.info(
        "Ingesta en %s:%d | API en %s:%d",
        settings.host, settings.port, settings.api_host, settings.api_port,
    )
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    import uvicorn

    _configure_logging()
    settings = get_settings()
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"

    if mode in ("api", "both"):
        _warn_if_api_exposed()

    if mode == "ingest":
        uvicorn.run(ingest_app, host=settings.host, port=settings.port,
                    log_level=settings.log_level.lower())
    elif mode == "api":
        uvicorn.run(api_app, host=settings.api_host, port=settings.api_port,
                    log_level=settings.log_level.lower())
    elif mode == "both":
        asyncio.run(_serve_both())
    else:
        sys.exit(f"Modo desconocido: {mode!r}. Use: ingest | api | both")


if __name__ == "__main__":
    main()
