"""
API de consulta que replica el contrato de BioTime 8.5.

Horario360 ya habla este contrato (``app/Services/Biometric/ZKTecoDevice.php``),
así que el objetivo es ser indistinguible: mismas rutas, mismos nombres de
campo, mismo formato de fecha. Con eso su ``.env`` no cambia en la migración.

Diferencias respecto al manual PDF, detectadas leyendo el consumidor real:
``first_name`` y ``punch_state_display`` no están documentados pero Horario360
los usa, de modo que se emiten igualmente.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from app import repository, security
from app.config import get_settings
from app.db import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()

#: Cómo BioTime rotula cada punch_state en español.
#: El 255 —el 96% de los registros -- significa "sin definir": el terminal no
#: distingue entrada de salida. Horario360 lo resuelve con UnknownRetriever,
#: que infiere la dirección por cercanía al horario esperado.
PUNCH_STATE_DISPLAY = {
    "0": "Entrada",
    "1": "Salida",
    "2": "Salida Descanso",
    "3": "Entrada Descanso",
    "4": "Entrada Hora Extra",
    "5": "Salida Hora Extra",
}


def _caller_allowed(request: Request) -> bool:
    """Comprueba la IP del llamante contra ``API_ALLOWED_IPS``.

    Segunda barrera además del firewall. El API escucha en 0.0.0.0 porque
    Horario360 corre en otro servidor; si la regla del firewall desaparece —un
    reinstalado, una directiva de grupo— esto evita que el endpoint de
    credenciales quede abierto a internet.

    Lista vacía = sin restricción (desarrollo y pruebas).
    """
    allowed = get_settings().api_allowed_ip_set
    if not allowed:
        return True

    client_ip = request.client.host if request.client else None
    if client_ip in allowed:
        return True

    logger.warning("Llamada al API rechazada desde %s", client_ip)
    return False


def _forbidden() -> JSONResponse:
    return JSONResponse({"detail": "Not allowed."}, status_code=403)


def _parse_boundary(value: str | None) -> datetime | None:
    """Interpreta start_time/end_time en la zona en que responde el API.

    Horario360 envía solo fecha (``Y-m-d``); el manual documenta además fecha y
    hora. Se aceptan ambas.
    """
    if not value:
        return None

    value = value.strip()
    tz = ZoneInfo(get_settings().api_render_tz)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _serialize(row, tz: ZoneInfo) -> dict:
    """Convierte una fila a la forma exacta que devuelve BioTime.

    punch_time y upload_time salen como cadena plana sin zona, ya convertidos a
    hora local. Añadir el offset ISO rompería a Carbon::parse en Horario360, que
    espera el mismo formato que emite BioTime hoy.
    """
    def as_local(value: datetime | None) -> str | None:
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if value else None

    punch_state = row["punch_state"]

    return {
        "id": row["id"],
        "emp_code": row["emp_code"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "punch_time": as_local(row["punch_time"]),
        "punch_state": punch_state,
        "punch_state_display": PUNCH_STATE_DISPLAY.get(punch_state),
        "verify_type": row["verify_type"],
        "work_code": row["work_code"],
        "terminal_sn": row["terminal_sn"],
        "terminal_alias": row["terminal_alias"],
        "area_alias": row["area_alias"],
        "longitude": row["longitude"],
        "latitude": row["latitude"],
        "gps_location": row["gps_location"],
        "mobile": row["mobile"],
        "source": row["source"],
        "purpose": row["purpose"],
        "crc": row["crc"],
        "is_attendance": row["is_attendance"],
        "reserved": row["reserved"],
        "upload_time": as_local(row["upload_time"]),
        "sync_status": row["sync_status"],
        "sync_time": as_local(row["sync_time"]),
        "emp": row["emp_id"],
        "terminal": row["terminal_id"],
    }


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

async def _issue_token(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _caller_allowed(request):
        return _forbidden()

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    username = (payload or {}).get("username") or ""
    password = (payload or {}).get("password") or ""

    pool = get_pool()
    async with pool.acquire() as conn:
        user = await repository.get_auth_user(conn, username)

    # Se verifica el hash aunque el usuario no exista, para que el tiempo de
    # respuesta no revele qué usuarios están dados de alta.
    stored_hash = user["password"] if user else None
    valid = security.verify_django_password(password, stored_hash)

    if not user or not user["is_active"] or not valid:
        logger.warning("Autenticación fallida: usuario=%r", username)
        return JSONResponse(
            {"non_field_errors": ["Unable to log in with provided credentials."]},
            status_code=400,
        )

    token = security.create_token(
        user["username"], user["id"], settings.jwt_secret, settings.jwt_ttl_hours
    )
    return JSONResponse({"token": token})


@router.post("/jwt-api-token-auth/")
async def jwt_token(request: Request) -> JSONResponse:
    """Endpoint que usa Horario360 (ZKTecoDevice::getToken)."""
    return await _issue_token(request)


@router.post("/api-token-auth/")
async def general_token(request: Request) -> JSONResponse:
    """Alternativa documentada en el manual. Mismo comportamiento."""
    return await _issue_token(request)


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "Authentication credentials were not provided."}, status_code=401
    )


# ---------------------------------------------------------------------------
# Marcaciones
# ---------------------------------------------------------------------------

@router.get("/iclock/api/transactions/")
async def transactions(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    emp_code: str | None = None,
    terminal_sn: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    authorization: str | None = Header(None),
) -> JSONResponse:
    settings = get_settings()

    if not _caller_allowed(request):
        return _forbidden()

    token = security.extract_bearer(authorization)
    if not token or not security.decode_token(token, settings.jwt_secret):
        return _unauthorized()

    size = min(page_size or settings.api_default_page_size, settings.api_max_page_size)
    start = _parse_boundary(start_time)
    end = _parse_boundary(end_time)

    pool = get_pool()
    async with pool.acquire() as conn:
        total, rows = await repository.query_transactions(
            conn,
            emp_code=emp_code,
            terminal_sn=terminal_sn,
            start=start,
            end=end,
            offset=(page - 1) * size,
            limit=size,
        )

    tz = ZoneInfo(settings.api_render_tz)

    def page_url(target: int) -> str:
        params = request.query_params._dict.copy()
        params["page"] = str(target)
        return str(request.url.replace_query_params(**params))

    # Horario360 pagina con `while ($response['next'])`: en la última página
    # tiene que ser null o el bucle no termina.
    has_next = (page - 1) * size + len(rows) < total

    return JSONResponse({
        "count": total,
        "next": page_url(page + 1) if has_next else None,
        "previous": page_url(page - 1) if page > 1 else None,
        "msg": "",
        "code": 0,
        "data": [_serialize(row, tz) for row in rows],
    })
