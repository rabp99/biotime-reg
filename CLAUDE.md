# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es este proyecto

Servidor de ingesta de marcaciones para 4 terminales ZKTeco SenseFace 7A que
reemplaza a BioTime 8.5. Escribe en la base PostgreSQL que BioTime ya usa y
expone el mismo contrato de API que consume Horario360 (`C:\Users\Usuario\dev\php\horario360`).

Alcance: **marcaciones crudas**. No reproduce el motor de cálculo de BioTime
(tablas `att_*`). Ver `README.md` para despliegue y migración.

## Comandos

```powershell
python -m pytest tests/ -q              # 54 pruebas
python -m pytest tests/test_protocol.py -q   # solo el parser (sin BD)
python -m pytest tests/test_ingest_roundtrip.py::test_reenvio_no_duplica -q

python -m app.main            # ingesta (83) + API (8083)
python -m app.main ingest     # solo ingesta
python -m app.main api        # solo API
```

Las pruebas de contrato y de extremo a extremo necesitan una copia restaurada
de la base en `postgresql://postgres:devlocal@127.0.0.1:55432/biotime`
(contenedor `biotime-pg`). Se saltan solas si no está; los comandos para
levantarla están en el README.

## Arquitectura

Un solo proceso puede servir dos aplicaciones ASGI independientes
(`app/main.py` expone `ingest_app`, `api_app` y `app` combinada):

- **Ingesta** (puerto 83, abierto a internet). Los terminales son clientes HTTP
  y empujan datos; el servidor solo responde. El puerto no es negociable: los 4
  equipos lo tienen grabado en su configuración.
- **API** (puerto 8083, restringido). Replica BioTime 8.5 para Horario360.

En la app combinada **el router del API debe registrarse antes** que el de
ingesta: `/iclock/{subpath:path}` capturaría `/iclock/api/transactions/` y
devolvería `OK` en texto plano en vez de JSON. Hay un test que lo fija.

Flujo de una marcación:

```
terminal → POST /iclock/cdata?table=ATTLOG   (hora local, sin zona)
         → zk_raw_payload                    (cuerpo crudo, siempre primero)
         → parse_attlog()                    (tolerante: salta líneas ilegibles)
         → to_utc(hora, terminal_tz)         (-5 → UTC)
         → iclock_transaction                (ON CONFLICT DO NOTHING)
         → "OK"                              (si no, el equipo reenvía)
```

## Invariantes que no se pueden romper

Cada una está cubierta por un test; romperlas causa fallos silenciosos, no
errores visibles.

1. **Respuesta en texto plano `OK`.** Cualquier otra cosa hace que el terminal
   reenvíe el lote. De ahí que la deduplicación sea obligatoria, no una mejora.

2. **Conversión de zona en la ingesta.** El equipo envía hora de pared sin
   offset. Insertarla tal cual en `punch_time` (`timestamptz`) la interpretaría
   en la zona de la sesión y desplazaría todo 5 horas. Se convierte con
   `iclock_terminal.terminal_tz`.

3. **`punch_time` en el API es cadena plana sin zona** (`Y-m-d H:i:s`), ya en
   hora local. Añadir el offset ISO rompe el `Carbon::parse` de Horario360.

4. **`next` debe ser `null` en la última página.** Horario360 pagina con
   `while ($response['next'])`.

5. **El cuerpo crudo se guarda antes de parsear.** El firmware en producción es
   push 3.0.4S, más nuevo que el que documenta el SDK de referencia (2.x).

6. **`first_name` y `punch_state_display` no están en el manual PDF** pero
   Horario360 los lee. El API real de BioTime devuelve más de lo documentado.

## Realidades de los datos

Descubiertas analizando el dump de producción; condicionan el diseño.

- **`punch_state = 255` en el 96%** de las 471.810 marcaciones: los equipos no
  distinguen entrada de salida. Horario360 usa `UnknownRetriever`
  (`ATTENDANCES_RETRIEVER_DRIVER=unknown_retriever`), que infiere la dirección
  por cercanía al horario esperado e **ignora `punch_state_display`**.
- **7,2% de marcaciones sin `emp_id`** en los equipos activos. El API las expone
  igualmente: filtrar por `emp_id` perdería ~4.000 registros.
- **`emp_code`** = 1 carácter de tipo de documento + número (9 dígitos).
- **Valores por defecto replicados de BioTime**: `source=1`, `is_attendance=1`,
  `sync_status=0`, `company_code='1'`, `work_code='0'`, `purpose=9` cuando el
  empleado resuelve y `NULL` cuando no.
- **`verify_type`**: 1 = huella, 15 = rostro.
- **7 equipos en `iclock_terminal`, solo 4 activos** (`VDE*`, firmware
  ZAM70-NF70VA). Los 3 `CMWB*` se retiraron en julio de 2026.
- **`iclock_terminal.last_activity` no es fiable** — BioTime no lo mantiene al
  día. Para salud de un equipo, usar `max(punch_time)` de `iclock_transaction`.

## Entorno

- **Producción**: Windows Server en `161.132.55.79`, con PostgreSQL en el mismo
  host. `tzdata` es dependencia obligatoria: Windows no trae la base IANA y sin
  ella el API no arranca.
- **Terminales detrás de NAT**: el servidor nunca puede conectarse a ellos. El
  camino PULL (`pyzk`, TCP 4370) es inviable; el backfill se hace con el comando
  `DATA QUERY ATTLOG` del propio protocolo, o reseteando `log_stamp` a `'0'`.
- **`docs/referencia-zkteco/`** — especificación del protocolo PUSH y manual del
  API de BioTime 8.5. Material propietario de ZKTeco bajo confidencialidad:
  excluido por `.gitignore`, no republicar. Es la referencia autoritativa
  cuando el firmware envía algo que el parser no reconoce.
- **`..\_backup-zkteco-sdk\`** (fuera del repositorio) — dump de producción y el
  resto del paquete original de ZKTeco, incluido `Pushdemo04042022`, la
  implementación Java de referencia de la era push 2.x.
