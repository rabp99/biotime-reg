-- Añadidos sobre la base existente de BioTime.
--
-- Las marcaciones siguen aterrizando en public.iclock_transaction y los
-- equipos en public.iclock_terminal: no se duplica el modelo. Aquí solo va
-- lo que BioTime no cubre y este servidor necesita.
--
-- Idempotente: se puede reejecutar sin efectos.

-- ---------------------------------------------------------------------------
-- 1. Auditoría de payloads crudos
--
-- Se escribe SIEMPRE, antes de intentar parsear. El firmware de los equipos
-- activos es push 3.0.4S, más nuevo que el que documenta el SDK de referencia
-- (2.x): habrá diferencias de trama que solo se descubren en producción. Si el
-- parser falla, el dato original sigue aquí y se puede reprocesar sin pedírselo
-- otra vez al terminal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zk_raw_payload (
    id            bigserial PRIMARY KEY,
    device_sn     text,
    source_ip     inet,           -- IP pública de la sede (para lista blanca)
    method        text NOT NULL,
    path          text NOT NULL,
    query         text,
    table_name    text,           -- parámetro ?table= (ATTLOG, OPERLOG, options...)
    body          bytea,
    body_bytes    integer NOT NULL DEFAULT 0,
    parsed_rows   integer,
    inserted_rows integer,
    skipped_lines integer,
    received_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS zk_raw_payload_received_idx
    ON zk_raw_payload (received_at DESC);
CREATE INDEX IF NOT EXISTS zk_raw_payload_device_idx
    ON zk_raw_payload (device_sn, received_at DESC);

-- Retención: los payloads crudos crecen sin límite. BioTime dejó
-- iclock_terminalcommandlog con 34,8 millones de filas por no purgar nunca.
-- No repetir ese error. Purga sugerida (cron diario):
--     DELETE FROM zk_raw_payload WHERE received_at < now() - interval '90 days';


-- ---------------------------------------------------------------------------
-- 2. Índice para el API de consulta
--
-- iclock_transaction solo tiene índices por id (PK), emp_id, terminal_id y el
-- UNIQUE (company_code, emp_code, punch_time). Un filtro por rango de fechas
-- —el caso de uso de reportes de Horario360— hoy haría seq scan sobre 471k
-- filas y creciendo.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS iclock_transaction_punch_time_idx
    ON iclock_transaction (punch_time DESC);


-- ---------------------------------------------------------------------------
-- 3. Credenciales del API que consume Horario360
--
-- Se guarda solo el hash de la clave: si alguien lee esta tabla no obtiene
-- acceso. La clave en claro se entrega una sola vez al crearla.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zk_api_client (
    id          serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    key_hash    text NOT NULL,      -- sha256 hex de la API key
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_used   timestamptz
);


-- ---------------------------------------------------------------------------
-- 4. Vista de consulta para Horario360
--
-- Expone emp_code siempre, esté o no vinculado a un empleado: el 7,2% de las
-- marcaciones de los equipos activos no tiene emp_id, y filtrarlas las perdería.
-- punch_time se devuelve como timestamptz; el cliente decide la zona.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW zk_v_punches AS
SELECT t.id,
       t.emp_code,
       t.punch_time,
       t.terminal_sn,
       t.terminal_alias,
       t.punch_state,          -- ojo: 255 = sin definir en el 96% de los casos
       t.verify_type,
       t.upload_time,
       e.id            AS employee_id,
       e.first_name,
       e.last_name
FROM iclock_transaction t
LEFT JOIN personnel_employee e ON e.id = t.emp_id;
