-- Registro de correcciones de códigos mal enrolados.
--
-- ALCANCE DE LOS CAMBIOS
--
--   Crea:     2 tablas nuevas (zk_emp_code_correction, zk_emp_code_correction_item)
--             y sus índices.
--   Modifica: NADA. Ninguna tabla de BioTime cambia de estructura.
--   Borra:    NADA.
--
-- Idempotente: se puede reejecutar sin efectos.
--
-- POR QUÉ EXISTE
--   A veces se enrola a un trabajador en el equipo con un código equivocado
--   (p. ej. 1135 en vez de 112345678). Sus marcaciones quedan bajo ese código y
--   Horario360, que consulta por emp_code, no las encuentra. Desde Horario360 un
--   Administrador reasigna esas marcaciones al código correcto con
--   POST /api/emp-code-corrections/.
--
--   Cada corrección guarda aquí qué filas movió y con qué valores estaban, para
--   poder auditarla y deshacerla (POST /api/emp-code-corrections/{id}/undo).

CREATE TABLE IF NOT EXISTS zk_emp_code_correction (
    id                 bigserial PRIMARY KEY,
    from_emp_code      varchar(20)  NOT NULL,
    to_emp_code        varchar(20)  NOT NULL,
    to_emp_id          integer,                 -- NULL si el código correcto no está en personnel_employee
    range_start        date         NOT NULL,   -- inclusivo, fecha en API_RENDER_TZ
    range_end          date         NOT NULL,   -- inclusivo, fecha en API_RENDER_TZ
    moved              integer      NOT NULL,
    skipped_conflicts  integer      NOT NULL,
    first_punch        timestamptz,
    last_punch         timestamptz,
    requested_by       varchar(150) NOT NULL,
    reference          varchar(150) NOT NULL,
    client_ip          inet,
    created_at         timestamptz  NOT NULL DEFAULT now(),
    undone_at          timestamptz,
    undone_by          varchar(150),
    restored           integer
);

CREATE INDEX IF NOT EXISTS zk_emp_code_correction_from_idx
    ON zk_emp_code_correction (from_emp_code, created_at DESC);
CREATE INDEX IF NOT EXISTS zk_emp_code_correction_to_idx
    ON zk_emp_code_correction (to_emp_code, created_at DESC);

CREATE TABLE IF NOT EXISTS zk_emp_code_correction_item (
    correction_id   bigint      NOT NULL REFERENCES zk_emp_code_correction (id),
    transaction_id  integer     NOT NULL,       -- iclock_transaction.id (sin FK: no se ata a BioTime)
    old_emp_code    varchar(20) NOT NULL,
    old_emp_id      integer,
    old_purpose     smallint,
    punch_time      timestamptz NOT NULL,
    PRIMARY KEY (correction_id, transaction_id)
);
