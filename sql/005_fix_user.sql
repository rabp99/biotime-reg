-- Rol dedicado a la corrección de códigos (PG_FIX_DSN).
--
-- El rol de la aplicación (biotime_reg, 002_app_user.sql) no tiene UPDATE sobre
-- iclock_transaction a propósito: la ingesta y la consulta nunca modifican
-- marcaciones. La corrección sí necesita hacerlo, así que usa una conexión
-- aparte con este rol, que solo puede cambiar las tres columnas que reasigna una
-- corrección (emp_code, emp_id, purpose) y escribir su propio registro.
--
-- Requiere conectarse como superusuario (normalmente 'postgres') y haber
-- ejecutado antes 004_emp_code_correction.sql.
-- CAMBIAR LA CONTRASEÑA antes de ejecutar.

-- ---------------------------------------------------------------------------
-- 1. El rol
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biotime_reg_fix') THEN
        CREATE ROLE biotime_reg_fix LOGIN PASSWORD 'CAMBIAR_ESTA_CLAVE';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE biotime TO biotime_reg_fix;
GRANT USAGE ON SCHEMA public TO biotime_reg_fix;

-- ---------------------------------------------------------------------------
-- 2. Marcaciones: leer y reasignar
--
-- UPDATE solo por columnas. El SELECT ... FOR UPDATE de la corrección se
-- conforma con UPDATE en alguna columna. Sin INSERT ni DELETE.
-- ---------------------------------------------------------------------------
GRANT SELECT ON iclock_transaction TO biotime_reg_fix;
GRANT UPDATE (emp_code, emp_id, purpose) ON iclock_transaction TO biotime_reg_fix;

-- ---------------------------------------------------------------------------
-- 3. Solo lectura: resolver empleado, departamento y equipo para el preview
-- ---------------------------------------------------------------------------
GRANT SELECT ON personnel_employee TO biotime_reg_fix;
GRANT SELECT ON personnel_department TO biotime_reg_fix;
GRANT SELECT ON iclock_terminal TO biotime_reg_fix;

-- ---------------------------------------------------------------------------
-- 4. Registro de correcciones
-- ---------------------------------------------------------------------------
-- El registro no se reescribe: solo se marca como deshecho.
GRANT SELECT, INSERT ON zk_emp_code_correction TO biotime_reg_fix;
GRANT UPDATE (undone_at, undone_by, restored) ON zk_emp_code_correction TO biotime_reg_fix;
GRANT USAGE, SELECT ON SEQUENCE zk_emp_code_correction_id_seq TO biotime_reg_fix;
GRANT SELECT, INSERT ON zk_emp_code_correction_item TO biotime_reg_fix;

-- ---------------------------------------------------------------------------
-- Comprobación
-- ---------------------------------------------------------------------------
SELECT table_name, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS permisos
FROM information_schema.role_table_grants
WHERE grantee = 'biotime_reg_fix'
GROUP BY table_name
ORDER BY table_name;

SELECT table_name, column_name, privilege_type
FROM information_schema.column_privileges
WHERE grantee = 'biotime_reg_fix' AND privilege_type = 'UPDATE'
ORDER BY table_name, column_name;
