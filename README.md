# biotime-reg

Servidor de marcaciones para 4 terminales ZKTeco SenseFace 7A. Reemplaza a
BioTime 8.5 en dos funciones: recibe las marcaciones que empujan los equipos y
las sirve a Horario360 con el mismo contrato que BioTime, para que Horario360
no tenga que cambiar.

## Qué hace y qué no

Recupera **marcaciones crudas**. No reproduce el motor de cálculo de BioTime
(turnos, tardanzas, horas extra: las tablas `att_*`). Ese cálculo lo hace
Horario360.

## Arquitectura

```
4× SenseFace 7A ──ADMS/HTTP :83──► ingesta ──┐
  BIO_05,06,07 → 190.119.162.162             ├──► PostgreSQL
  BIO_08       → 38.56.214.116               │    iclock_transaction
                                             │    iclock_terminal
Horario360 ──HTTP :8083──────────► API ──────┘
```

Los terminales están detrás de NAT: **el servidor nunca puede conectarse a
ellos**. Por eso la ingesta es PUSH y no hay opción de sondeo (puerto 4370).

Dos puertos a propósito:

| Puerto | Sirve | Exposición |
|---|---|---|
| **83** | `/iclock/cdata`, `/iclock/getrequest`, `/iclock/devicecmd` | Internet. Obligatorio: los equipos lo tienen grabado |
| **8083** | `/iclock/api/transactions/`, `/jwt-api-token-auth/` | Solo Horario360. Nunca a internet |

Separarlos mantiene el endpoint de credenciales fuera del puerto público y
permite migrar consumo e ingesta por separado.

## Módulos

| Fichero | Responsabilidad |
|---|---|
| `app/protocol.py` | Parseo del protocolo ADMS. Puro, sin BD ni HTTP |
| `app/ingest.py` | Rutas `/iclock/*` que hablan con los terminales |
| `app/api.py` | Contrato BioTime 8.5 que consume Horario360 |
| `app/repository.py` | SQL sobre `iclock_transaction` / `iclock_terminal` |
| `app/security.py` | Verificación de contraseñas Django + JWT |
| `sql/001_schema.sql` | Añadidos sobre la base de BioTime (idempotente) |

## Despliegue

Para instalar en el servidor de producción, seguir
**[`docs/DESPLIEGUE.md`](docs/DESPLIEGUE.md)**: guía paso a paso con enlaces de
descarga, migración en dos fases, verificación y diagnóstico.

Lo que sigue es el resumen para desarrollo o consulta rápida.

## Instalación (Windows Server)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
# Editar .env: PG_DSN y JWT_SECRET como mínimo
python -c "import secrets; print(secrets.token_urlsafe(48))"   # para JWT_SECRET

psql -U postgres -d biotime -f sql\001_schema.sql
```

`tzdata` no es opcional: Windows no trae la base IANA de zonas horarias y sin
ella el API falla al arrancar con `ZoneInfoNotFoundError: America/Lima`.

Arranque:

```powershell
python -m app.main            # ambos puertos
python -m app.main ingest     # solo ingesta (83)
python -m app.main api        # solo API (8083)
```

Como servicio de Windows (requiere [NSSM](https://nssm.cc/download) en el PATH y
consola de Administrador):

```powershell
.\deploy\install-services.ps1            # instala ambos servicios
.\deploy\install-services.ps1 -Only api  # solo el API (fase 1)
```

Crea `biotime-reg-ingest` y `biotime-reg-api` por separado, con reinicio
automático y rotación de logs en `logs\`. Separados para poder revertir uno sin
tocar el otro.

## Migración desde BioTime

Los terminales aceptan **un solo servidor ADMS**. En dos fases, cada una
reversible por separado.

### Fase 1 — API (sin tocar los equipos)

1. Abrir el puerto 8083 solo para Horario360:
   ```powershell
   .\deploy\configure-firewall.ps1
   ```
2. Arrancar solo el API: `Start-Service biotime-reg-api`
3. Comprobar desde el servidor de Horario360 (`161.132.68.120`) que responde:
   ```bash
   curl http://161.132.55.79:8083/health
   ```
4. En el `.env` de Horario360, cambiar una línea y limpiar la caché:
   ```
   ZKTECO_API_URL=http://161.132.55.79:8083/
   ```
   ```bash
   php artisan config:clear
   ```

**Rollback**: devolver `ZKTECO_API_URL` a `http://161.132.55.79:83/`.

> La zona de renderizado (`API_RENDER_TZ=America/Lima`) está confirmada de forma
> indirecta: los márgenes de `UnknownRetriever` son de ±120 minutos, y si
> BioTime devolviera UTC el desfase sería de 300, con lo que Horario360 no
> emparejaría ninguna marcación. Como funciona hoy, devuelve hora local.

### Fase 2 — Ingesta

1. Detener el servicio de BioTime y confirmar que el puerto 83 queda libre:
   `netstat -ano | findstr :83`
