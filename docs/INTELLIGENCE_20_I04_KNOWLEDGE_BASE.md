# NEXORA INTELLIGENCE 2.0 — I04 Intelligence Knowledge Base

**Implementation branch:** feature/intelligence20-i04-knowledge-base
**Immutable parent:** I03 / PR #53, SHA \`1f1a97e61d06e9f2024fcd63722103e30f1fe5cb\`.
**Scope:** durable shared public knowledge ONLY, not Learning Engine (I05).
**Safety:** REAL still disabled, no orders/Deriv buys, F01/P0 unchanged,
no merge/deploy/production migration.

## Architectural storage decision

Use the **same** \`db.engine\`, \`db.Base\` and \`db.SessionLocal\`
as historical research and P0/F01. No second DB, Redis, WebSocket,
knowledge service, user feature store or separate cache. Three necessary
**additive** tables are mapped in \`app/db.py\`:

| Persistent table | Content | Integrity |
| --- | --- | --- |
| \`intelligence_knowledge\` | One canonical public identity (type, market, symbol, TF, subject, model version); state, revision, public payload, SHA256, validity and review timestamps | Unique identity SHA256, revision check, validity check, searchable indexes |
| \`intelligence_knowledge_evidence\` | Append-only *minimized* public evidence summaries, source digests, independent-origin grouping, sample counts, aggregation group size, observed period and verification marker | FK to knowledge; unique source digest/knowledge, sample and period checks |
| \`intelligence_knowledge_decisions\` | One immutable decision event per revision (discovery, evidence, transition, renewal); reasons, before/after state, evidence-set SHA256 and timestamp | FK + unique (knowledge_id, revision) + CAS consistency verification |

None of the three has a \`user_id\`, \`account_id\`, stake, balance,
token, individual payout, individual order or broker-contract column.
DEMO and REAL read the exact same public record. All public payload keys
are allowlisted; unknown keys, nested user data, free-form text and
nonfinite values are refused. Execution evidence requires at least 30
observations from groups containing 30 or more records. Source identities
are SHA256 digests, not customer names or raw account identifiers.

### Reusing existing historical evidence

\`research_persistence.HistoricalResearchReport\` remains the original
research snapshot owner. \`append_research_report_evidence\` checks the
existing report in that table, requires at least 100 historical holdout
observations and records only source hashes, observation counts, period
and minimized public metadata. Retention can remove old reports without
removing I04's minimal verified lineage snapshot. This is lineage
provenance, not a substitute for an external strategy profitability audit.

## Knowledge lifecycle and guarantees

\`\`\`text
DISCOVERED -> UNDER_VALIDATION -> VALIDATED -> ACTIVE
                                             |
                                             v
                                        REVIEW_REQUIRED
                                             |
                                             v
                                         INVALIDATED
                                             |
                                             v
                                         REVALIDATED -> ACTIVE
\`\`\`

Additional explicit rejection to \`INVALIDATED\` is allowed for
DISCOVERED, UNDER_VALIDATION, VALIDATED and REVALIDATED. An active record
may be invalidated immediately when the trusted public market regime
changes. All transitions require the caller's expected revision, an
audit-safe reason code and the old state; every transition and evidence
addition increments \`revision\` atomically and appends a decision in
the SAME transaction. PostgreSQL row locks and CAS protect concurrent
workers; stale writers fail.

- **No automatic activation:** discovery is always DISCOVERED; a human/
  trusted internal review process must call explicit state transitions.
- **Validation gate:** two distinct *verified* independent origin groups
  with >=72 observations each; strategy additionally requires one
  existing research holdout with >=100 samples. Execution type requires
  two independent sufficiently aggregated execution sources.
- **MODEL protection:** I04 may store discovered model metadata but
  cannot transition MODEL into VALIDATED, ACTIVE or REVALIDATED; no
  training, inference, promotion, retraining or rollback of models.
- **Review:** ACTIVE records beyond \`next_review_at\` or
  \`valid_until\` are rejected immediately on retrieval. When I04 is
  enabled, the EXISTING research maintenance loop calls
  \`review_due_knowledge\` to persist \`REVIEW_REQUIRED\`; no second
  scheduler or timer.
- **Market change:** retrieval requires the caller's current public
  regime for any regime-specific record. Mismatch/absent current regime
  always excludes the record; the explicit
  \`invalidate_on_market_change\` API persists invalidation upon a
  trusted new public observation. It is NOT automatically wired to an
  account-scoped scanner or financial execution system.
- **Recovery:** revision, evidence-set digest, public payload hash and
  contiguous decision chain are checked before every retrieval.
  \`INVALIDATED\` and \`REVIEW_REQUIRED\` cannot be reused.
  \`revise_validity\` renews time windows only on inactive states;
  revalidation requires *fresh*, independent evidence recorded after
  review/invalidation, before explicit reactivation.
- **Integrity limitations:** SHA256 proves local snapshot consistency,
  not independent authentication or historical truth of the source;
  the internal \`verified\` mark must come from a trusted producer.
  No external party has been represented as having signed off.

## Public retrieval; NO public mutation endpoint

The existing FastAPI service adds two authenticated, **read-only** routes:

- \`GET /api/research/knowledge?category=MARKET&market=forex&symbol=frxEURUSD&observed_regime=UPTREND\`
  returns \`active\` and explicit \`excluded\` reasons; never auto-revives
  stale entries. Enforced public/mode-independent view.
- \`GET /api/research/knowledge/{knowledge_id}?observed_regime=UPTREND\`
  returns version, validity, sanitized evidence, reasoned decision history.
  This detail view is not a reusable signal; follow \`reusable_for_research\`
  before research consumption.

No public WRITE routes. No financial execution integration.

### Formal schema migration and startup

\`migrations/I04_KNOWLEDGE_BASE_V1_POSTGRES.sql\` is **prepared, NOT run
in production**. It requires the formal F01 revision, creates three
additive tables in one transaction, locks/validates structure and stamps
\`i04_knowledge_base_v1\` last. Replays are idempotent; incompatible
partial schema aborts/rolls back. PostgreSQL startup performs **only
read-only checks**. The opt-in flag
\`NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED=false\` is OFF by default; old
F01/P0 installations remain runnable and no implicit I04 migration
occurs. If enabled on an unmigrated/incompatible DB, startup fails
closed with zero DDL. Public read routes return 503 while disabled.

Formal production migration, enabling the feature and deployment
require separate explicit authorization, outside this PR. Only
PostgreSQL 16 disposable test schemas may run the migration in CI.

## Components mapped onto one implementation

- **Market Knowledge / Strategy Knowledge / Signal Knowledge /
  Model Knowledge / Execution Knowledge:** five typed categories in
  \`IntelligenceKnowledge\`, not five competing databases.
- **Evidence & Lineage:** \`IntelligenceKnowledgeEvidence\`, source
  digests, minimal public summary and linked research reports.
- **Intelligence Knowledge Base:** \`knowledge_base.discover\`,
  \`append_evidence\`, \`change_state\`, \`revise_validity\`.
- **Knowledge Retrieval Engine:** \`get_knowledge\` and
  \`retrieve_active\`, with required current regime checks.
- **Intelligence Decision Log:** contiguous immutable
  \`IntelligenceKnowledgeDecision\` events for every revision.
- **Existing infrastructure:** one SQLAlchemy connection pool,
  PostgreSQL/SQLite test setup, research reports, existing hourly
  research maintenance. I01–I03 public descriptions can become future
  certified evidence inputs only through explicit trusted adapters.

## Acceptance evidence

Unit tests: \`tests/test_intelligence2_knowledge_base.py\` exercise
discovery and dedup, unsupported private fields, evidence sufficiency,
state guards, CAS, expiration and review, revalidation with fresh
evidence, market-change invalidation, corrupted payload/lineage refusal,
research report source reuse, aggregate privacy, MODEL nonpromotion,
authenticated sharing between DEMO and REAL and zero buy calls.

Disposable PostgreSQL tests:
\`tests/test_postgres_i04_knowledge.py\` exercise F01 prerequisite,
formal migration and replay, incompatible partial schema rollback,
zero automatic DDL on boot, missing-table failure and concurrent
two-worker CAS (one wins; no revision gaps).

Existing I01, I02, I03, P0-01..P0-05 and F01 CI regressions remain
required. A test in a disposable DB is not a production migration or
financial deployment authorization.

**Next stage I05 = Learning Engine (NOT STARTED).** I05 may propose
controlled learning/validation workflows only after consuming this
versioned Knowledge Base. No training or model promotion was added.
