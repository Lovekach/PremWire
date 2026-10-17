# Immutable daily rollout preparation — 14 October 2026

## NOT_READY — shadow only, no live switch

Existing production preparation, collection tasks, platform settings and the
20:56 Moscow trigger were not changed. No live SQLite write, Telegram send,
published-message edit, Git push or export-policy refresh was performed.

## Changes prepared

- `daily_fact_digest.py`: mixed touches-caption/goal-row tables are omitted,
  not accepted because their digits occur in SOURCE. Frozen statistical slots
  explicitly carry metric, value, unit, subject, period and source. Checks cover
  goals, assists, shots, touches, xG and big-chance unit separation; numeric
  statistics without a supported schema are rejected. Match/table statistics
  also receive typed slots. SOURCE-as-of table snapshots are labelled as such,
  not presented as an invented season or fixture date.
- `daily_fact_patterns.py`: reusable reported asking-price, winter-target and
  explicit seller-price patterns recover three additional transfer reports.
  Asking prices are not paid fees, salaries or completed deals. Presentation
  aliases translate literal names without completing unnamed actors.
- Related transfer reports retain separate conditions, currencies and status,
  with a shorter deterministic presentation. Frozen facts are unchanged by edit.
- `daily_fact_e2e.py`: `prepare_immutable_edition` is a reusable preparation
  function, not a worker or a second sender. It builds and validates an edition
  without any model. `BoundedStyleChat` is optional: loopback-only endpoint,
  12-second HTTP timeout, 2048 local context, 128 output tokens, at most one call,
  <=2400-character user prompt and no retry. Global Ollama settings are untouched.
- `PublishedFactStore` uses the same existing daily SQLite receipt contract.
  A staged manifest is not published history. Only PUBLISHED, matching body hash
  and non-null message receipt make its actually printed bundles usable. A crash
  after PUBLISHED but before signature finalization preserves staged facts
  without sending again. SENDING/ambiguous delivery never enters history.
- The existing finite shadow runner replays saved collection, same-day outage
  fallback, preparation, payload and the real `DigestLedger`/`publish_prepared`
  functions with a fake in-memory API. All databases are in isolated artifacts.
  Resource-busy/unknown detection skips optional editing; owned model cleanup
  does not enqueue another unload request after timeout or stop foreign models.

## Qwen timeout diagnosis — proven versus unknown

The previous 95.688 / 90.656 / 87.016 seconds were total replay times, not three
successful model responses. The model/setup/unload stage took approximately
65 seconds, including the 60-second request deadline and about five seconds
from the follow-up unload request. Source processing took 22–31 seconds.

The first historical editing group was reconstructed from frozen variants:
897 user-prompt characters, three events; IDs match the recorded request group.
This is not a giant whole-SOURCE prompt. Context was 4096 and output cap 550;
the new optional path uses smaller per-request limits. Exact historical prompt
tokens, load duration and CPU/GPU offload split were not recorded. They cannot
be honestly reconstructed from wall-clock totals.

Concurrent foreign Ollama backends were observed during the original failures.
During this continuation, the GPU used 7496/8151 MiB at 99% utilization; its
processes were left alone. Earlier idle observation was 724 MiB / 2%.
Cold loading plus resource contention is a plausible contributor, not a proven
sole root cause. Two diagnostic launcher attempts made zero model calls: one
could not resolve its executable in the sandbox, another refused unavailable
resource inventory. These are fallback tests, NOT fresh successful Qwen replies.
The corrected resource inventory and VRAM gate remain fail-closed.

