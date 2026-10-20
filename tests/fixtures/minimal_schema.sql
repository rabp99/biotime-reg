-- Esquema mínimo de BioTime para pruebas, sin datos de producción.
--
-- Alternativa ligera a restaurar el dump cuando no está disponible. Columnas,
-- tipos, nulabilidad y restricciones copiados de la base de producción
-- (PostgreSQL 10.7) para las tablas que usan la consulta y la corrección de
-- códigos. No incluye auth_user ni personnel_area: las pruebas que los
-- necesitan se saltan solas.
--
-- Uso:
--   docker run -d --name biotime-pg-min -e POSTGRES_PASSWORD=devlocal -e POSTGRES_DB=biotime -p 55433:5432 postgres:10
--   docker exec -i biotime-pg-min psql -U postgres -d biotime < tests\fixtures\minimal_schema.sql
--   docker exec -i biotime-pg-min psql -U postgres -d biotime < sql\004_emp_code_correction.sql
--   $env:TEST_PG_DSN = 'postgresql://postgres:devlocal@127.0.0.1:55433/biotime'
--   python -m pytest tests/test_emp_code_corrections.py -q

CREATE TABLE IF NOT EXISTS personnel_company (
    id           serial PRIMARY KEY,
    company_code varchar(50) NOT NULL,
    company_name varchar(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS personnel_department (
    id              serial PRIMARY KEY,
    dept_code       varchar(50)  NOT NULL,
    dept_name       varchar(100) NOT NULL,
    is_default      boolean      NOT NULL,
    parent_dept_id  integer,
    dept_manager_id integer,
    company_id      integer      NOT NULL
);

CREATE TABLE IF NOT EXISTS personnel_employee (
    id             serial PRIMARY KEY,
    create_time    timestamptz,
    status         smallint    NOT NULL,
    emp_code       varchar(20) NOT NULL,
    first_name     varchar(25),
    last_name      varchar(25),
    department_id  integer REFERENCES personnel_department (id) DEFERRABLE INITIALLY DEFERRED,
    enable_payroll boolean     NOT NULL,
    is_active      boolean     NOT NULL,
    company_id     integer     NOT NULL REFERENCES personnel_company (id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT personnel_employee_company_id_emp_code_uniq UNIQUE (company_id, emp_code)
);

CREATE TABLE IF NOT EXISTS iclock_terminal (
    id            serial PRIMARY KEY,
    sn            varchar(50) NOT NULL UNIQUE,
    alias         varchar(50) NOT NULL,
    state         integer     NOT NULL DEFAULT 1,
    terminal_tz   smallint    NOT NULL DEFAULT -5,
    last_activity timestamptz,
    area_id       integer
);

CREATE TABLE IF NOT EXISTS iclock_transaction (
    id             serial PRIMARY KEY,
    emp_code       varchar(20) NOT NULL,
    punch_time     timestamptz NOT NULL,
    punch_state    varchar(5)  NOT NULL,
    verify_type    integer     NOT NULL,
    work_code      varchar(20),
    terminal_sn    varchar(50),
    terminal_alias varchar(50),
    area_alias     varchar(30),
    longitude      double precision,
    latitude       double precision,
    gps_location   text,
    mobile         varchar(50),
    source         smallint,
    purpose        smallint,
    crc            varchar(100),
    is_attendance  smallint,
    reserved       varchar(100),
    upload_time    timestamptz,
    sync_status    smallint,
    sync_time      timestamptz,
    is_mask        smallint,
    temperature    numeric,
    emp_id         integer REFERENCES personnel_employee (id) DEFERRABLE INITIALLY DEFERRED,
    terminal_id    integer REFERENCES iclock_terminal (id) DEFERRABLE INITIALLY DEFERRED,
    company_code   varchar(50),
    CONSTRAINT iclock_transaction_company_code_emp_code_punch_time_uniq
        UNIQUE (company_code, emp_code, punch_time)
);

-- La sesión de producción está en Asia/Hong_Kong: reproducirlo hace que un
-- límite de rango calculado sin zona explícita falle en las pruebas.
ALTER DATABASE biotime SET timezone TO 'Asia/Hong_Kong';

INSERT INTO personnel_company (id, company_code, company_name)
SELECT 1, '1', 'PRUEBA' WHERE NOT EXISTS (SELECT 1 FROM personnel_company);
INSERT INTO personnel_department (dept_code, dept_name, is_default, company_id)
SELECT '1', 'PRUEBA', true, 1 WHERE NOT EXISTS (SELECT 1 FROM personnel_department);
INSERT INTO iclock_terminal (sn, alias)
SELECT 'ZZTERMINAL', 'PRUEBA' WHERE NOT EXISTS (SELECT 1 FROM iclock_terminal);
