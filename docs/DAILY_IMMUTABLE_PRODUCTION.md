# Deterministic daily production

Owner-approved activation: 14 October 2026. The existing `daily-digest` command
now uses Immutable FACT BUNDLE preparation, with zero model calls. Historical
shadow notes and legacy rewrite helpers do not select the live route.

## One finite route

Existing 20:56 Europe/Moscow task -> finite launcher -> daily-digest --confirm
-> bounded collection -> source normalization/relevance filtering -> immutable
facts -> semantic event dedupe -> selection -> deterministic Russian text
-> provenance/essential-fact/metric/category gate -> existing Telegram sender.

No new publication task, manual approval, resident polling or resident model.
Former auxiliary minute-collection and preparation tasks are disabled; their
definitions are retained privately for diagnostics. Existing source definitions,
destination, credentials, daily ledger and secondary service settings are kept.
The global editor/model configuration is not changed for other functionality.

## Delivery and recovery

The existing per-day ledger and process lock remain authoritative. Frozen
members are exact represented originals, not approximate matching stories.
Fact manifests are staged under STARTED, and become published-history evidence
only with a matching PUBLISHED receipt/body hash. A crash after that receipt can
restore included-source markers without a network send. A changed frozen edition
cannot silently replace an earlier prepared one.

SENDING is persisted before POST. Timeouts, 5xx, invalid responses or crashes
without a confirmed receipt remain uncertain and are never blindly retried.
Only explicit Telegram rejection permits non-delivery handling. Flood-control
retry_after allows one repeat with a maximum five-second wait, within the finite
delivery budget. Authentication/destination errors are not retried automatically.
The Bot API documents retry_after as the delay before repeating a flood-rejected
request: https://core.telegram.org/bots/api#responseparameters.

Unavailable sources have bounded requests; Telegram source GETs permit one
bounded retry and RSS keeps bounded mirror fallbacks. Healthy independent
stories survive individual relation failures. With no safe edition, delivery is
skipped, not replaced by legacy Qwen output. An outage is not terminal NO_NEWS.

Last confirmed source cutoff retains late unprinted news, capped at two days.
Publication date remains the current Moscow day. Legacy history is accepted only
from receipt-matching artifacts and actually printed facts. New history uses
receipt-bound immutable manifests. No automatic ledger reset or rollback.

## Finite health check

Per-run private health artifact and daily SQLite health record include date,
start/end, source/news/rejection counts and reasons, essential-fact checks,
published event count, UTF-16 size, phase durations, message ID, Telegram state,
LLM count (zero) and final SUCCESS/PARTIAL_SUCCESS/FAILED/SKIPPED_UNSAFE status.
Latest live health is written locally. The finite launcher records interruption
and preserves a known PUBLISHED receipt, without sending or starting a model.

No administrative integration is invented and technical diagnostics are not
posted in the football channel. Runtime reports, source snapshots, SQLite,
credentials and restoration copies are private and excluded from public export.

## Safety rollback

Previous entry/configuration snapshots are retained privately. Never restore
free-form Qwen automatically. A blocked edition is skipped with diagnostics;
the next ordinary scheduled batch uses the same immutable route. Any reviewed
manual code recovery preserves existing day receipts, sources and suppressions.
PUBLISHED/SENDING cannot be resent by restoring old launch code.

## Acceptance boundary

Real network dry-run exercised the actual production orchestrator with an
isolated copy of daily SQLite and blocked sender/model calls. No test post was
sent. The first real immutable edition is scheduled, not claimed published in
advance. Optional X RSS completeness remains best-effort. Source fidelity does
not constitute independent real-world verification of claims in trusted sources.