New successful responses can record allowlisted prompt/eval counts and separate
load/eval durations. Those meanings follow the official
[chat API](https://docs.ollama.com/api/chat). Loaded model/VRAM metadata is exposed
by [api/ps](https://docs.ollama.com/api/ps), while
[Ollama FAQ](https://docs.ollama.com/faq) explains CPU/GPU residency inspection.
Read-only main-endpoint inspection was unavailable; no model was loaded just to
obtain these measurements under contention. Availability of optional editing
is not an acceptance requirement for a correct deterministic edition.

## Tests and saved-source route replay

284 focused tests PASS in 34.086 seconds, including 29 public-export mechanism
tests. This is not a claim that the full historical suite passes. An intermediate
test fixture changed source references without changing its new typed-stat
references; the fixture was corrected to preserve provenance, not to relax the
gate. No remaining focused failure.

Three independent isolated ledger chains replay 11, 12 and 13 October:

| Chain | 11 October | 12 October | 13 October | Three-day total |
|---|---:|---:|---:|---:|
| 1 | 7.907 s | 7.750 s | 15.765 s | 31.422 s |
| 2 | 4.531 s | 4.563 s | 8.203 s | 17.297 s |
| 3 | 3.984 s | 4.406 s | 8.328 s | 16.718 s |

11 October: 16 events / 2580 UTF-16. 12 October: 15 / 2186, not padded.
13 October: 17 / 2839; 32 included essential facts retained, zero detected
distortion, provenance defects, promotion or duplicate events. Replay chains
use zero LLM calls and zero live sends. Same-day double publication is blocked
in all nine editions. Separate tests exercise timeout, Ollama unavailable,
ambiguous send, matching-receipt recovery and transport/fact validation.

These times include saved ingestion and SQLite/fake receipt, NOT current network
collection or real Telegram latency. They leave >464 seconds of the 480-second
window before network overhead, but do not prove network-to-delivery SLA.

Private evidence: `artifacts/daily-route-shadow/20261014-072331/summary.json`,
per-day phase JSONs, isolated SQLite and `3/2026-10-13.txt`. SOURCE archives,
receipts and private logs must remain excluded from public export.

## Important-news coverage and remaining blockers

Recovered three previously unsupported concrete transfer sources: winter
interest with an explicit seller price, a distinct reported purchase threshold,
and a named January target. The contradictory statistical table is intentionally
omitted. A supported season goals/assists point initially lacked the new typed
schema; that false rejection was fixed and regression-tested.

Still unsupported: a reported youth-contract requirements story and a financial
regulator report (potentially important), a probabilistic forecast, and a longer
player-record comparison. Generic polls/lineup teasers are not important missing
events. An official signing's extra source is represented by its main bundle,
not a lost signing. Unnamed buyers and incomplete U21 membership remain
unverified, not completed from memory. Claiming zero important SOURCE omissions
across the whole feed would be false.

Other blockers: incomplete X/manual ingestion coverage, conservative incomplete
legacy free-text history evidence, remaining source-shape/actor alias ambiguities,
and untested current network SLA. A surname-only report and a full-name report
are not automatically merged if identity cannot be proved. Full support for
same teams in different independent fixtures needs explicit fixture context;
do not treat day labels alone as a new fact or as sufficient fixture identity.

## Exact safe integration plan — not executed

1. Complete important-source coverage and legacy-history review; prepare a
   current-day isolated collection-to-payload acceptance with real per-feed
   coverage. Recheck read-only scheduler XML with the owner identity; last
   successful read confirmed 20:56, this sandbox's repeat read was denied.
2. After owner approval, replace ONLY the preparation block inside existing
   `daily_text_digest.run_daily` with `prepare_immutable_edition`. Do not add a
   second task/worker/sender. Route `--prepare` through the same preparation
   function, not the old free-form rewrite cache. Keep collection/accumulation,
   process lock, pause checks, window checks and existing DigestLedger unchanged.
3. Filter existing source tombstones before preparation; recheck all selected
   original references after editing. Adapt selected bundles to existing
   DigestStory/source snapshots for `DailyState.freeze`, inclusion bookkeeping
   and secondary delivery; retain exact mappings. This adapter is NOT yet live
   integrated or accepted. Do not delete old caches/ledgers to migrate.
4. Stage the validated manifest and code/config/parser fingerprints under the
   existing day claim, then invoke the existing single `publish_prepared` once.
   Finalize facts only after its matching receipt. Use the same daily_digest
   day key so old and new implementations cannot deliver independent editions.
   SENDING and uncertain receipts stop permanently pending exact reconciliation.
5. Initially use zero optional model calls. After idle-resource validation,
   enable only the bounded style selector; never fall back to legacy free-form
   regeneration on a model failure. Preserve all current settings/platform flows
   and the 20:56 trigger. Respect the 480-second supervisor/window deadline.
6. First real edition diagnostics: executing code/config/parser hashes,
   source window and per-feed coverage, omissions, included fact count, section
   and UTF-16 checks, phase timings, style skip/error and actual model-call count,
   staged manifest, Telegram receipt, exact X mappings and secondary status.
7. Rollback restores only a reviewed entry/preparation code snapshot, not Git
   reset/clean, settings, owner changes or DB history. Keep receipt and immutable
   namespaces. If the day is PUBLISHED/SENDING, rollback must NOT resend it.
   Remove the retired preparation path only after the controlled edition and
   rollback rehearsal pass; keep one active daily route throughout.

## Public export security boundary

Changed-code Gitleaks PASS: zero findings, redaction, no allowlist. Reviewed
Bandit results contain six LOW operator-runner observations: subprocess import,
three controlled shell-free subprocess calls, two intentionally swallowed
availability-probe errors. See private `artifacts/daily-rollout-bandit.json`.
The runner's commands are fixed inventory/GPU probes and an owned finite server,
not SOURCE-supplied commands or a production publisher. It remains an operator
file requiring an explicit hash-bound export review; this rationale does not
silently amend policy or owner approval.

The existing 13 October 22:00 sync report is BLOCKED / pushed false /
NEEDS_REVIEW_UNKNOWN_FILE for PROJECT_CONTEXT.md. Keep operator continuation
context excluded or review an inert public overlay by the existing reviewed-seed
process. That policy refresh requires a renewed owner approval bound to its hash.
No activation flag, scanner exception, history import or force push was used.
Full public candidate/history gates did not run past the boundary blocker and
are NOT claimed PASS. New source/tests are source-authored; no downloaded media,
model bytes or private dataset was added. Production readiness remains NOT_READY.