2. Arrancar la ingesta: `python -m app.main`
3. Los equipos entregan su backlog en el siguiente ciclo. Vigilar:
   ```powershell
   curl http://127.0.0.1:8083/health
   ```

**Rollback**: detener este servicio y relevantar BioTime en el 83.

**Antes de la fase 2**: exportar el histórico de BioTime. Sus tablas `att_*`
dejarán de mantenerse al apagar su motor de cálculo.

## Verificación

```powershell
python -m pytest tests/ -q
```

Las pruebas de `test_api_contract.py` y `test_ingest_roundtrip.py` necesitan una
copia de la base restaurada; se saltan solas si no la encuentran. Levantarla:

```powershell
$dump = 'C:\Users\Usuario\dev\_backup-zkteco-sdk\dump'
docker run -d --name biotime-pg -e POSTGRES_PASSWORD=devlocal -e POSTGRES_DB=biotime `
  -p 55432:5432 -v "${dump}:/dump:ro" postgres:18
docker exec biotime-pg pg_restore -U postgres -d biotime --no-owner -j 4 /dump/<fichero>.dump
docker cp sql\001_schema.sql biotime-pg:/tmp/
docker exec biotime-pg psql -U postgres -d biotime -f /tmp/001_schema.sql
```

El dump contiene datos de producción y vive fuera del repositorio.

## Operación

**Salud del sistema.** `GET /health` devuelve la última marcación por terminal.
Es la señal que importa: BioTime dejó de ingerir el 2026-08-02 y estuvo días sin
que nadie lo notara. Conviene alertar si `last_punch` supera unas horas en día
laborable.

**Reforzar un rango de fechas.** Poner a `0` el `log_stamp` del equipo hace que
reenvíe desde el principio de su buffer:

```sql
UPDATE iclock_terminal SET log_stamp = '0' WHERE sn = 'VDE2251700310';
```

Los duplicados los absorbe el índice único `(company_code, emp_code, punch_time)`.

**Purga de auditoría.** `zk_raw_payload` crece sin límite. BioTime dejó
`iclock_terminalcommandlog` con 34,8 millones de filas por no purgar nunca:

```sql
DELETE FROM zk_raw_payload WHERE received_at < now() - interval '90 days';
```

## Datos que conviene tener presentes

- **`punch_state` es `255` en el 96% de los registros**: los equipos no marcan
  entrada ni salida, solo el instante. Horario360 lo resuelve con
  `UnknownRetriever`, que infiere la dirección por cercanía al horario esperado.
- **7,2% de las marcaciones no tienen empleado asociado** en los equipos
  activos: son códigos que no están en `personnel_employee`. El API los expone
  igualmente; filtrarlos por `emp_id` los perdería.
- **`emp_code`** = 1 carácter de tipo de documento + número de documento.
- **Zonas horarias**: la base guarda UTC. Los equipos envían hora local sin
  offset y se convierte con `iclock_terminal.terminal_tz` (hoy `-5`). El API
  devuelve cadena plana ya en hora local, como BioTime — añadir el offset ISO
  rompería el `Carbon::parse` de Horario360.

## Seguridad

El protocolo ADMS no tiene autenticación: quien conozca un SN puede inyectar
marcaciones en un puerto abierto a internet. Y el host tiene IP pública, así que
el API tampoco puede quedar suelto. Barreras activas, en dos capas:

| | Firewall | Aplicación |
|---|---|---|
| Puerto 83 (ingesta) | Solo las 2 IPs de las sedes | `ALLOWED_SNS` + `ALLOWED_SOURCE_IPS` |
| Puerto 8083 (API) | Solo `161.132.68.120` | `API_ALLOWED_IPS` |

La capa de aplicación existe porque el firewall se puede perder en un
reinstalado o una directiva de grupo, y eso dejaría el endpoint de credenciales
abierto a internet. `deploy\configure-firewall.ps1` aplica la primera capa.

Si las sedes cambian de IP pública, los terminales dejan de entregar datos en
silencio. Comprobar con:

```sql
SELECT sn, alias, real_ip FROM iclock_terminal WHERE state = 1;
```

Pendiente de evaluar: TLS. Hoy las credenciales de Horario360 y las marcaciones
viajan en claro. El firmware soporta HTTPS, pero conviene probarlo con un solo
equipo antes de comprometer los cuatro.

## Referencias

`docs/referencia-zkteco/` guarda la especificación del protocolo PUSH y el
manual del API de BioTime 8.5. Es material propietario de ZKTeco bajo
confidencialidad: excluido por `.gitignore`, **no republicar**.

El resto del paquete original —incluido `Pushdemo04042022`, la implementación
Java de referencia— está en `..\_backup-zkteco-sdk\`, fuera del repositorio. Se
consultó para escribir `app/protocol.py`, pero es de la era push **2.x** y el
firmware en producción es **3.0.4S**: ante una discrepancia, manda el PDF de la
especificación, no el código de ejemplo.
