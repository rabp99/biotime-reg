# Corrección de códigos mal enrolados

## Problema

Horario360 identifica a cada trabajador en el biométrico con
`emp_code = identificador de tipo de documento + número` (`1` = DNI, `2` = CE; p. ej.
`112345678`). A veces el trabajador se enrola en el equipo con otro código (p. ej. `1135`).
Sus marcaciones quedan en `iclock_transaction` bajo ese código y Horario360, que consulta
`/iclock/api/transactions/?emp_code=…`, no las encuentra.

La solución tiene dos partes:

1. **Manual, en el equipo**: volver a registrar al trabajador con el código correcto.
2. **Histórica, aquí**: desde Horario360 (Mantenedor → Trabajadores → "Corregir registro en
   biométrico", solo Administrador) se llama a estas rutas para reasignar las marcaciones del
   código incorrecto al correcto, dentro de un rango de fechas.

Las dos partes se pueden hacer en cualquier orden. Mientras no se vuelva a registrar al
trabajador, el equipo sigue enviando el código incorrecto: la corrección se puede repetir y solo
mueve lo nuevo.

## Reglas

- Se actualizan juntas `emp_code`, `emp_id` y `purpose`, con el mismo criterio que la ingesta:
  `emp_id` = primer `personnel_employee` con el código correcto (o `NULL` si no existe) y
  `purpose = 9` si resuelve, `NULL` si no. **El código correcto no tiene que existir en
  `personnel_employee`**: el API filtra por la columna `emp_code`.
- **Nunca se viola el UNIQUE `(company_code, emp_code, punch_time)`**: si el código correcto ya
  tiene una marcación en el mismo instante (misma empresa), la del código incorrecto se queda
  como está y se informa en `skipped_conflicts` / `conflicts`.
- Rango `start`–`end` inclusivo, en fechas de `API_RENDER_TZ` (America/Lima), igual que
  `start_time`/`end_time` del API: `punch_time >= start 00:00` y `< end + 1 día 00:00`.
- Concurrencia: `pg_advisory_xact_lock` sobre ambos códigos y `FOR UPDATE` sobre las filas.
- Cada corrección queda en `zk_emp_code_correction` (+ `_item` con el valor anterior de cada
  fila) y se puede deshacer.
- Usa su propia conexión (`PG_FIX_DSN`, rol `biotime_reg_fix`). La ingesta y la consulta siguen
  sin permiso para modificar marcaciones.

## API

Puerto del API (8083). Mismas barreras que `/iclock/api/transactions/`:

1. IP en `API_ALLOWED_IPS` → si no, `403`.
2. `CORRECTIONS_ENABLED=true` → si no, `404` (como si no existiera).
3. Token de `/jwt-api-token-auth/` (`Authorization: JWT <token>`) → si no, `401`.

Fechas de entrada `AAAA-MM-DD`. Fechas-hora de salida `AAAA-MM-DD HH:MM:SS` en hora de Lima, sin
zona, como `punch_time` en el API de marcaciones. Códigos: `^[0-9A-Za-z]{1,20}$`.

Errores:

```json
{ "error": { "code": "validation_error", "message": "Datos inválidos.", "fields": { "end": "La fecha final no puede ser anterior a la inicial." } } }
```

| HTTP | `code` | Cuándo |
|---|---|---|
| 404 | `not_found` | Corrección inexistente |
| 409 | `already_undone` | Undo repetido |
| 422 | `validation_error` | Parámetros inválidos (`fields` con el detalle) |
| 422 | `nothing_to_move` | Sin marcaciones del código incorrecto en el rango, o todas en conflicto |
| 503 | `not_configured` | Falta `PG_FIX_DSN` o el rol no conecta |

`message` está en español para mostrarlo tal cual.

### `GET /api/emp-code-corrections/preview?from=1135&to=112345678[&start=2026-08-01&end=2026-10-15]`

Solo lectura. `start`/`end` opcionales (los dos o ninguno).

```json
{
  "from": {
    "emp_code": "1135",
    "employee": null,
    "total_transactions": 143,
    "first_punch": "2026-08-02 07:51:00",
    "last_punch": "2026-10-14 19:03:00",
    "range_transactions": 120,
    "range_first_punch": "2026-08-05 07:49:00",
    "range_last_punch": "2026-10-14 19:03:00",
    "terminals": [ { "sn": "VDE2251700310", "alias": "BIO_05", "count": 120 } ]
  },
  "to": {
    "emp_code": "112345678",
    "employee": { "id": 1540, "emp_code": "112345678", "first_name": "JUAN", "last_name": "PEREZ", "department": "ADMINISTRACION" },
    "total_transactions": 3
  },
  "range": { "start": "2026-08-01", "end": "2026-10-15" },
  "conflicts": 0
}
```

`total_transactions`, `first_punch`, `last_punch`: todo el código incorrecto. `range_*`,
`terminals`, `conflicts`: solo el rango (sin rango, `range` es `null` y coinciden con los totales).

### `POST /api/emp-code-corrections/`

```json
{ "from": "1135", "to": "112345678", "start": "2026-08-01", "end": "2026-10-15",
  "requested_by": "admin@horario360", "reference": "employee:1234", "dry_run": false }
```

`start`, `end`, `requested_by`, `reference` obligatorios (texto ≤ 150). `dry_run` opcional: ejecuta
todo y revierte, sin dejar registro.

`201` (o `200` con `dry_run`):

```json
{ "correction_id": 57, "dry_run": false, "from": "1135", "to": "112345678", "to_employee_id": 1540,
  "range": { "start": "2026-08-01", "end": "2026-10-15" },
  "moved": 120, "skipped_conflicts": 0,
  "first_punch": "2026-08-05 07:49:00", "last_punch": "2026-10-14 19:03:00" }
```

`first_punch`/`last_punch` son de lo movido: es el rango que Horario360 debe recalcular.

### `GET /api/emp-code-corrections/?from=&to=&limit=50`

Historial, más reciente primero (`limit` 1–200):

```json
{ "data": [ { "id": 57, "from": "1135", "to": "112345678", "to_employee_id": 1540,
              "range": { "start": "2026-08-01", "end": "2026-10-15" }, "moved": 120, "skipped_conflicts": 0,
              "first_punch": "…", "last_punch": "…", "requested_by": "…", "reference": "…",
              "created_at": "…", "undone_at": null, "undone_by": null, "restored": null } ] }
```

### `GET /api/emp-code-corrections/{id}`

Un elemento con la misma forma.

### `POST /api/emp-code-corrections/{id}/undo`

```json
{ "requested_by": "admin@horario360" }
```

Restaura `emp_code`, `emp_id` y `purpose` de las filas que siguen con el código correcto (las que
alguien cambió después, o que chocarían con otra marcación del código original, se omiten).

```json
{ "correction_id": 57, "restored": 120, "skipped": 0,
  "first_punch": "2026-08-05 07:49:00", "last_punch": "2026-10-14 19:03:00" }
```

## Instalación en el servidor

Rutas como en `docs/DESPLIEGUE.md` (`C:\apps\biotime-reg`, PostgreSQL de BioTime en `7496`).

1. **Respaldo** de la tabla de marcaciones (pequeña, unos 170 MB):

   ```powershell
   & "C:\ZKBioTime\pgsql\bin\pg_dump.exe" -h 127.0.0.1 -p 7496 -U postgres -Fc -t iclock_transaction -f "D:\respaldos\iclock_transaction_previo_correccion.dump" biotime
   ```

2. **Actualizar el código** en `C:\apps\biotime-reg` (git pull o copia).

3. **Tablas de registro**:

   ```powershell
   & "C:\ZKBioTime\pgsql\bin\psql.exe" -h 127.0.0.1 -p 7496 -U postgres -d biotime -f C:\apps\biotime-reg\sql\004_emp_code_correction.sql
   ```

4. **Rol de corrección**: editar `sql\005_fix_user.sql`, cambiar `CAMBIAR_ESTA_CLAVE` y ejecutar:

   ```powershell
   & "C:\ZKBioTime\pgsql\bin\psql.exe" -h 127.0.0.1 -p 7496 -U postgres -d biotime -f C:\apps\biotime-reg\sql\005_fix_user.sql
   ```

   La salida lista los permisos: `SELECT` sobre `iclock_transaction`, `personnel_employee`,
   `personnel_department`, `iclock_terminal`, las dos tablas `zk_emp_code_correction*`, y
   `UPDATE` solo en `iclock_transaction (emp_code, emp_id, purpose)` y
   `zk_emp_code_correction (undone_at, undone_by, restored)`.

5. **`.env`**:

   ```
   CORRECTIONS_ENABLED=true
   PG_FIX_DSN=postgresql://biotime_reg_fix:LA_CLAVE@127.0.0.1:7496/biotime
   ```

   (Si la clave tiene `@ / ? # % :`, codificarla en la URL; ver `docs/DESPLIEGUE.md`.)

6. **Reiniciar solo el API** (la ingesta no cambia): `Restart-Service biotime-reg-api`.

7. **Comprobar desde el servidor de Horario360** (es la IP autorizada):

   ```bash
   TOKEN=$(curl -s -X POST http://161.132.55.79:8083/jwt-api-token-auth/ \
     -H 'Content-Type: application/json' -d '{"username":"USUARIO","password":"CLAVE"}' | jq -r .token)
   curl -s "http://161.132.55.79:8083/api/emp-code-corrections/preview?from=CODIGO_MALO&to=CODIGO_BUENO" \
     -H "Authorization: JWT $TOKEN"
   ```

   Luego un `POST` con `"dry_run": true` antes de la primera corrección real.

**Rollback**: `CORRECTIONS_ENABLED=false` y reiniciar el API. Una corrección concreta se deshace con
`/undo`; en último caso, el respaldo del paso 1.

## Pruebas

`tests/test_emp_code_corrections.py`. Usan datos sintéticos (`ZZ…`) y los borran. Sin el dump de
producción, con el esquema mínimo:

```powershell
docker run -d --name biotime-pg-min -e POSTGRES_PASSWORD=devlocal -e POSTGRES_DB=biotime -p 55433:5432 postgres:10
Get-Content tests\fixtures\minimal_schema.sql | docker exec -i biotime-pg-min psql -U postgres -d biotime
Get-Content sql\004_emp_code_correction.sql | docker exec -i biotime-pg-min psql -U postgres -d biotime
$env:TEST_PG_DSN = 'postgresql://postgres:devlocal@127.0.0.1:55433/biotime'
python -m pytest tests/test_emp_code_corrections.py -q
```

Para verificar además los permisos del rol, crearlo con `sql\005_fix_user.sql` en esa base y
definir `TEST_PG_FIX_DSN=postgresql://biotime_reg_fix:CLAVE@127.0.0.1:55433/biotime`.
