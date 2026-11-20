-- F01 global + user fencing. Formal migration; PostgreSQL 16.
-- REVIEW/APPROVAL REQUIRED. NEVER execute on production from this PR.
-- Application startup is read-only; maintenance and authorization are prerequisites.
-- Run the whole file in one session. Any structural mismatch aborts this
-- transaction: neither newly created objects nor the revision are committed.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';

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

CREATE TABLE IF NOT EXISTS nexora_schema_revisions (
    revision VARCHAR(80) PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Serialize validation with concurrent DDL/DML; no incompatible object may
-- change between inspection and stamping. This script is for maintenance only.
LOCK TABLE real_global_authorization_fences,
           real_authorization_fences,
           nexora_schema_revisions IN ACCESS EXCLUSIVE MODE;

DO $f01_validate$
DECLARE
    spec RECORD;
    col JSONB;
    table_oid OID;
    table_kind "char";
    users_oid OID;
    actual_count INTEGER;
    actual_type TEXT;
    actual_not_null BOOLEAN;
    actual_default TEXT;
    actual_pk TEXT[];
    actual_checks TEXT[];
BEGIN
    SELECT c.oid INTO users_oid
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = 'users'
       AND c.relkind = 'r';
    IF users_oid IS NULL OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute a
         WHERE a.attrelid = users_oid AND a.attname = 'id'
           AND a.atttypid = 'pg_catalog.int4'::regtype
           AND a.attnotnull AND NOT a.attisdropped
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint p
          JOIN pg_catalog.pg_attribute a ON a.attrelid = p.conrelid
         WHERE p.conrelid = users_oid AND p.contype = 'p'
           AND p.conkey = ARRAY[a.attnum]::smallint[] AND a.attname = 'id'
           AND NOT p.condeferrable
    ) THEN
        RAISE EXCEPTION 'F01 incompatible prerequisite: %.users(id INTEGER PRIMARY KEY)', current_schema();
    END IF;

    FOR spec IN
        SELECT * FROM (VALUES
          ('real_authorization_fences',
           '[["user_id","integer",true,null],["generation","integer",true,"0"],["updated_at","timestamp with time zone",true,"CURRENT_TIMESTAMP"]]'::jsonb,
           ARRAY['user_id']::text[], ARRAY['generation>=0']::text[], 3),
          ('real_global_authorization_fences',
           '[["id","integer",true,null],["generation","integer",true,"0"],["updated_at","timestamp with time zone",true,"CURRENT_TIMESTAMP"]]'::jsonb,
           ARRAY['id']::text[], ARRAY['generation>=0','id=1']::text[], 3),
          ('nexora_schema_revisions',
           '[["revision","character varying(80)",true,null],["applied_at","timestamp with time zone",true,"CURRENT_TIMESTAMP"]]'::jsonb,
           ARRAY['revision']::text[], ARRAY[]::text[], 1)
        ) AS expected(name, columns_json, pk_columns, checks, constraints_count)
    LOOP
        SELECT c.oid, c.relkind INTO table_oid, table_kind
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = current_schema() AND c.relname = spec.name;
        IF table_oid IS NULL OR table_kind <> 'r' THEN
            RAISE EXCEPTION 'F01 incompatible table: %', spec.name;
        END IF;

        SELECT count(*) INTO actual_count FROM pg_catalog.pg_attribute a
         WHERE a.attrelid = table_oid AND a.attnum > 0 AND NOT a.attisdropped;
        IF actual_count <> jsonb_array_length(spec.columns_json) THEN
            RAISE EXCEPTION 'F01 incompatible columns: % (expected %, found %)',
                spec.name, jsonb_array_length(spec.columns_json), actual_count;
        END IF;

        FOR col IN SELECT value FROM jsonb_array_elements(spec.columns_json) AS j(value)
        LOOP
            SELECT pg_catalog.format_type(a.atttypid, a.atttypmod),
                   a.attnotnull, pg_catalog.pg_get_expr(d.adbin, d.adrelid)
              INTO actual_type, actual_not_null, actual_default
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d
                ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = table_oid AND a.attname = col->>0
               AND a.attnum > 0 AND NOT a.attisdropped;
            IF NOT FOUND OR actual_type IS DISTINCT FROM col->>1
               OR actual_not_null IS DISTINCT FROM (col->>2)::boolean
               OR actual_default IS DISTINCT FROM col->>3 THEN
                RAISE EXCEPTION 'F01 incompatible column %.%: type/nullability/default',
                    spec.name, col->>0;
            END IF;
        END LOOP;

        SELECT count(*) INTO actual_count FROM pg_catalog.pg_constraint p
         WHERE p.conrelid = table_oid;
        IF actual_count <> spec.constraints_count OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_constraint p
             WHERE p.conrelid = table_oid AND
               (NOT p.convalidated OR p.condeferrable OR p.condeferred
                OR NOT p.conislocal OR (p.contype = 'c' AND p.connoinherit))
        ) THEN
            RAISE EXCEPTION 'F01 incompatible constraints: %', spec.name;
        END IF;

        SELECT array_agg(a.attname ORDER BY u.ordinality) INTO actual_pk
          FROM pg_catalog.pg_constraint p
          JOIN LATERAL unnest(p.conkey) WITH ORDINALITY AS u(attnum, ordinality)
            ON TRUE
          JOIN pg_catalog.pg_attribute a
            ON a.attrelid = p.conrelid AND a.attnum = u.attnum
         WHERE p.conrelid = table_oid AND p.contype = 'p';
        IF actual_pk IS DISTINCT FROM spec.pk_columns OR NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_constraint p
              JOIN pg_catalog.pg_index i ON i.indexrelid = p.conindid
             WHERE p.conrelid = table_oid AND p.contype = 'p'
               AND i.indisprimary AND i.indisunique AND i.indisvalid AND i.indisready
        ) THEN
            RAISE EXCEPTION 'F01 incompatible primary key: %', spec.name;
        END IF;

        SELECT coalesce(array_agg(
            regexp_replace(lower(pg_catalog.pg_get_expr(p.conbin, p.conrelid)),
                           '[[:space:]()]', '', 'g')
            ORDER BY pg_catalog.pg_get_expr(p.conbin, p.conrelid)),
            ARRAY[]::text[])
          INTO actual_checks
          FROM pg_catalog.pg_constraint p
         WHERE p.conrelid = table_oid AND p.contype = 'c';
        -- Compare sorted values; constraint names are intentionally irrelevant.
        IF (SELECT array_agg(x ORDER BY x) FROM unnest(actual_checks) AS x)
           IS DISTINCT FROM
           (SELECT array_agg(x ORDER BY x) FROM unnest(spec.checks) AS x) THEN
            RAISE EXCEPTION 'F01 incompatible CHECK constraints: % (found %)',
                spec.name, actual_checks;
        END IF;

        IF spec.name = 'real_authorization_fences' AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_constraint f
             WHERE f.conrelid = table_oid AND f.contype = 'f'
               AND f.confrelid = users_oid
               AND f.conkey = ARRAY[(SELECT a.attnum
                                     FROM pg_catalog.pg_attribute a
                                    WHERE a.attrelid = table_oid AND a.attname = 'user_id')]::smallint[]
               AND f.confkey = ARRAY[(SELECT a.attnum
                                      FROM pg_catalog.pg_attribute a
                                     WHERE a.attrelid = users_oid AND a.attname = 'id')]::smallint[]
               AND f.confupdtype = 'a' AND f.confdeltype = 'a'
               AND f.confmatchtype = 's'
        ) THEN
            RAISE EXCEPTION 'F01 incompatible foreign key: %.user_id -> users.id', spec.name;
        END IF;
    END LOOP;
END
$f01_validate$;

-- Only after every table/column/PK/FK/CHECK/default passes inspection.
INSERT INTO real_global_authorization_fences (id, generation, updated_at)
VALUES (1, 0, CURRENT_TIMESTAMP)
ON CONFLICT (id) DO NOTHING;

DO $f01_singleton$
BEGIN
    IF (SELECT count(*) FROM real_global_authorization_fences) <> 1
       OR NOT EXISTS (SELECT 1 FROM real_global_authorization_fences
                       WHERE id = 1 AND generation >= 0) THEN
        RAISE EXCEPTION 'F01 incompatible global singleton: expected id=1 with nonnegative generation';
    END IF;
END
$f01_singleton$;

-- Stamp last, inside the SAME transaction. Any exception rolls back
-- everything including this revision (psql must stop on error).
INSERT INTO nexora_schema_revisions (revision, applied_at)
VALUES ('f01_global_fencing_v1', CURRENT_TIMESTAMP)
ON CONFLICT (revision) DO NOTHING;
COMMIT;

-- This is not an authorized production runbook. Destructive rollback of
-- distributed fencing requires separately approved maintenance procedures.
