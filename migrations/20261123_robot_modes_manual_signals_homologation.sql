-- NEXORA 2.0: modo de configuração e agenda de sinais (somente planejamento).
-- NÃO executar em produção. Aplicar formalmente apenas em PostgreSQL de homologação
-- depois de backup, revisão e janela aprovada. A aplicação não gera DDL em PostgreSQL.
BEGIN;
CREATE TABLE IF NOT EXISTS robot_usage_modes (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    mode VARCHAR(16) NOT NULL DEFAULT 'intelligent',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT robot_usage_modes_valid_mode CHECK (mode IN ('intelligent','manual'))
);
CREATE TABLE IF NOT EXISTS robot_manual_signals (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol VARCHAR(30) NOT NULL,
    direction VARCHAR(4) NOT NULL CHECK (direction IN ('CALL','PUT')),
    expiry_minutes INTEGER NOT NULL CHECK (expiry_minutes IN (1,3,5,15)),
    scheduled_for TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_robot_manual_signals_user_id
    ON robot_manual_signals (user_id);
CREATE INDEX IF NOT EXISTS ix_robot_manual_signals_user_schedule
    ON robot_manual_signals (user_id, scheduled_for);
COMMIT;

-- Verificação após aplicação:
-- SELECT tablename FROM pg_tables WHERE schemaname = 'public'
-- AND tablename IN ('robot_usage_modes','robot_manual_signals');
-- SELECT COUNT(*) FROM robot_usage_modes;
-- SELECT COUNT(*) FROM robot_manual_signals;
