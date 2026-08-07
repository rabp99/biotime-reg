"""Servidor de ingesta de marcaciones ZKTeco."""

import sys

#: Versión mínima real, no aspiracional:
#:   - dataclass(slots=True) existe desde 3.10
#:   - FastAPI evalúa las anotaciones en tiempo de ejecución, y las uniones
#:     `X | None` solo son válidas desde 3.10
#:   - zoneinfo desde 3.9
MIN_PYTHON = (3, 10)

if sys.version_info < MIN_PYTHON:
    actual = ".".join(map(str, sys.version_info[:3]))
    minima = ".".join(map(str, MIN_PYTHON))
    raise RuntimeError(
        f"Se requiere Python {minima} o superior; se está usando {actual}\n"
        f"  Intérprete: {sys.executable}\n\n"
        "  En el servidor suele haber un Python antiguo que pertenece a BioTime.\n"
        "  No lo actualices: instala Python 3.12 aparte y crea el entorno con su\n"
        "  ruta completa. Ver docs/DESPLIEGUE.md, pasos 1 y 4."
    )
