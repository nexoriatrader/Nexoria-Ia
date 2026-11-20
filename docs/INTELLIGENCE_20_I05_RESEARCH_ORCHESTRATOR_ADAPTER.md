# I05 Learning Engine ↔ NEXORA Research Orchestrator — read-only adapter

**Development branch:** `feature/intelligence20-i05-research-orchestrator-adapter`  
**Stacked base:** PR #61 (I04 adapter), which preserves PR #60 (Research Orchestrator) and PR #59 (I06 — Research Evidence Engine).  
**Release status:** DRAFT; no merge, deploy, migration, production activation, new model or financial execution.

## Real contracts inspected on PR #56

I05's actual implementation is located on the separate
`feature/intelligence20-i05-learning-research` branch (PR #56),
stacked on I04 / PR #54. It is not present in the PR #61 code lineage.

This adapter consumes **only** existing read methods:

- `learning_registry.learning_enabled()`: I05 + I04 enablement gate;
  `NEXORA_INTELLIGENCE_LEARNING_ENABLED=false` and
  `NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED=false` by default.
- `learning_registry.get_research_release(market, symbol, timeframe_seconds, now)`:
  current I05 research release with `RESEARCH_CHAMPION` or
  `NO_VALIDATED_RESEARCH_MODEL`, generation, release ID/event,
  Champion and Fallback **knowledge IDs**, and execution guards.
- `knowledge_base.get_knowledge(knowledge_id, now, observed_regime,
  include_lineage=True)`: public I04 MODEL identity, state, revision,
  public payload digest, model version, validity and review dates,
  independently validated public evidence windows, decision history.

The I05 registry already owns release integrity, Champion/Fallback
selection, release CAS, validated model gating and rollback. The
I04 Knowledge Base remains the **only** metadata/evidence store,
with `HistoricalResearchReport` remaining the existing artifact store.

**Explicitly NOT invoked:** `research_candidate`,
`evaluate_public_regimes`, `stage_public_candidate`,
`append_independent_public_validation`,
`validate_research_challenger`, `promote_research_champion`,
`monitor_and_rollback` and `run_scheduled_cycle`.
No automatic promotions, new training/inference, rescans, caches,
workers, tables or event queues.

## Wiring and minimal public scope

`orchestrate_research_with_i04_i05(...)` calls the existing I04
orchestrator once (I01 → I02 → I03 → I06 → read-only I04 adapter) and
then enriches the resulting PUBLIC observations with read-only I05
metadata. Synchronous I05/I04 SQLAlchemy reads run using
`asyncio.to_thread`, with SessionLocal ownership entirely inside
those existing module methods.

The adapter derives the **I05 public model scope** from the lowest
already verified I03 timeframe, plus market and symbol (no new
timeframe calculation). The observed public regime is from I03:
UPTREND or DOWNTREND. This scope is not a contract duration,
entry opportunity, or financial signal. Mixed/unverified context is
skipped; excluded assets never query I05.

I05's model `regime=UNKNOWN` describes the model's stored validity
scope; `observed_public_regime` separately records the current I03
context. Both are identified without fabricating a model-specific
prediction.

## Champion / Challenger / Fallback and provenance

Read I05's validated public Champion/Fallback IDs only. Re-read each
selected I04 MODEL record, and refuse influence unless:

- state `ACTIVE`, `reusable_for_research=true`, `reuse_status=CURRENT_VERIFIED_ACTIVE`,
  same market/symbol/timeframe and permitted regime;
- same known I04/I05 versions, public model SHA256, public payload
  SHA256, nonfinancial guards and original I05 training source version;
- validation/expiry windows encompass the current UTC time;
- last I04 decision is `I05_RESEARCH_ONLY_PROMOTION` to ACTIVE at the
  current model revision;
- public `MODEL_ARTIFACT` evidence includes three VERIFIED independent
  groups: train (at least 500 observations), original holdout (at least
  200) and later independent validation (at least 200);
- chronological train→holdout→independent validation without overlap,
  with I05's existing 72-candle embargo between periods.

The returned result carries only public metadata: knowledge ID,
revision, model version/hash, payload digest, validation periods and
counts, validity and next-review timestamps, I05 release ID,
generation and release event. It also exposes the existing I04
`sample_size`, `holdout_samples`, `success_rate` and
`confidence_heuristic` as a versioned `historical_validation`
summary, explicitly named **historical regime-label holdout accuracy**
and its Wilson 95% lower bound. Both rates are validated as finite
and between 0 and 1; neither represents a probability of financial
gain, a certified forecast or a Signal Confidence score. These values
never increase evidence completeness or authorize any research action.
It **does not copy raw artifacts, model weights, I04 payloads,
predictions, personal identifiers or financial data**.

Champion and Fallback remain as selected by I05. The existing
`get_research_release` may already select a verified Fallback when
the former Champion is unavailable. This adapter never invents a
promotion or promotes a Challenger. Challenger is recorded only as
`NOT_USED_UNTIL_SEPARATE_VALIDATION` because I05 exposes no
read-only approved-challenger listing through this contract.

## Degradation and review

A pre-existing I05 release event `AUTO_ROLLBACK` is reported
as observed rollback history. An expired, unapproved, unverifiable
or unavailable model is marked review-required, **not used**.
If the release contains no validated public model, the result is
`I05_NO_VALIDATED_RESEARCH_MODEL`. No fallback or model ID is
fabricated.

Fresh degradation/OOS monitoring is **not performed** here:
I05's `monitor_and_rollback` is a **mutating** method and must
stay under its existing explicit control loop. Thus the adapter can
report recorded rollback and invalidation, but cannot claim that
a new holdout was run or that a fresh degradation measurement exists.

Knowledge/model provenance annotates existing research. It never
increases I06's evidence-completeness score, approves a previously
ineligible asset, changes a research outcome based on model
predictions, publishes to distribution, or sends orders.

## DEMO / REAL / simulation separation

Only `PUBLIC_CLOSED_REGIME_RESEARCH` is consumed.
Every model record has `financial_source_used=false`,
`prediction_used_for_trading=false` and
`automatic_promotion_performed=false`. Tenant/account/mode,
individual transaction, balance, token, stake and P&L fields are
rejected at the adapter boundary, never placed in the shared
public research report. Existing DEMO/REAL/simulation results are
not conflated or treated as independent public holdouts.

## Integration dependency

**I05 and I04 are not yet included in the PR #61 chain.**
At runtime the adapter imports the existing modules only if they
become available. Until then, `NOT_CONNECTED`, `DISABLED`,
`BLOCKED_I04_NOT_CONNECTED` or `I05_UNAVAILABLE` explicitly
indicate the absence; there is no mock fallback in application code.
The test suite injects doubles with I05's **actual method signatures**
and I04's actual read-view format.

A real persisted end-to-end integration requires independently
bringing the I04/I05 code into the chain and having I04's formal
PostgreSQL schema available; neither a merge nor migration is
authorized in this PR. No second Learning Engine or knowledge store
is introduced.

## Tests

`tests/test_intelligence2_i05_research_adapter.py` verifies
actual read signatures, approved Champion/Fallback provenance,
version and regime, OOS periods and embargo, missing release,
invalid/expired models, forged financial data, no automatic
promotion, recorded rollback, disabled/missing I04/I05, asynchronous
one-pass composition and bounded query count.

CI runs those tests plus unchanged I01–I03, I06, I04 adapter,
Research Orchestrator and general regressions. No production
DB access, migration, deploy or Deriv orders.

**PR DRAFT. Preserve #59, #60 and #61; do not merge or activate.**
