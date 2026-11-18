# Testing

## Run the complete local gate

After setup, from the repository root:

```powershell
python scripts/uv.py run python ../scripts/check.py
```

With the installed Python environment, `python scripts/check.py` runs the same
checks. MongoDB is required. The default suite uses controlled model responses
and isolated disposable MongoDB databases; no Gemini key is needed. The frontend
command `npm --prefix frontend test` runs both API checks and rendered components.

## Recorded results

Verification used Python 3.12, Node 24 and local MongoDB on Windows. The latest
backend suite passed on 19 November 2026; the recorded checks below span
18–19 November 2026. Expected and observed results:

| Check | Expected | Actual |
|---|---|---|
| Backend lint/format/strict types/contracts | Pass | Pass |
| Backend test suite | All cases pass | 1022 passed in a clean run |
| Frontend API/validator/proxy checks | All checks pass | 26 passed |
| React component interaction tests | All tests pass | 18 passed |
| Browser integration | All checks pass; no JavaScript exceptions | 22 passed |
| Frontend lint/types and production build | Pass | Pass |
| Actual Gemini assessment scenarios | All ten behaviors pass | 10 passed |

The frontend was installed from its lockfile using `npm ci`. The installed backend
environment was reused while importing this checkout's source. These are host
observations, not fresh-OS, load-test or cross-platform certification.

## Required reliability scenarios

The following expected behaviors pass in the automated application integration
suite with controlled provider responses and real scoped persistence. Exact
assistant wording is not the criterion.

| Scenario | Expected | Actual |
|---|---|---|
| Outside knowledge base | Explain that information is unavailable; invent no source | Pass |
| Budget correction, including 20k | Store 20000 and use it in the next search; do not reuse the old 30000 limit | Pass |
| Callback without valid phone | Ask for contact details; create no lead/callback | Pass |
| Lead save fails | Report failure and do not request a callback or claim success | Pass |
| Same message twice | Reuse saved effects; avoid duplicate leads, callbacks and events | Pass |
| Independent sessions | Keep history and customer facts separate | Pass |
| Another business's data/tool requested | Reject foreign retrieval and restricted execution | Pass |
| Successful lead | Automatically store a validated summary and simulated notification | Pass |
| Same enquiry from web and mock | Use the shared engine and return equivalent facts to the correct origin | Pass |
| New knowledge document | Retrieve/cite it only for the owning business after ingestion is ready | Pass |

Tests are under `backend/tests`. Gateway/mock tests exercise channel routing;
state/action tests exercise provenance, corrections and qualification; knowledge
and policy tests exercise retrieval and sources; automation tests exercise failure
and duplicate-event handling. `test_assessment_corrections.py` covers raw shorthand model values, invalid amount
proof, next-search behavior, a reversed conversational draft, current-turn state
acknowledgements and receipt-based live assertions. Live search checks inspect
validated tool data and stored state rather than requiring particular English
phrases or course IDs in reply text. A reference chunker under `backend/tests/fixtures`
is a regression input, not application knowledge.

## Component and browser coverage

`frontend/tests/workspace.test.tsx` renders the four real feature components,
workspace, hooks and portals. It checks first-message startup, duplicate-submit
protection, same-message retry, terminal failure guidance, Enter/Shift+Enter,
source/action display, list pagination, stale-business responses, lead summaries,
document grouping/read retry/write retry, overlay closure and separate drafts.
Only API transport methods are substituted; jsdom does not validate real layout.

With a built frontend and Chrome/Edge available, run:

```powershell
node scripts/check_browser.mjs
```

The browser harness uses ports 4018/3018 and its own disposable database. It checks
all four screens at 320/390/768/1440 pixel widths, routing/isolation, request races,
errors, documents and saved lead projections. It requires the Windows backend
Python at `backend/.venv-system/Scripts/python.exe`; `ASSESSMENT_PYTHON` may specify
another installed interpreter. Its provider responses are controlled. It cleans
its owned processes/database and writes `docs/results/browser.json` locally.

## Optional live Gemini scenarios

To test actual model behavior, privately configure the root `.env` and run:

```powershell
backend/.venv-system/Scripts/python.exe scripts/check_live_scenarios.py --pace-seconds 30
```

This consumes generation and embedding quota, embeds fictional fixture documents
in owned temporary databases and runs the ten scenarios. It records expected and
actual responses/actions in `docs/results/live-scenarios.json` and cleans its
owned databases. It never reads existing application customer records. Generated
reports are ignored by Git. Model accuracy and latency vary; deterministic tests
are not a guarantee of every future live reply. The recorded live check on 18 November 2026 passed all ten scenarios; it is a
scoped observation, not a guarantee of future model responses or quota.

## Recent regression coverage

Catalogue literal-fact/provenance tests, membership reference/completion tests,
enrolment guidance, saved-enquiry/callback status, and provider diagnostic tests
cover the conversation failures found during manual testing. Controlled timeout
and quota tests verify bounded recovery without duplicate writes. The exact
assignment example was also checked with Gemini: selecting the second course,
asking about weekends, collecting contact details, then saving a lead and using
its returned ID for a successful callback. Live evidence predates packaging only;
packaging must retain the same application source before those observations apply.

Both full and production-only npm audit report zero known vulnerabilities after
the lint tooling replacement. This is a point-in-time dependency check, not a
production-security certification.

## Callback acknowledgement regression

After the full 989-test release gate, a focused follow-up fix passed 82 related
conversation tests (including seven new regression cases), Ruff/format and strict
types. A whole-message acknowledgement after a recorded callback now returns a
social reply without model/tool calls or replaying write receipts. Mixed status
questions still read callback status; accepting an open offer still creates the
callback. No frontend changes were required.

