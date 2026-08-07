-- Índice OPCIONAL sobre una tabla de BioTime. Es el único cambio que toca su
-- esquema, por eso va en un fichero aparte: se decide y se ejecuta por separado.
--
-- QUÉ HACE
--   Añade un índice a iclock_transaction. No modifica ni una fila de datos, no
--   cambia columnas y no afecta a BioTime si algún día se relevanta.
--
-- POR QUÉ
--   iclock_transaction ya tiene un UNIQUE (company_code, emp_code, punch_time)
--   que cubre bien las consultas de Horario360 que filtran por empleado. Pero
--   ZKTecoDevice.php también tiene dos métodos que consultan SOLO por rango de
--   fechas, sin emp_code:
--       getAttendancesByDate()
--       getAttendancesEmployeeExport()
--   Esos hoy hacen recorrido secuencial sobre 471.810 filas y creciendo.
--
-- CUÁNDO EJECUTARLO
--   No es imprescindible para arrancar. Si esos dos métodos van lentos, o antes
--   de que la tabla crezca mucho más.
--
-- CONCURRENTLY
--   Construye el índice sin bloquear escrituras: los terminales pueden seguir
--   entregando marcaciones mientras se crea. Sin esta opción la tabla queda
--   bloqueada para escritura durante la construcción.
--
--   Requisitos:
--     - NO puede ejecutarse dentro de una transacción (nada de BEGIN/COMMIT).
--       Con `psql -f` funciona directamente.
--     - Si falla a medias deja un índice inválido. Comprobar y limpiar con:
--         SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
--         DROP INDEX iclock_transaction_punch_time_idx;

CREATE INDEX CONCURRENTLY IF NOT EXISTS iclock_transaction_punch_time_idx
    ON iclock_transaction (punch_time DESC);

-- Para revertir:
--     DROP INDEX CONCURRENTLY IF EXISTS iclock_transaction_punch_time_idx;
