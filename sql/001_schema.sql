-- Añadidos sobre la base existente de BioTime.
--
-- ALCANCE DE LOS CAMBIOS
--
--   Crea:     1 tabla nueva (zk_raw_payload) y sus 2 índices
--   Modifica: NADA. Ninguna tabla de BioTime cambia de estructura.
--             No se añaden, quitan ni alteran columnas.
--   Borra:    NADA.
--
-- Las marcaciones siguen aterrizando en iclock_transaction y los equipos en
-- iclock_terminal, tal como hace BioTime hoy.
--
-- Idempotente: se puede reejecutar sin efectos.

-- ---------------------------------------------------------------------------
-- Auditoría de payloads crudos
--
-- POR QUÉ EXISTE
--   El protocolo funciona por confirmación: el terminal envía un lote, el
--   servidor responde OK, y el equipo avanza su puntero dando el lote por
--   entregado. No lo reenvía. Si el parser no entendió una línea, esa marcación
--   se habría perdido sin dejar rastro.
--
--   El riesgo es concreto: los equipos activos corren firmware push 3.0.4S
--   (2024), más nuevo que el que documenta el SDK de referencia (2.x, 2022).
--
--   Guardando el cuerpo antes de parsear, el dato original sigue disponible: se
--   corrige el parser y se reprocesa, sin pedirle nada al terminal.
--
-- QUÉ SE GUARDA
--   Solo las entregas de datos. Los sondeos de /iclock/getrequest —lo más
--   frecuente— no se almacenan.
--
--   De ATTPHOTO, BIODATA y demás binarios se guarda la fila con sus metadatos
--   pero NO el contenido: esta aplicación no procesa fotos ni plantillas, y
--   almacenarlas costaría cientos de KB por marcación sin servir para
--   reprocesar nada.
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
-- No repetir ese error. Purga sugerida (mensual):
--     DELETE FROM zk_raw_payload WHERE received_at < now() - interval '90 days';
