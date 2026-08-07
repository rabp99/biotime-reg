"""
Ingesta ADMS: las rutas bajo ``/iclock/`` que hablan con los terminales.

Los equipos son clientes HTTP; este servidor solo responde. Reglas del protocolo
que no se pueden relajar:

- La respuesta es texto plano. ``OK`` = aceptado, cualquier otra cosa = fallo.
- Si el equipo no recibe ``OK``, reenvía el lote entero. De ahí que la
  deduplicación en base de datos sea obligatoria, no una optimización.
- El cuerpo crudo se guarda antes de parsear: el firmware en producción
  (push 3.0.4S) es más nuevo que el que documenta el SDK de referencia.
"""

from __future__ import annotations

import ipaddress
import logging
from email.utils import formatdate

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from app import repository
from app.config import get_settings
from app.db import get_pool
from app.protocol import (
    DeviceStamps,
    build_handshake,
    decode_body,
    normalize_command,
    parse_attlog,
    parse_info,
    parse_options_payload,
)

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_LOG_BYTES = 1_000_000  # no guardar fotos completas en la auditoría


def _text(body: str, status: int = 200) -> Response:
    """Respuesta en texto plano con la cabecera Date que algunos equipos usan."""
    return PlainTextResponse(
        body,
        status_code=status,
        headers={"Date": formatdate(usegmt=True)},
        media_type="text/plain",
    )


def _client_ip(request: Request) -> str | None:
    """IP de origen, validada.

    Se devuelve None si no es una IP parseable. Las columnas de destino son de
    tipo ``inet`` y rechazarían el valor, haciendo fallar toda la petición con
    500; el terminal lo leería como error y reintentaría el lote en bucle. Una
    IP irreconocible no es motivo para perder marcaciones.
    """
    if not request.client:
        return None
    try:
        ipaddress.ip_address(request.client.host)
    except ValueError:
        logger.debug("IP de origen no parseable: %r", request.client.host)
        return None
    return request.client.host


def _authorized(sn: str | None, ip: str | None) -> bool:
    """Filtra por SN e IP de origen.

    El puerto está expuesto a internet sin autenticación —así es el protocolo—,
    de modo que sin este filtro cualquiera que conozca un SN podría inyectar
    marcaciones. Ambas listas vacías desactivan la comprobación.
    """
    settings = get_settings()

    allowed_sns = settings.allowed_sn_set
    if allowed_sns and (not sn or sn not in allowed_sns):
        logger.warning("SN rechazado: sn=%s ip=%s", sn, ip)
        return False

    allowed_ips = settings.allowed_ip_set
    if allowed_ips and (not ip or ip not in allowed_ips):
        logger.warning("IP de origen rechazada: sn=%s ip=%s", sn, ip)
        return False

    return True


@router.api_route("/iclock/{subpath:path}", methods=["GET", "POST"])
async def iclock(subpath: str, request: Request) -> Response:
    """Punto de entrada único, replicando el enrutado de PushCommServlet.

    Algunos firmwares añaden extensión al path (``cdata.aspx``), por eso se
    normaliza en vez de declarar rutas fijas.
    """
    settings = get_settings()
    command = normalize_command(subpath)
    sn = request.query_params.get("SN")
    ip = _client_ip(request)

    if not sn and command in ("cdata", "getrequest", "devicecmd"):
        return _text("error: missing SN")

    if not _authorized(sn, ip):
        # Respuesta deliberadamente escueta: no confirma si el SN existe.
        return _text("error", status=403)

    if command == "cdata":
        if request.method == "GET":
            return await _handshake(sn, ip)
        return await _upload(sn, ip, request)

    if command == "getrequest":
        return await _getrequest(sn, ip, request)

    if command == "devicecmd":
        return await _devicecmd(sn, ip, request)

    # registry, ping, descargas de ficheros... Nada de esto afecta al objetivo
    # (recuperar marcaciones), pero hay que responder OK o el equipo reintenta
    # en bucle. Se registra para descubrir qué usa realmente el firmware 3.0.4S.
    logger.info("Comando no manejado: %s sn=%s ip=%s", command or subpath, sn, ip)
    await _store_raw(request, sn, ip, subpath, body=None)
    return _text("OK")


# ---------------------------------------------------------------------------

