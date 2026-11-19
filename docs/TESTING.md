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
| Backend test suite | All cases pass | 1099 passed in the callback-wording release run |
| Frontend API/validator/proxy checks | All checks pass | 26 passed |
| React component interaction tests | All tests pass | 18 passed |
| Browser integration | All checks pass; no JavaScript exceptions | 22 passed |
| Frontend lint | Pass | Previously passed on unchanged source; latest rerun blocked by Windows Application Control |
| Frontend types and production build | Pass | Passed again in the callback-wording release run |
| Actual Gemini assessment scenarios | All ten behaviors pass | 10 passed again in the enquiry-handoff fix retest |
| Published baseline expanded live regression (historical) | All twenty scenarios / twenty-four turns pass | 24/24 automatic checks; replies manually reviewed |
| Enquiry-handoff fix live replay | Reported conversation flows pass | 30/30 existing runner turns and 25/25 additional replay turns; replies and records reviewed |

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

## Final submission verification — 19 November 2026

The final application passed **1053 backend tests** in a clean run, Ruff lint and
format checks, strict mypy checks over 50 application files, and generated-contract
comparison. Frontend lint, types, 26 API checks and 18 rendered-component tests
passed. The unchanged frontend build also passed the 22-check browser harness.

The expanded actual-Gemini regression ran **20 scenarios / 24 message turns**,
with **24/24 automatic passes** and manual review of every reply. It checked
prerequisites and a named introductory alternative, joint refund conditions,
unknown policies, live prices/availability, budget-free searches and corrections,
mixed policy/current-fee answers, fragmented contact collection, social closing,
fitness isolation, and newly added/revised documents. Expected and actual values
are reproducible with the authored cases and [evaluation runner](../evaluation/README.md).
The prior baseline and interrupted attempts were retained privately; they were not
relabelled as successful runs. Once used for fixes, these cases are regressions,
not fresh held-out generalization evidence.

The published baseline expanded live run's median API turn time was **4.98 seconds**, excluding deliberate
pacing. Retrieval covered the expected document for all nine answerable policy
queries at top-k and after the existing threshold. These are small-sample
observations, not a universal accuracy, threshold-calibration or latency guarantee.
No retrieval threshold was changed to obtain these results.

A separate final headless-browser flow used the actual frontend, REST gateway,
Gemini and automatic worker in an owned disposable database. Its eight chat turns
searched courses, selected the second result, retrieved a cited refund policy,
collected name and phone separately, saved a lead, requested a counselor callback
and closed the conversation. The Leads screen then displayed the actual requested
callback, generated counselor summary and stored local notification. Conversations
showed the saved dialogue, and Knowledge base listed the business documents. No
JavaScript exceptions were observed; owned services/database were cleaned up.
An earlier probe exposed unsupported counselor-callback consent wording, now covered
by a regression. A subsequent harness wait matched the outgoing Chat view during
navigation; the final probe waited for the actual lead card before opening it.
These earlier attempts were retained privately rather than counted as passes.

Regression additions ensure an explicit named-course enquiry owns its collection
goal, separate literal contact replies remain evidence-validated, a documented
beginner alternative survives optional paraphrasing, and an explicitly requested
fee is not lost when a matching successful course receipt already exists. Explicit
counselor callback wording is recognized while quoted requests and revocations
remain non-authorizing. No payment/enrolment or real notification integration was added.

Existing locked dependencies were reused while importing this checkout's source.
This is Windows host verification, not a fresh-OS installation qualification.
Raw responses, customer/session identifiers and browser captures remain ignored
local evidence; no application database export or private credentials are submitted.

## Enquiry-handoff fix verification — 19 November 2026

A fresh replay of the published baseline reproduced three incorrect replies: a
clear enquiry request, its name-only continuation, and an enquiry request after
a refund answer returned generic unavailable-information text. The fix routes
clear whole-message requests through the existing owned collection transition.
It preserves selected-course validation, contact evidence, qualification,
callback consent and actual write receipts. New controlled-provider regressions
also block model state extraction and verify that the handoff and separate
contact collection still complete correctly without calling it.

