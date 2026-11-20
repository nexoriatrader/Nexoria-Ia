-- F01 global + user fencing, formal PostgreSQL migration proposal.
-- REVIEW/APPROVAL REQUIRED. DO NOT execute against production from this PR.
-- No application startup path invokes this script.
-- Precondition: maintenance/zero in-flight REAL START and FINISH operations,
-- validated backups and authorization by database owner.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';

-- Existing user PK and legacy REAL consent tables must already exist.
CREATE TABLE IF NOT EXISTS real_authorization_fences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS real_global_authorization_fences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO real_global_authorization_fences (id, generation, updated_at)
VALUES (1, 0, CURRENT_TIMESTAMP)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS nexora_schema_revisions (
    revision VARCHAR(80) PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Review every existing index/column/PK definition before stamping a schema
-- that might already have one of the tables with an incompatible definition.
INSERT INTO nexora_schema_revisions (revision, applied_at)
VALUES ('f01_global_fencing_v1', CURRENT_TIMESTAMP)
ON CONFLICT (revision) DO NOTHING;
COMMIT;

-- Verification is intentionally separate from this migration script:
-- app.db.verify_postgresql_schema() validates the presence of both fencing
-- tables, the singleton row and the revision during read-only startup.
--
-- ROLLBACK IS NOT AN AUTOMATIC DROP: while deployments/STARTs are in flight
-- removing either fence risks authorization resurrection. Prepare a separate
-- approved rollback in maintenance mode with old/new application versions
-- stopped, archived state and explicit verification. Never run rollback from CI.
