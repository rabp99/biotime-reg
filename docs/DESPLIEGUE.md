# Despliegue en producción

Guía paso a paso para instalar en `161.132.55.79` (Windows Server) y sustituir a
BioTime 8.5, **reutilizando su misma base de datos PostgreSQL**.

No se usa Docker: todo se instala directamente sobre Windows.

## Antes de empezar

**Tiempo estimado:** 60–90 minutos. La fase 2 corta el servicio unos minutos.

**Qué se sustituye y qué no.** BioTime hace dos cosas: recibe marcaciones de los
terminales y las sirve por API. Esta solución hace exactamente esas dos. **No**
reproduce su motor de cálculo de asistencia (tablas `att_*`: turnos, tardanzas,
horas extra). Si alguien usa esos reportes, no los tendrá tras el corte.

**Antes de la fase 2**, exportar el histórico de BioTime:

```powershell
& "C:\ruta\a\pg_dump.exe" -U postgres -Fc -f "D:\respaldos\biotime_previo_corte.dump" biotime
```

---

## Paso 1 — Software a descargar

| Software | Versión | Descarga |
|---|---|---|
| **Python** | 3.12 o 3.13 · **3.10 mínimo** (64 bits) | https://www.python.org/downloads/windows/ |
| **NSSM** | 2.24 | https://nssm.cc/download |

Nada más. PostgreSQL ya está instalado (lo usa BioTime) y no se toca.

### Python: cuidado con el que ya está instalado

> ⚠️ **El servidor ya tiene Python.** BioTime 8.5 es una aplicación Django y
> trae el suyo, habitualmente **3.7**. No lo desinstales ni lo actualices:
> romperías BioTime, que es tu plan de rollback.

Comprobar qué hay antes de instalar nada:

```powershell
python --version
where.exe python
```

Si sale 3.7, 3.8 o 3.9, **no sirve**: esta aplicación necesita **3.10 como
mínimo** (usa `dataclass(slots=True)` y uniones de tipo `X | None`).

Instala Python 3.12 **al lado**, y en el instalador **desmarca
"Add python.exe to PATH"** para no alterar el entorno de BioTime.

Localizar el intérprete nuevo:

```powershell
py -0p
```

Anota la ruta de la 3.12 (normalmente `C:\Program Files\Python312\python.exe`).
Se usa en el paso 4.

### Síntoma si te equivocas de versión

```
ERROR: Could not find a version that satisfies the requirement fastapi==0.115.6
       (from versions: ... 0.103.0, 0.103.1, 0.103.2)
WARNING: You are using pip version 19.2.3
```

PyPI solo ofrece las versiones compatibles con el intérprete en uso. Si la lista
se corta en `0.103.2` y pip es el `19.2.3`, estás en Python 3.7.

**NSSM:** descomprimir y copiar `win64\nssm.exe` a una carpeta del PATH, por
ejemplo `C:\Windows\System32`. Verificar:

```powershell
nssm version
```

---

## Paso 2 — Reconocer el servidor

No des por hecho ningún nombre ni puerto: averígualos.

### 2.1 Servicios de BioTime

```powershell
Get-Service -DisplayName *BioTime*, *ZK*, *postgres* | Format-Table Name, DisplayName, Status, StartType -AutoSize
```

Aparecerán al menos dos tipos:

- **La aplicación** (nombres tipo `BioTime8.5`, `ZKBioTime`, `Apache-BioTime`,
  `biotime_web`). **Esta es la que se detiene.**
- **PostgreSQL** (nombres tipo `postgresql-x64-13`, `BioTime_PostgreSQL`).
  **Esta se queda encendida**: es la base que vamos a seguir usando.

> ⚠️ Detener PostgreSQL dejaría a la aplicación nueva sin base de datos.
> Anota qué servicio es cuál antes de continuar.

### 2.2 Datos de conexión a PostgreSQL

**BioTime no usa el 5432.** En esta instalación es el **7496**.

La fuente fiable es la propia consola de BioTime:

> **ZKBio Time Platform Service Console** → pestaña **Base de datos**

Muestra servidor, nombre de base, usuario y puerto. La contraseña aparece
oculta; si no la tienes, está en la documentación interna del servidor.

Valores de esta instalación:

| Campo | Valor |
|---|---|
| Motor | PostgreSQL |
| Servidor | `127.0.0.1` |
| Base de datos | `biotime` |
| Usuario | `postgres` |
| **Puerto** | **`7496`** |
| Instalación | `C:\ZKBioTime` |

