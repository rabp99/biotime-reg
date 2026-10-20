"""
Rutas de corrección de códigos mal enrolados, consumidas por Horario360.

Viven en el puerto del API (8083) y reutilizan sus dos barreras: la lista
API_ALLOWED_IPS y el JWT de /jwt-api-token-auth/. Además:

- Van apagadas por defecto (CORRECTIONS_ENABLED). Apagadas responden 404, como
  si no existieran.
- Usan su propia conexión (PG_FIX_DSN), con el único rol que puede modificar
  marcaciones.

Errores siempre como ``{"error": {"code", "message", "fields"?}}``; ``message``
está en español para que Horario360 lo pueda mostrar tal cual.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from app import corrections, security
from app.api import _caller_allowed, _forbidden, _unauthorized
from app.config import get_settings
from app.db import get_fix_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/emp-code-corrections")

#: emp_code es varchar(20) en iclock_transaction.
EMP_CODE_RE = re.compile(r"^[0-9A-Za-z]{1,20}$")
TEXT_MAX = 150


def _error(status: int, code: str, message: str, fields: dict[str, str] | None = None) -> JSONResponse:
    body: dict = {"code": code, "message": message}
    if fields:
        body["fields"] = fields
    return JSONResponse({"error": body}, status_code=status)


def _validation(fields: dict[str, str]) -> JSONResponse:
    return _error(422, "validation_error", "Datos inválidos.", fields)


def _guard(request: Request, authorization: str | None) -> JSONResponse | None:
    """IP, interruptor y token, en ese orden. Devuelve la respuesta de rechazo."""
    if not _caller_allowed(request):
        return _forbidden()

    settings = get_settings()
    if not settings.corrections_enabled:
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    token = security.extract_bearer(authorization)
    if not token or not security.decode_token(token, settings.jwt_secret):
        return _unauthorized()
    return None


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().api_render_tz)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _codes(raw_from: object, raw_to: object, fields: dict[str, str]) -> tuple[str, str]:
    from_code = str(raw_from or "").strip()
    to_code = str(raw_to or "").strip()
    if not EMP_CODE_RE.match(from_code):
        fields["from"] = "Código inválido: solo letras y números, hasta 20 caracteres."
    if not EMP_CODE_RE.match(to_code):
        fields["to"] = "Código inválido: solo letras y números, hasta 20 caracteres."
    if from_code and from_code == to_code:
        fields["to"] = "El código correcto debe ser distinto del incorrecto."
    return from_code, to_code


def _range(raw_start: object, raw_end: object, fields: dict[str, str], required: bool) -> corrections.Range | None:
    if not raw_start and not raw_end:
        if required:
            fields["start"] = "Obligatorio."
            fields["end"] = "Obligatorio."
        return None
    if not raw_start or not raw_end:
        fields["start" if not raw_start else "end"] = "Indique ambas fechas o ninguna."
        return None

    parsed: dict[str, date] = {}
    for key, raw in (("start", raw_start), ("end", raw_end)):
        try:
            parsed[key] = date.fromisoformat(str(raw).strip())
        except ValueError:
            fields[key] = "Fecha inválida, use AAAA-MM-DD."
    if len(parsed) < 2:
        return None
    if parsed["start"] > parsed["end"]:
        fields["end"] = "La fecha final no puede ser anterior a la inicial."
        return None
    return corrections.Range(parsed["start"], parsed["end"])


def _text(raw: object, key: str, fields: dict[str, str]) -> str:
    value = str(raw or "").strip()
    if not value:
        fields[key] = "Obligatorio."
    elif len(value) > TEXT_MAX:
        fields[key] = f"Máximo {TEXT_MAX} caracteres."
    return value


async def _acquire_pool():
    try:
        return await get_fix_pool(), None
    except Exception as exc:  # DSN ausente o rol mal configurado
        logger.error("Pool de corrección no disponible: %s", exc)
        return None, _error(
            503, "not_configured", "La corrección de códigos no está configurada en el servidor."
        )


# ---------------------------------------------------------------------------

@router.get("/preview")
async def preview(
    request: Request,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    start: str | None = None,
    end: str | None = None,
    authorization: str | None = Header(None),
) -> JSONResponse:
    if (rejected := _guard(request, authorization)) is not None:
        return rejected

    fields: dict[str, str] = {}
    from_code, to_code = _codes(from_, to, fields)
    rng = _range(start, end, fields, required=False)
    if fields:
        return _validation(fields)

    pool, error = await _acquire_pool()
    if error:
        return error
    async with pool.acquire() as conn:
        payload = await corrections.preview(conn, from_code, to_code, rng, _tz())
    return JSONResponse(payload)


@router.post("/")
@router.post("", include_in_schema=False)
async def create(request: Request, authorization: str | None = Header(None)) -> JSONResponse:
    if (rejected := _guard(request, authorization)) is not None:
        return rejected

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return _validation({"body": "Se esperaba un objeto JSON."})

    fields: dict[str, str] = {}
    from_code, to_code = _codes(body.get("from"), body.get("to"), fields)
    rng = _range(body.get("start"), body.get("end"), fields, required=True)
    requested_by = _text(body.get("requested_by"), "requested_by", fields)
    reference = _text(body.get("reference"), "reference", fields)
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        fields["dry_run"] = "Debe ser true o false."
    if fields:
        return _validation(fields)

    pool, error = await _acquire_pool()
    if error:
        return error
    try:
        async with pool.acquire() as conn:
            result = await corrections.correct(
                conn,
                from_code=from_code,
                to_code=to_code,
                rng=rng,
                tz=_tz(),
                requested_by=requested_by,
                reference=reference,
                client_ip=_client_ip(request),
                dry_run=dry_run,
            )
    except corrections.NothingToMove as exc:
        message = (
            "No hay marcaciones del código incorrecto en ese rango."
            if exc.in_range == 0
            else "Todas las marcaciones del rango ya existen con el código correcto."
        )
        return _error(422, "nothing_to_move", message)

    logger.info(
        "Corrección %s %s -> %s [%s, %s]: movidas=%d conflictos=%d por=%r ref=%r ip=%s",
        "simulada" if dry_run else f"#{result['correction_id']}",
        from_code, to_code, rng.start, rng.end, result["moved"], result["skipped_conflicts"],
        requested_by, reference, _client_ip(request),
    )
    return JSONResponse(result, status_code=200 if dry_run else 201)


@router.get("/")
@router.get("", include_in_schema=False)
async def index(
    request: Request,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    authorization: str | None = Header(None),
) -> JSONResponse:
    if (rejected := _guard(request, authorization)) is not None:
        return rejected

    pool, error = await _acquire_pool()
    if error:
        return error
    async with pool.acquire() as conn:
        rows = await corrections.list_corrections(conn, from_ or None, to or None, limit)
    tz = _tz()
    return JSONResponse({"data": [corrections.correction_dict(r, tz) for r in rows]})


@router.get("/{correction_id}")
async def show(
    request: Request, correction_id: int, authorization: str | None = Header(None)
) -> JSONResponse:
    if (rejected := _guard(request, authorization)) is not None:
        return rejected

    pool, error = await _acquire_pool()
    if error:
        return error
    async with pool.acquire() as conn:
        row = await corrections.get_correction(conn, correction_id)
    if row is None:
        return _error(404, "not_found", "La corrección no existe.")
    return JSONResponse(corrections.correction_dict(row, _tz()))


@router.post("/{correction_id}/undo")
async def undo(
    request: Request, correction_id: int, authorization: str | None = Header(None)
) -> JSONResponse:
    if (rejected := _guard(request, authorization)) is not None:
        return rejected

    try:
        body = await request.json()
    except Exception:
        body = None
    fields: dict[str, str] = {}
    requested_by = _text((body or {}).get("requested_by") if isinstance(body, dict) else None,
                         "requested_by", fields)
    if fields:
        return _validation(fields)

    pool, error = await _acquire_pool()
    if error:
        return error
    try:
        async with pool.acquire() as conn:
            result = await corrections.undo(conn, correction_id, requested_by, _tz())
    except corrections.CorrectionNotFound:
        return _error(404, "not_found", "La corrección no existe.")
    except corrections.AlreadyUndone:
        return _error(409, "already_undone", "La corrección ya fue deshecha.")

    logger.info(
        "Corrección #%d deshecha: restauradas=%d omitidas=%d por=%r ip=%s",
        correction_id, result["restored"], result["skipped"], requested_by, _client_ip(request),
    )
    return JSONResponse(result)
