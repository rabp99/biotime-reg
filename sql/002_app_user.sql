-- Usuario dedicado para la aplicación, con permisos mínimos.
--
-- Alternativa a reutilizar las credenciales de BioTime, que son de superusuario
-- y dan acceso a las 192 tablas. Esta aplicación solo necesita 6.
--
-- Requiere conectarse como superusuario (normalmente 'postgres').
-- CAMBIAR LA CONTRASEÑA antes de ejecutar.

-- ---------------------------------------------------------------------------
-- 1. El rol
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biotime_reg') THEN
        CREATE ROLE biotime_reg LOGIN PASSWORD 'CAMBIAR_ESTA_CLAVE';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE biotime TO biotime_reg;
GRANT USAGE ON SCHEMA public TO biotime_reg;

-- ---------------------------------------------------------------------------
-- 2. Lectura y escritura de marcaciones
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT ON iclock_transaction TO biotime_reg;
GRANT USAGE, SELECT ON SEQUENCE iclock_transaction_id_seq TO biotime_reg;

-- Sin DELETE ni UPDATE a propósito: la aplicación nunca modifica ni borra
-- marcaciones. Si un error intentara hacerlo, PostgreSQL lo impediría.

-- ---------------------------------------------------------------------------
-- 3. Equipos
--
-- Necesita UPDATE para los stamps de sincronización y la última actividad, e
-- INSERT por si hay que dar de alta un equipo repuesto.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON iclock_terminal TO biotime_reg;
GRANT USAGE, SELECT ON SEQUENCE iclock_terminal_id_seq TO biotime_reg;

-- ---------------------------------------------------------------------------
-- 4. Solo lectura: resolver empleado, área y autenticar el API
-- ---------------------------------------------------------------------------
GRANT SELECT ON personnel_employee TO biotime_reg;
GRANT SELECT ON personnel_area TO biotime_reg;
GRANT SELECT ON auth_user TO biotime_reg;

-- ---------------------------------------------------------------------------
-- 5. Tablas propias
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON zk_raw_payload TO biotime_reg;
GRANT USAGE, SELECT ON SEQUENCE zk_raw_payload_id_seq TO biotime_reg;

-- ---------------------------------------------------------------------------
-- Comprobación
-- ---------------------------------------------------------------------------
SELECT table_name, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS permisos
FROM information_schema.role_table_grants
WHERE grantee = 'biotime_reg'
GROUP BY table_name
ORDER BY table_name;