async def _store_raw(
    request: Request, sn: str | None, ip: str | None, subpath: str, body: bytes | None
) -> int | None:
    settings = get_settings()
    if not settings.store_raw_bodies:
        return None

    stored = body if body is not None and len(body) <= MAX_BODY_LOG_BYTES else None
    pool = get_pool()
    async with pool.acquire() as conn:
        return await repository.insert_raw(
            conn,
            device_sn=sn,
            source_ip=ip,
            method=request.method,
            path=f"/iclock/{subpath}",
            query=str(request.url.query) or None,
            table_name=request.query_params.get("table"),
            body=stored,
            body_bytes=len(body) if body is not None else 0,
        )


async def _handshake(sn: str, ip: str | None) -> Response:
    """GET /iclock/cdata?SN=..&options=all — configuración inicial del equipo."""
    settings = get_settings()
    pool = get_pool()

    async with pool.acquire() as conn:
        terminal = await repository.ensure_terminal(
            conn, sn, ip, settings.device_tz_fallback
        )
        await repository.touch_terminal(conn, sn, ip)

    stamps = DeviceStamps(
        stamp=terminal["log_stamp"] or "0",
        op_stamp=terminal["op_log_stamp"] or "0",
        photo_stamp=terminal["capture_stamp"] or "0",
    )
    body = build_handshake(sn, stamps, settings.handshake_options)
    logger.info("Handshake sn=%s ip=%s stamp=%s", sn, ip, stamps.stamp)
    return _text(body)


async def _upload(sn: str, ip: str | None, request: Request) -> Response:
    """POST /iclock/cdata — el equipo entrega datos."""
    settings = get_settings()
    table = (request.query_params.get("table") or "").upper()
    body = await request.body()

    raw_id = await _store_raw(request, sn, ip, "cdata", body)

    pool = get_pool()
    async with pool.acquire() as conn:
        terminal = await repository.ensure_terminal(
            conn, sn, ip, settings.device_tz_fallback
        )
        await repository.touch_terminal(conn, sn, ip)
        await repository.update_stamps(
            conn,
            sn,
            request.query_params.get("Stamp"),
            request.query_params.get("OpStamp"),
            request.query_params.get("PhotoStamp"),
        )

        if table != "ATTLOG":
            # OPERLOG, ATTPHOTO, options: no aportan al objetivo. Se confirman
            # para que el equipo avance su puntero y no los reenvíe siempre.
            if table == "OPTIONS":
                capabilities = parse_options_payload(decode_body(body))
                logger.info("Capacidades sn=%s: %s", sn, capabilities)
            logger.info("Tabla ignorada sn=%s table=%s bytes=%d", sn, table, len(body))
            return _text("OK")

        text = decode_body(body, terminal["language"] and str(terminal["language"]))
        records, skipped = parse_attlog(text)

        tz_offset = terminal["terminal_tz"]
        if tz_offset is None:
            tz_offset = settings.device_tz_fallback

        inserted = await repository.insert_transactions(
            conn,
            sn=sn,
            records=records,
            tz_offset_hours=tz_offset,
            company_code=settings.company_code,
        )

        if raw_id is not None:
            await repository.update_raw_result(
                conn, raw_id, len(records), inserted, len(skipped)
            )

    if skipped:
        logger.warning(
            "Líneas no reconocidas sn=%s: %d (muestra: %r)", sn, len(skipped), skipped[:3]
        )
    logger.info(
        "ATTLOG sn=%s recibidas=%d nuevas=%d duplicadas=%d",
        sn, len(records), inserted, len(records) - inserted,
    )
    return _text("OK")


async def _getrequest(sn: str, ip: str | None, request: Request) -> Response:
    """GET /iclock/getrequest — el equipo pregunta si hay comandos pendientes.

    Este servidor solo recupera marcaciones: nunca encola comandos, así que la
    respuesta es siempre ``OK``. Aun así hay que responder, o el equipo entra en
    reintentos y acaba degradando también el envío de datos.
    """
    info = request.query_params.get("INFO")
    pool = get_pool()
    async with pool.acquire() as conn:
        await repository.ensure_terminal(
            conn, sn, ip, get_settings().device_tz_fallback
        )
        await repository.touch_terminal(conn, sn, ip)
        if info:
            await repository.update_terminal_info(conn, sn, parse_info(info))

    return _text("OK")


async def _devicecmd(sn: str, ip: str | None, request: Request) -> Response:
    """POST /iclock/devicecmd — resultado de un comando. Se registra y se acepta."""
    body = await request.body()
    await _store_raw(request, sn, ip, "devicecmd", body)
    logger.info("devicecmd sn=%s: %s", sn, decode_body(body)[:500])
    return _text("OK")
