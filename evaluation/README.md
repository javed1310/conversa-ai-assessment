# Frozen agent and retrieval evaluation

This evaluation adds measurement without changing application routing, prompts, tools,
retrieval thresholds or response generation. It complements the regression suite; it
does not replace the assessment's reliability and channel tests.

`development_cases.json` contains four development examples. `heldout_cases.json`
contains twenty separately authored synthetic cases, including multi-turn state,
mixed policy/current-price questions, unknown information, isolation, lead collection
and new document versions. The manifest freezes their hashes, the application code
and the business corpus before the first live run. Do not tune against the held-out
set and continue calling it held-out: move it to regression coverage and author a
fresh evaluation set for a later candidate.

From the repository root, with backend dependencies installed and MongoDB running:

```powershell
backend/.venv/Scripts/python.exe scripts/evaluate_heldout.py --env-file .env --candidate-label submission
```

Use the interpreter containing the backend dependencies if your environment has a
different name. The command uses real Gemini chat and embeddings, so a valid key
and sufficient quota are required. It creates and removes only its own uniquely
named `ai_heldout_` MongoDB database. It seeds fictional data; existing application
sessions and leads are not read. Background automation is disabled in this runner
because automation is covered by the regression suite.

The published application includes fixes made after the frozen baseline. Label its
run as a candidate, as above. Omit `--candidate-label` only when application code
matches the original manifest; otherwise the runner deliberately refuses to label
changed code as the original baseline. The recorded fingerprints were generated on
Windows; the optional evaluation runner has not been qualified on other platforms.

Results are private, ignored files in `.cache/heldout-baseline-<timestamp>.json`.
Each run has a separate file so a later run preserves prior evidence. Do not commit
raw outputs. The report separates these checks:

- Actual successful tool receipts and persisted preferences/lead state.
- Expected answer facts, false refusals and source document/version correctness.
- Original-query top-k document recall and recall after the existing threshold.
- Observed operation names, argument schema validity and final trace category.

The evaluator forwards actual operation results and exceptions unchanged. Its
decision trace excludes argument values, result bodies, credentials and customer
identifiers. It records observations, not hidden model reasoning. The report's
responses use synthetic test customers only.

An automatic `CHECK` requires inspection, not an immediate production change.
Patterns can miss a valid paraphrase or overlook a semantic error. Manually review
all replies, including automatic passes, for correctness and relevant citations.
Retrieval recall measures evidence coverage, not answer accuracy. A successful
runner exit means execution completed; it does not mean every quality check passed.
Live latency includes model/provider variability and evaluation instrumentation.
Daily quota exhaustion interrupts the run and is not evidence of an answer-quality
failure. Remaining cases stay untested. Rerun after quota resets; do not substitute
mocked model answers or reduce the cases to claim a complete live result.

Run the evaluation harness tests separately:

```powershell
cd backend
python -m pytest tests/test_evaluation_support.py -q
```

These tests check trace privacy, unchanged operation results/exceptions, independent
answer/source scoring and development/held-out separation.

After fixing a discovered issue, label a regression run explicitly:

```powershell
backend/.venv/Scripts/python.exe scripts/evaluate_heldout.py --env-file .env --candidate-label contact-fix --case-ids h01-advanced-suitability h06-undocumented-age h14-fragmented-enquiry
```

This permits changed application code while retaining its actual fingerprint in a
separate `regression-candidate-<timestamp>.json` report. The frozen dataset/corpus
hashes still apply. The report counts only the selected cases. These are regression
results after inspection of the baseline, not an independent held-out accuracy claim.