Para confirmarlo desde PowerShell:

```powershell
Get-NetTCPConnection -State Listen | Where-Object { (Get-Process -Id $_.OwningProcess -EA SilentlyContinue).ProcessName -match 'postgres' } | Select-Object LocalAddress, LocalPort -Unique
```

### 2.4 Localizar psql.exe

Hace falta para ejecutar los scripts SQL. Viene con la instalación de BioTime:

```powershell
Get-ChildItem C:\ZKBioTime -Recurse -Filter psql.exe -EA SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
```

Anota la ruta: se usa en los pasos 5 y 7.

### 2.3 Quién ocupa el puerto 83

```powershell
Get-NetTCPConnection -LocalPort 83 -State Listen | Select-Object LocalPort, OwningProcess, @{n='proceso';e={(Get-Process -Id $_.OwningProcess).ProcessName}}
```

Debe salir el proceso de BioTime. Es el puerto que hay que liberar en la fase 2.

---

## Paso 3 — Copiar el proyecto

```powershell
New-Item -ItemType Directory -Force -Path C:\apps
cd C:\apps
git clone <url-del-repositorio> biotime-reg
cd biotime-reg
```

Sin git, copiar la carpeta por red y descomprimirla en `C:\apps\biotime-reg`.

---

## Paso 4 — Instalar dependencias

Usar la **ruta completa** del Python 3.12 localizado en el paso 1. Escribir solo
`python` tomaría el de BioTime.

```powershell
cd C:\apps\biotime-reg
& "C:\Program Files\Python312\python.exe" -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

Debe imprimir `Python 3.12.x`. Si dice otra cosa, la ruta no era la correcta:
borra el entorno con `Remove-Item -Recurse -Force .venv` y repite.

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`tzdata` no es opcional: Windows no trae la base IANA de zonas horarias y sin
ella el API falla al arrancar con `ZoneInfoNotFoundError: America/Lima`.

Comprobar que el código está sano (36 pruebas que no necesitan base de datos):

```powershell
python -m pytest tests/test_protocol.py -q
```

---

## Paso 5 — Usuario de base de datos

Dos caminos. **El primero es el recomendado.**

### Opción A — Usuario dedicado (recomendado)

La aplicación solo necesita 6 de las 192 tablas. Un usuario propio con permisos
mínimos evita que un fallo pueda tocar el resto, y no obliga a averiguar la
contraseña de BioTime.

Editar `sql\002_app_user.sql` y cambiar `CAMBIAR_ESTA_CLAVE`. Luego, como
superusuario:

```powershell
& "C:\ruta\a\psql.exe" -U postgres -d biotime -f C:\apps\biotime-reg\sql\002_app_user.sql
```

`psql.exe` está en el `bin\` de la instalación de PostgreSQL que usa BioTime.

### Opción B — Reutilizar las credenciales de BioTime

Si no tienes acceso de superusuario, busca la configuración de BioTime en su
carpeta de instalación (`settings.py`, `config.ini` o similar bajo
`C:\Program Files\BioTime*` o `C:\BioTime*`).

Funciona, pero da a la aplicación permisos de superusuario que no necesita.

---

## Paso 6 — Configuración (`.env`)

El fichero `.env` lleva las credenciales y **no está en el repositorio**
(excluido por `.gitignore`). Hay que llevarlo al servidor aparte del código.

### 6.1 Copiarlo al servidor

Si ya lo tienes preparado en la máquina de desarrollo
(`C:\Users\Usuario\dev\biotime-reg\.env`), cópialo a `C:\apps\biotime-reg\.env`.

Por carpeta compartida o RDP:

```powershell
Copy-Item '\\ruta\compartida\.env' 'C:\apps\biotime-reg\.env'
```

Por SSH, si está habilitado:

```powershell
scp .env usuario@161.132.55.79:C:/apps/biotime-reg/.env
```

> **No lo envíes por correo, chat ni lo subas a ningún repositorio.** Contiene
> la contraseña de la base de datos en texto plano.

Si no lo tienes preparado, créalo desde la plantilla:

```powershell
Copy-Item .env.example .env
notepad .env
```

### 6.2 Valores a completar

```ini
# Datos del paso 2.2. El puerto es 7496, no 5432.
# Usuario: el dedicado del paso 5 (opción A) o 'postgres' (opción B).
PG_DSN=postgresql://USUARIO:CLAVE@127.0.0.1:7496/biotime

