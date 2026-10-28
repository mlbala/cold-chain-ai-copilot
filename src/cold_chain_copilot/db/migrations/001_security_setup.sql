-- 001_security_setup.sql
-- Idempotent RBAC setup for the Cold Chain Copilot. Run as the Aiven admin (avnadmin).
-- `fde-copilot setup-rbac` creates (or re-keys) the LOGIN role usr_fde_ro first, then runs
-- this file in a single transaction. Re-running is safe.

-- 1. Raw legacy table (system of record; the agent can never read it directly) -------------
CREATE TABLE IF NOT EXISTS public.legacy_fleet_shipments (
    shipment_id         VARCHAR(32)    PRIMARY KEY,
    truck_id            VARCHAR(32)    NOT NULL,
    driver_name         VARCHAR(128)   NOT NULL,
    origin_hub          VARCHAR(64)    NOT NULL,
    destination_hub     VARCHAR(64)    NOT NULL,
    cargo_type          VARCHAR(64)    NOT NULL,
    current_lat         NUMERIC(9, 6)  NOT NULL CHECK (current_lat BETWEEN -90 AND 90),
    current_lon         NUMERIC(9, 6)  NOT NULL CHECK (current_lon BETWEEN -180 AND 180),
    reefer_temp_c       NUMERIC(5, 2)  NOT NULL,
    target_min_temp_c   NUMERIC(5, 2)  NOT NULL,
    target_max_temp_c   NUMERIC(5, 2)  NOT NULL,
    door_status         VARCHAR(16)    NOT NULL CHECK (door_status IN ('CLOSED', 'OPEN')),
    battery_pct         SMALLINT       NOT NULL CHECK (battery_pct BETWEEN 0 AND 100),
    eta_hours           NUMERIC(6, 2)  NOT NULL CHECK (eta_hours >= 0),
    shipment_status     VARCHAR(32)    NOT NULL,
    internal_margin_usd NUMERIC(12, 2) NOT NULL,
    last_updated_utc    TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT legacy_fleet_shipments_temp_band CHECK (target_min_temp_c < target_max_temp_c)
);

CREATE INDEX IF NOT EXISTS idx_legacy_fleet_shipments_status
    ON public.legacy_fleet_shipments (shipment_status);

-- 2. Restricted AI schema and view (internal_margin_usd is never projected) ---------------
CREATE SCHEMA IF NOT EXISTS fde_views;

CREATE OR REPLACE VIEW fde_views.vw_active_fleet
WITH (security_barrier = true) AS
SELECT
    shipment_id,
    truck_id,
    driver_name,
    origin_hub,
    destination_hub,
    cargo_type,
    current_lat,
    current_lon,
    reefer_temp_c,
    target_min_temp_c,
    target_max_temp_c,
    door_status,
    battery_pct,
    eta_hours,
    shipment_status,
    last_updated_utc
FROM public.legacy_fleet_shipments
WHERE shipment_status <> 'DELIVERED';

COMMENT ON VIEW fde_views.vw_active_fleet IS
    'Operational, margin-free projection of non-delivered shipments for the AI agent.';

-- 3. Immutable audit trail -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fde_views.agent_audit_log (
    log_id        SERIAL       PRIMARY KEY,
    session_id    VARCHAR(64)  NOT NULL,
    timestamp_utc TIMESTAMPTZ  NOT NULL DEFAULT now(),
    node_name     VARCHAR(64)  NOT NULL,
    model_used    VARCHAR(64),
    tool_called   VARCHAR(64),
    executed_sql  TEXT,
    status        VARCHAR(32)  NOT NULL,
    latency_ms    INTEGER      CHECK (latency_ms IS NULL OR latency_ms >= 0),
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_audit_log_timestamp
    ON fde_views.agent_audit_log (timestamp_utc DESC);
CREATE INDEX IF NOT EXISTS idx_agent_audit_log_session
    ON fde_views.agent_audit_log (session_id);

-- Append-only even for the table owner: UPDATE, DELETE and TRUNCATE always fail.
CREATE OR REPLACE FUNCTION fde_views.reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'fde_views.agent_audit_log is append-only: % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

REVOKE ALL ON FUNCTION fde_views.reject_audit_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_agent_audit_log_no_update_delete ON fde_views.agent_audit_log;
CREATE TRIGGER trg_agent_audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON fde_views.agent_audit_log
    FOR EACH ROW EXECUTE FUNCTION fde_views.reject_audit_mutation();

DROP TRIGGER IF EXISTS trg_agent_audit_log_no_truncate ON fde_views.agent_audit_log;
CREATE TRIGGER trg_agent_audit_log_no_truncate
    BEFORE TRUNCATE ON fde_views.agent_audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION fde_views.reject_audit_mutation();

-- 4. Least-privilege grants for usr_fde_ro -------------------------------------------------
-- Start from zero on both schemas, then grant exactly two privileges (plus the sequence
-- usage that INSERT on a SERIAL column requires).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM usr_fde_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM usr_fde_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM usr_fde_ro;

REVOKE ALL ON SCHEMA fde_views FROM PUBLIC;
REVOKE ALL ON SCHEMA fde_views FROM usr_fde_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA fde_views FROM usr_fde_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA fde_views FROM usr_fde_ro;

GRANT USAGE ON SCHEMA fde_views TO usr_fde_ro;
GRANT SELECT ON fde_views.vw_active_fleet TO usr_fde_ro;
GRANT INSERT ON fde_views.agent_audit_log TO usr_fde_ro;
GRANT USAGE ON SEQUENCE fde_views.agent_audit_log_log_id_seq TO usr_fde_ro;

-- Session defaults for the agent role: bounded runtime, view-first name resolution.
ALTER ROLE usr_fde_ro SET statement_timeout = '5s';
ALTER ROLE usr_fde_ro SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE usr_fde_ro SET search_path = fde_views;