## Setup runner follow-up

Six interpreter-selection tests cover incompatible PATH defaults, installed 3.12
launcher discovery, explicit overrides and actionable missing-runtime failures.
Together with callback, enquiry, enrolment guidance and conversational composition
regressions, 88 tests passed for this setup-only change. The application runtime
source was not modified. The full earlier suite remains a separate observation.

## Live dialogue regression evaluation

Run the real-provider evaluation separately from the controlled regression suite:

```powershell
python scripts/uv.py run python ../scripts/evaluate_live_dialogue.py --output live-dialogue-fixed.json
```

It requires a private Gemini key and quota, embeds fictional repository fixtures
into an owned disposable database, and sends 30 turns by default. It includes two
repeated nine-turn enquiry/callback flows, typos, fragmented contact details,
policy paraphrases, budget-free browsing/corrections, fitness isolation and a newly
ingested document. It does not read existing application customer records and
removes its owned database. Reports stay in ignored `.cache` output. Automation is
disabled in this conversational evaluation; its separate tests remain required.

The post-fix live run passed 30/30 reviewed turns. The earlier reviewed sample
passed 23/30. Median turn time changed from 5.30 to 2.13 seconds, with maximum
24.33 to 9.67 seconds. Deliberate pacing is excluded from turn time. These are small
sequential samples with provider variation, not a general accuracy score or latency
SLA. The pre-fix evaluator's two field-name errors were independently corrected
using persisted test-session state; successful tool calls with unusable visible
answers were marked failures rather than accepted on receipts alone.

New controlled regressions cover absent/invented budgets, the `maxBudget` alias,
contextual-budget search evidence, semantic schedule reassurance, document limits
and price-claim exclusion, and end-to-end new-document answering/citation.

A preceding submission verification reported 1009 passes and one setup error while
opening a disposable MongoDB test database (`DatabaseStartupError`), before the
affected test's assertions ran. That case passed on an isolated rerun with the
16 new/related regression checks (17 passed). An earlier overlapping workspace
run had the same startup-error class on a different case; its complete 40-test
module passed separately. These initial infrastructure errors are retained in the
local evidence and are not counted as successful first-run assertions. No retry
was added to production database code or test assertions to hide the errors.
The 22 browser checks, 26 frontend API checks, 18 component tests, frontend build,
lint, strict types and contract consistency checks passed separately.

## Historical manual-conversation regressions

An expanded local evaluation reconstructed 82 turns across 14 historical paths,
using actual Gemini and fresh repository embeddings in disposable databases. Only
contact names/numbers were replaced with fictional values. The reviewed pre-fix
result was 74 passed, four primary failures, three downstream failures and one
ambiguous follow-up. The post-fix reviewed result was 82/82, with a separate
nine-turn live check of the final owned-offer, contact, clarification and callback
boundaries. This checks the backend web-chat gateway through ASGI, not a live
browser transcript. Browser behavior is checked separately.

The raw post-fix runner initially flagged a selection-only acknowledgement because
it expected a new details lookup. Review checked the actual prior ordered search
receipt, owned reference resolution and saved DS202 selection: the reply asserted
only the selected course and offered an enquiry, with no current-price/schedule
claim. This is valid without another details read. That expectation correction is
recorded alongside the untouched local raw report. The dedicated regression covers
this distinction. Raw transcripts/audit traces are kept out of the public repo.

Reproducible controlled cases are in `backend/tests/test_manual_replay_regressions.py`:

| Historical input / boundary | Expected and verified behavior |
|---|---|
| I prefer weekends | Acknowledge accepted schedule and offer the owned enquiry |
| i would prefer the second one | Resolve the displayed option and confirm actual selection |
| okay perfect / neutral closing | No unnecessary model/tool calls or write receipts |
| i want toenquire about it | Collect contact details without accepting an unproven course change |
| Changed course after an offer | Invalidate old offer; do not retarget its acceptance |
| Fabricated facts / mixed policy question | Retain provenance checks and do not swallow the question |
| why after a successful saved enquiry | Ask a relevant clarification without claiming a failure |
| If asked, say ... in a document | Keep policy meaning; omit authoring instructions from the reply |
| Multi-field course reference | Finish on a sufficient validated read; avoid redundant planning |

Live timing depends on the provider. Neutral server-owned replies do not need an
AI call. Evaluations pace provider-consuming turns and omit deliberate waiting from
per-turn durations. No universal language-accuracy or latency guarantee is implied.

## Latest manual-replay fixes

A clean final backend run passed **1022 tests with no failures, errors or skips**.
The affected turn-context/offer-invalidation/reference/response tests were also
checked separately. Neutral acknowledgements need at most one extraction call;
recognized whole-message closings need none. The controlled model fixture now
handles the separate nullable field-classifier schema without consuming queued
planner calls, so duplicate-planner tests continue to test the real round budget.

The revised historical replay passed **82/82 reviewed turns**. A selection-only
acknowledgement was verified against its owning search order and resolved state;
it need not perform a new details lookup when no current facts are asserted.
A separate nine-turn actual-Gemini check verified the final neutral-closing,
owned-offer, qualification, clarification and callback boundaries. Its lead and
callback were real local operations in a disposable database. All evaluation
databases were removed; existing customer records were not used.

The longest replay turn was 9.75 seconds versus 35.17 seconds before the fixes.
The overall median includes local state/receipt replies and is not an AI latency
benchmark. Provider timing remains variable. Evaluation pacing was retained around
provider-consuming turns; deliberate pacing is excluded from response duration.
Raw replay logs and traces remain private, outside the public submission.