# Generar uno propio, ver más abajo. No reutilizar entre entornos.
JWT_SECRET=...
```

El resto de valores ya vienen correctos en la plantilla: los SN de los 4
equipos, las IPs de las dos sedes, la de Horario360 y los puertos.

Generar el secreto del API:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 6.3 Contraseñas con caracteres especiales

`PG_DSN` es una URL. Si la contraseña contiene `@`, `/`, `?`, `#`, `%` o `:`,
hay que codificarla:

```powershell
python -c "from urllib.parse import quote; print(quote(input('clave: '), safe=''))"
```

Caracteres como `*`, `.`, `-` o `_` no necesitan codificación.

### 6.4 Comprobar que se lee bien

```powershell
python -c "from app.config import get_settings; s=get_settings(); import urllib.parse as u; p=u.urlsplit(s.pg_dsn); print(f'host={p.hostname} puerto={p.port} base={p.path.lstrip(chr(47))} usuario={p.username}')"
```

Debe imprimir `puerto=7496`. Si sale `puerto=None` o el valor por defecto, el
`.env` no se está leyendo o el DSN está mal escrito.

---

## Paso 7 — Crear la tabla propia

### Qué le hace este script a tu base de datos

| | |
|---|---|
| **Crea** | 1 tabla nueva: `zk_raw_payload`, más sus 2 índices |
| **Modifica** | **Nada.** Ninguna tabla de BioTime cambia de estructura: no se añaden, quitan ni alteran columnas |
| **Borra** | **Nada** |

Las marcaciones siguen guardándose en `iclock_transaction` y los equipos en
`iclock_terminal`, exactamente como hace BioTime. `zk_raw_payload` solo guarda
una copia del cuerpo crudo de cada petición, para poder reprocesar si el
firmware envía algo que el parser no reconoce.

```powershell
& "C:\ruta\a\psql.exe" -U postgres -d biotime -f C:\apps\biotime-reg\sql\001_schema.sql
```

Es idempotente: se puede repetir sin efectos.

Para revertirlo por completo basta con `DROP TABLE zk_raw_payload;` — BioTime
queda exactamente como estaba.

### Índice opcional (`sql\003_index_punch_time.sql`)

Es el **único** cambio que toca una tabla de BioTime, y por eso va aparte. Añade
un índice a `iclock_transaction` para las dos consultas de Horario360 que
filtran solo por fecha sin `emp_code`. No modifica datos ni columnas.

No hace falta para arrancar. Cuando lo ejecutes, usa `CONCURRENTLY` —ya viene en
el script— para no bloquear las escrituras mientras se construye:

```powershell
& "C:\ruta\a\psql.exe" -U postgres -d biotime -f C:\apps\biotime-reg\sql\003_index_punch_time.sql
```

---

## Paso 8 — Verificar la conexión ✅

**Este es el paso que confirma que la aplicación habla con la base existente.**

```powershell
cd C:\apps\biotime-reg
.\.venv\Scripts\Activate.ps1
python -m tools.check_db
```

Salida esperada:

```
--- Conexión ---
  [OK]    Conectado a 'biotime' como 'biotime_reg'
  [OK]    PostgreSQL 13.x ...

--- Tablas de BioTime ---
  [OK]    iclock_transaction: 471,810 filas
  [OK]    iclock_terminal: 7 filas
  [OK]    personnel_employee: 1,548 filas
  [OK]    auth_user: 1 filas

--- Objetos propios (sql/001_schema.sql) ---
  [OK]    zk_raw_payload
  [OK]    zk_api_client
  [OK]    Índice por punch_time

--- Deduplicación ---
  [OK]    UNIQUE sobre iclock_transaction presente

--- Permisos de escritura ---
  [OK]    INSERT en iclock_transaction permitido
  [OK]    INSERT en zk_raw_payload permitido
  [OK]    UPDATE en iclock_terminal permitido
  [OK]    Prueba revertida: no se modificó nada

--- Equipos registrados ---
  [OK]    4 equipos activos

--- Usuario del API ---
  [OK]    'acruzado' existe y está activo

--- Zona horaria ---
  [OK]    Última marcación en UTC:  2026-08-03 04:00:15
  [OK]    Misma marcación en Lima:  2026-08-02 23:00:15

RESULTADO: todo correcto. La base está lista.
```

La prueba de escritura ocurre dentro de una transacción que **siempre se
revierte**: comprueba los permisos sin dejar rastro.

**No continuar con ningún `[FALLO]`.** Ver *Diagnóstico* al final.