The complete backend gate passed **1,070 tests**. The existing live dialogue runner
passed **30/30 turns** with two enquiry-to-callback passes; an additional replay of
reported customer flows passed **25/25 turns**. The repaired enquiry handoffs took
approximately **0.06–0.08 seconds**, compared with the baseline's failed
43–59-second enquiry turns. The first 30-turn replay's observed median was
**2.05 seconds**. These measurements exclude deliberate pacing and are not a
general latency guarantee. One original additional-replay check was overly strict:
a selection-only acknowledgement need not call the details tool or claim current
facts. Its corrected check requires the actual stored DS202 selection and no
unrequested enquiry/callback write; the original flagged report was retained.

All **ten live assessment scenarios** passed with actual Gemini and newly embedded
fictional fixtures. The final run used 30-second message pacing to respect provider
request limits. Successful enquiry/callback receipts, Leads API callback visibility,
automatic structured summaries and matching simulated notifications were checked.
The **22 browser integration checks** passed against the fixed backend and the
unchanged, previously built frontend. The earlier frontend lint/types/build,
26 API checks and 18 rendered component tests remain applicable to that unchanged
frontend. All owned test databases were removed.

Gemini 503s and a deadline occurred and recovered. Some model-backed replies still
took roughly a minute. The fix removes unnecessary model classification from the
clear enquiry/contact handoff; it does not remove provider quota or latency from
RAG, broader interpretation or summaries. These are regression observations using
cases inspected during fixes, not fresh held-out accuracy evidence. Unfinished
architecture improvements were excluded from this candidate.

## Callback wording release verification — 19 November 2026

A manually reported request, "i want to request a call back", reached the callback
tool but was rejected by the shared authorization policy as
`CALLBACK_INTENT_REQUIRED`. The policy now normalizes call back/call-back/callback
and accepts explicit "I want to request..." wording. An owned saved lead can be
used directly without model classification. Mixed customer corrections remain on
the validated state path; callback status questions, quotations and revocations
remain non-authorizing. Existing short commands are preserved.

The final complete backend run passed **1,099 tests**, including 29 new callback
wording/persistence cases. Lint, formatting, strict types and generated contract
checks passed. Earlier checks exposed two historical assertions requiring redundant
lead/callback tool calls for an already completed request. They now verify the
stronger outcome: no repeated write, unchanged stored effect IDs, and unchanged
summary/notification data. A mixed phone-correction case also verifies that the
direct route cannot discard that supplied customer detail.

Fresh frontend verification passed TypeScript, **26 API/validator/proxy checks**,
**18 component tests**, the production build and **22 browser checks**. The latest
Biome lint invocation was blocked by this Windows host's Application Control
policy, including a direct version invocation. The combined verification command
stopped there; the remaining checks were run separately. No OS policy or linter
rule was bypassed. Frontend source and dependencies are unchanged from the earlier
successful lint run; a fresh lint pass is not claimed for this release check.

A fresh seven-turn actual-Gemini web-gateway replay searched the catalogue,
selected DS101, corrected a weekend assumption, collected an enquiry, saved a lead,
and requested a callback using the reported wording. The callback ID was visible
in the Leads API. An exact duplicate payload reused its saved reply, and a new
message repeating the request retained the same callback and one lead. The owned
fictional test database was removed. Summary automation was disabled in this
targeted live probe and remains covered by the full integration suite and earlier
live runs. Earlier 55-turn and ten-scenario live observations above are retained
as historical evidence; they were not relabelled as this callback-specific replay.

Reproduce the consent and integration regressions from the repository root:

```powershell
python scripts/uv.py run python -m pytest tests/test_callback_request_wording.py tests/test_callback_status.py tests/test_action_agent.py tests/test_reliability.py -q
```

## Intent spelling regression verification

Reported typos `first ine` and `i want to enquie about it` now use bounded shared
spelling hints for course selection and enquiry intent. Original input is retained
in history and provenance. Customer facts, IDs, prices and callback consent are not
fuzzily rewritten. Ambiguous or mixed messages retain the normal interpretation
path. This supports a defined one-edit tolerance, not arbitrary misspellings.

The affected agent, state, reference, enquiry and callback suites passed **186
tests**. Lint, strict types and contract export checks passed. The full 1,099-test
run above is the prior callback release's result; a fresh full-suite result is not
claimed for this focused spelling fix. Reproduce the new cases using
`python scripts/uv.py run python -m pytest tests/test_intent_spelling.py -q`.

A fresh actual-Gemini seven-turn replay using `first ine` and `enquie` passed,
including current course details, enquiry/contact collection, callback creation,
Leads API status, identical-payload replay and repeat-request duplicate protection.
The replay used fictional data and removed its owned database.
