"""
Protocolo PUSH/ADMS de ZKTeco: parseo y construcción de respuestas.

Módulo puro, sin dependencias de BD ni de FastAPI, para poder testearlo aislado.

Referencias:
- sdk/Attendance PUSH Communication Protocol 20230111.pdf (especificación)
- sdk/Pushdemo04042022/.../DataParseUtil.java (implementación de referencia)

Nota sobre variabilidad: el formato exacto de las tramas cambia entre firmwares.
Por eso el parser es tolerante (ignora campos extra, salta líneas que no
reconoce) y devuelve siempre las líneas descartadas para poder auditarlas.
El cuerpo crudo se guarda en BD antes de llamar aquí.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# ---------------------------------------------------------------------------
# Marcaciones (ATTLOG)
# ---------------------------------------------------------------------------

#: Formatos de fecha vistos en distintos firmwares.
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
)


@dataclass(frozen=True, slots=True)
class AttlogRecord:
    """Una marcación tal como la envía el terminal."""

    pin: str
    punch_time: datetime
    status: int | None
    verify_mode: int | None
    work_code: str | None
    raw_line: str


def parse_timestamp(value: str) -> datetime | None:
    """Convierte la marca de tiempo del terminal a datetime naive (hora local del equipo).

    Devuelve None si no se reconoce el formato; el llamante decide qué hacer.
    """
    value = value.strip()
    if not value:
        return None

    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    # Algunos firmwares envían epoch en segundos.
    if value.isdigit():
        try:
            # Rango defensivo: descarta valores absurdos que en realidad son
            # otra cosa (p. ej. un PIN colado en la columna equivocada).
            epoch = int(value)
            if 946_684_800 <= epoch <= 4_102_444_800:  # 2000-01-01 .. 2100-01-01
                return datetime.fromtimestamp(epoch)
        except (ValueError, OSError, OverflowError):
            pass

    return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_attlog(payload: str) -> tuple[list[AttlogRecord], list[str]]:
    """Parsea el cuerpo de un POST con ``table=ATTLOG``.

    Formato por línea, campos separados por tabulador::

        PIN \\t YYYY-MM-DD HH:MM:SS \\t Status \\t VerifyMode \\t WorkCode [\\t reservados...]

    Los campos a partir del quinto se ignoran: varían por modelo y no aportan
    al objetivo (recuperar marcaciones).

    Returns:
        (registros reconocidos, líneas descartadas)
    """
    records: list[AttlogRecord] = []
    skipped: list[str] = []

    for raw_line in payload.splitlines():
        line = raw_line.strip("\r\n")
        if not line.strip():
            continue

        fields = line.split("\t")
        if len(fields) < 2:
            skipped.append(line)
            continue

        pin = fields[0].strip()
        punch_time = parse_timestamp(fields[1])

        # PIN vacío o fecha irreconocible: no se puede identificar la marcación.
        if not pin or punch_time is None:
            skipped.append(line)
            continue

        records.append(
            AttlogRecord(
                pin=pin,
                punch_time=punch_time,
                status=_to_int(fields[2]) if len(fields) > 2 else None,
                verify_mode=_to_int(fields[3]) if len(fields) > 3 else None,
                work_code=fields[4].strip() if len(fields) > 4 and fields[4].strip() else None,
                raw_line=line,
            )
        )

    return records, skipped


# ---------------------------------------------------------------------------
# Enrutado
# ---------------------------------------------------------------------------

def normalize_command(subpath: str) -> str:
    """Extrae el comando del path bajo ``/iclock/``.

    Algunos firmwares añaden extensión (``cdata.aspx``) o segmentos extra.
    Se replica la normalización de PushCommServlet: primer segmento, sin
    extensión, en minúsculas.

        >>> normalize_command("cdata.aspx")
        'cdata'
        >>> normalize_command("getrequest")
        'getrequest'
    """
    first = subpath.strip("/").split("/")[0]
    return first.split(".")[0].lower()


# ---------------------------------------------------------------------------
# Handshake: respuesta a GET /iclock/cdata?options=all
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DeviceStamps:
    """Punteros de sincronización que el terminal usa para saber qué reenviar."""

    stamp: str = "0"           # ATTLOG
    op_stamp: str = "0"        # OPERLOG
    photo_stamp: str = "0"     # ATTPHOTO


def build_handshake(sn: str, stamps: DeviceStamps, options: dict[str, str]) -> str:
    """Construye la respuesta de configuración del handshake inicial.

    El terminal la interpreta como pares ``clave=valor``, una por línea.
    ``options`` permite sobrescribir o añadir claves desde configuración sin
    tocar código: los firmwares difieren en qué esperan.
    """
    lines = [f"GET OPTION FROM: {sn}"]

    values: dict[str, str] = {
        "Stamp": stamps.stamp,
        "OpStamp": stamps.op_stamp,
        "PhotoStamp": stamps.photo_stamp,
        **options,
    }

    lines.extend(f"{key}={value}" for key, value in values.items())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Payloads informativos del terminal
# ---------------------------------------------------------------------------

def parse_options_payload(payload: str) -> dict[str, str]:
    """Parsea ``POST /iclock/cdata?table=options``: capacidades del terminal.

    Llega como pares ``clave=valor`` separados por coma y/o salto de línea,
    a veces envuelto en llaves. Se normaliza todo a un diccionario plano.
    """
    result: dict[str, str] = {}
    for chunk in re.split(r"[,\n\r]+", payload.strip().strip("{}")):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


#: Orden de los campos del parámetro ``INFO`` en GET /iclock/getrequest.
#: Ejemplo real: ``Ver 2.0.1-20150901,1,2,4,192.168.18.212,10,7,12,1,111,1,1,1``
_INFO_FIELDS = (
    "firmware",
    "user_count",
    "fp_count",
    "att_count",
    "ip",
)


def parse_info(info: str) -> dict[str, str]:
    """Parsea el parámetro ``INFO`` que el terminal envía en getrequest.

    Solo se extraen los primeros campos, que son estables entre firmwares.
    El resto varía de modelo a modelo y no interesa para el objetivo.
    """
    parts = [p.strip() for p in info.split(",")]
    return {
        name: parts[index]
        for index, name in enumerate(_INFO_FIELDS)
        if index < len(parts) and parts[index]
    }


# ---------------------------------------------------------------------------
# Decodificación tolerante
# ---------------------------------------------------------------------------

def decode_body(body: bytes, language: str | None = None) -> str:
    """Decodifica el cuerpo de la petición.

    El terminal usa GB2312 cuando su idioma es chino (código 83) y UTF-8 en el
    resto de casos. Se decodifica con ``errors="replace"`` a propósito: perder
    un carácter es preferible a descartar un lote entero de marcaciones por un
    byte mal codificado.
    """
    encoding = "gb2312" if language == "83" else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