Sobre la zona horaria: la hora de Lima debe parecer una hora laboral plausible.
Si la razonable fuera la de UTC, cambiar `API_RENDER_TZ=UTC` en el `.env`.

---

## Paso 9 — Instalar los servicios

```powershell
# Consola como Administrador
cd C:\apps\biotime-reg
.\deploy\install-services.ps1
```

Crea dos servicios independientes, con reinicio automático y rotación de logs:

| Servicio | Puerto | Función |
|---|---|---|
| `biotime-reg-api` | 8083 | Sirve a Horario360 |
| `biotime-reg-ingest` | 83 | Recibe de los terminales |

Separados a propósito: permite revertir uno sin tocar el otro.

Firewall:

```powershell
.\deploy\configure-firewall.ps1
```

Abre el 83 solo a las dos sedes y el 8083 solo a Horario360.

---

## Fase 1 — Migrar el API (sin tocar los terminales)

BioTime sigue funcionando durante toda esta fase. Reversible cambiando una línea.

### 1.1 Arrancar solo el API

```powershell
Start-Service biotime-reg-api
Get-Service biotime-reg-api
```

### 1.2 Comprobar en local

```powershell
curl.exe http://127.0.0.1:8083/health
```

Devuelve los 4 equipos con su última marcación.

### 1.3 Comprobar desde Horario360

Desde el servidor `161.132.68.120`:

```bash
curl http://161.132.55.79:8083/health

# Token con las mismas credenciales que ya usa
curl -X POST http://161.132.55.79:8083/jwt-api-token-auth/ \
  -H "Content-Type: application/json" \
  -d '{"username":"acruzado","password":"LA_CLAVE_ACTUAL"}'

# Marcaciones
curl "http://161.132.55.79:8083/iclock/api/transactions/?page_size=2" \
  -H "Authorization: JWT <token>"
```

Comparar el `punch_time` con el que devuelve BioTime hoy en el puerto 83. **Debe
ser idéntico**, con formato `2026-08-02 23:00:15` (sin zona). Si difieren en
5 horas, ajustar `API_RENDER_TZ` y reiniciar el servicio.

### 1.4 Reapuntar Horario360

En su `.env`:

```ini
ZKTECO_API_URL=http://161.132.55.79:8083/
```

```bash
php artisan config:clear
```

Verificar en la interfaz que las marcaciones siguen apareciendo.

> **Rollback fase 1:** devolver `ZKTECO_API_URL` a `http://161.132.55.79:83/` y
> `php artisan config:clear`.

---

## Fase 2 — Migrar la ingesta

Aquí se corta BioTime. Hacerlo fuera de horario de marcación.

### 2.1 Respaldo

```powershell
& "C:\ruta\a\pg_dump.exe" -U postgres -Fc -f "D:\respaldos\biotime_previo_corte.dump" biotime
```

### 2.2 Detener BioTime

**Solo el servicio de la aplicación. PostgreSQL se queda encendido.**

```powershell
Stop-Service '<NombreDelServicioBioTime>'

# Impedir que se relance solo tras un reinicio y vuelva a tomar el puerto 83
Set-Service '<NombreDelServicioBioTime>' -StartupType Manual
```

Confirmar que PostgreSQL sigue vivo:

```powershell
Get-Service | Where-Object { $_.Name -match 'postgres' } | Format-Table Name, Status
python -m tools.check_db          # debe seguir en verde
```

### 2.3 Confirmar que el puerto 83 quedó libre

```powershell
Get-NetTCPConnection -LocalPort 83 -State Listen -EA SilentlyContinue
```

Sin resultados = libre. Si algo sigue escuchando, identificar y detener antes de
continuar.

### 2.4 Arrancar la ingesta

```powershell
Start-Service biotime-reg-ingest
Get-Service biotime-reg-*
```

### 2.5 Ver llegar las marcaciones

Los terminales consultan cada pocos minutos. Al conectar, **entregan todo lo
acumulado desde que BioTime dejó de ingerir** (2026-08-02): unas 5 jornadas,
alrededor de 10.000 marcaciones.

```powershell
Get-Content C:\apps\biotime-reg\logs\biotime-reg-ingest.log -Tail 50 -Wait
```

Líneas esperadas:

```
Handshake sn=VDE2251700310 ip=190.119.162.162 stamp=1767455068
ATTLOG sn=VDE2251700310 recibidas=847 nuevas=847 duplicadas=0
```

Y en base de datos:

```sql
SELECT terminal_sn, count(*), max(punch_time)
FROM iclock_transaction
WHERE upload_time > now() - interval '1 hour'
GROUP BY terminal_sn;
```

> **Rollback fase 2:**
> ```powershell
> Stop-Service biotime-reg-ingest
> Set-Service '<NombreDelServicioBioTime>' -StartupType Automatic
> Start-Service '<NombreDelServicioBioTime>'
> ```
> Los terminales vuelven a BioTime en su siguiente ciclo. No se pierde nada: lo
> que ya se insertó queda, y el resto sigue en el buffer de los equipos.

---

## Paso 10 — Verificación final

| Comprobación | Comando | Esperado |
|---|---|---|
| Servicios activos | `Get-Service biotime-reg-*` | Ambos `Running` |
| Salud | `curl.exe http://127.0.0.1:8083/health` | `"status":"ok"`, 4 equipos |
| Los 4 equipos entregan | consulta SQL de 2.5 | 4 filas, `max(punch_time)` reciente |
| Sin duplicados | ver abajo | 0 filas |
| Horario360 lee | su interfaz | marcaciones del día |
| Sobreviven al reinicio | `Restart-Computer` | ambos servicios arrancan solos |

```sql
-- No debe devolver nada: el índice único lo impide
SELECT emp_code, punch_time, count(*)
FROM iclock_transaction
WHERE punch_time > now() - interval '2 days'
GROUP BY 1, 2 HAVING count(*) > 1;
```

---

## Operación continua

**Vigilar que sigan entrando marcaciones.** BioTime estuvo 5 días sin ingerir y
nadie se enteró. Conviene una alerta si `last_punch` de `/health` se atrasa más
de unas horas en día laborable.

**Reforzar un rango de fechas.** Poner el stamp a `0` hace que el equipo reenvíe
desde el principio de su buffer; los duplicados los absorbe el índice único:

```sql
UPDATE iclock_terminal SET log_stamp = '0' WHERE sn = 'VDE2251700310';
```

**Purgar la auditoría** (mensual). `zk_raw_payload` crece sin límite; BioTime
dejó `iclock_terminalcommandlog` con 34,8 millones de filas por no purgar nunca:

```sql
DELETE FROM zk_raw_payload WHERE received_at < now() - interval '90 days';
```

---

## Diagnóstico

**`check_db` no conecta.** Revisar que el servicio de PostgreSQL esté arrancado,
que el puerto del `PG_DSN` coincida con el del paso 2.2, y que `pg_hba.conf`
acepte conexiones desde `127.0.0.1`.

**`ZoneInfoNotFoundError: America/Lima`.** Falta `tzdata`:
`pip install tzdata`.

**El servicio no arranca.** Mirar `logs\biotime-reg-*.err.log`. Causa habitual:
`.env` ausente o `PG_DSN` mal escrito.

**Puerto 83 ocupado.** Algo de BioTime sigue vivo:
```powershell
Get-Process -Id (Get-NetTCPConnection -LocalPort 83 -State Listen).OwningProcess
```

**Los terminales no entregan nada.** En orden:
1. ¿Llegan peticiones? `Get-Content logs\biotime-reg-ingest.log -Tail 50 -Wait`
2. ¿Rechazadas por IP? Buscar `IP de origen rechazada` en el log. Si las sedes
   cambiaron de IP pública, actualizar `ALLOWED_SOURCE_IPS`.
3. ¿Rechazadas por SN? Buscar `SN rechazado`.
4. ¿Ni siquiera llegan? Revisar el firewall y que el equipo apunte a
   `161.132.55.79:83` en *Menú → Comunicación → Conf. Srvr. de Nube*.

**Llegan pero no se guardan.** El cuerpo crudo queda registrado aunque el parseo
falle. El firmware en producción es push **3.0.4S**, más nuevo que el que
documenta el SDK de referencia (2.x):

```sql
SELECT id, table_name, parsed_rows, inserted_rows, skipped_lines,
       convert_from(body, 'UTF8') AS contenido
FROM zk_raw_payload
WHERE table_name = 'ATTLOG' AND skipped_lines > 0
ORDER BY received_at DESC LIMIT 5;
```

**Horario360 recibe horas desplazadas 5 horas.** Cambiar `API_RENDER_TZ` entre
`America/Lima` y `UTC`, y `Restart-Service biotime-reg-api`.

**Horario360 da error al leer la respuesta.** Comprobar que no está pidiendo al
puerto 83: ahí responde la ingesta en texto plano, no el API en JSON.
