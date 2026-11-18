# Tests and recorded results

Run from the repository root. Python dependencies and Mongo must be available; frontend checks require installed npm dependencies. Windows setup is in the [README](../README.md).

```powershell
powershell -NoProfile -File scripts/check.ps1
```

The default suite substitutes model HTTP but exercises real scoped Mongo operations in owned disposable test databases. Expected: 760 backend tests and 26 frontend API/proxy checks pass; lint/types/contracts/build pass. [Recorded backend result](evidence/backend-final.xml), [frontend API result](evidence/frontend-api-final.json). Counts are qualification snapshots, not an employer score.

## Optional actual Gemini qualification

These commands consume generation/embedding quota. Use your own private key and run sequentially. New fictional documents are embedded in owned temporary databases; existing application customer records are not read/copied. The databases are removed in cleanup.

```powershell
backend/.venv-system/Scripts/python.exe scripts/reassess_assignment_live.py --output SUBMISSION_FINAL-live.json --pace-seconds 30
backend/.venv-system/Scripts/python.exe scripts/verify_assessment_sections.py
```

The first records its rerun as `docs/evidence/SUBMISSION_FINAL-live.json`; the second writes `docs/evidence/SECTIONS_FIX-after-live.json`. The `.venv-system` location is created by the Windows uv runner. On another environment, activate the equivalent Python 3.12 environment and run the same scripts with its Python. Longer pacing does not bypass daily provider limits.

[Actual recorded ten-scenario results](evidence/live-scenarios-final.json) contain expected and actual responses/tool traces and PASS status:

| Case | Expected | Recorded |
|---|---|---|
| A01 | Unsupported information is unavailable; no fabricated facts/actions | PASS |
| A02 | Revised budget stored and used by the next search | PASS |
| A03 | Missing valid phone requested before lead/callback creation | PASS |
| A04 | Lead-save failure does not claim success or request callback | PASS |
| A05 | Duplicate/repeated requests retain one set of saved effects | PASS |
| A06 | Independent sessions retain separate history/details | PASS |
| A07 | Another business's data/documents/tools remain inaccessible | PASS |
| A08 | Successful lead automatically gets a validated stored summary/local notification | PASS |
| A09 | Web/mock share the engine and route equivalent facts to the correct recipient | PASS |
| A10 | New document is cited for its owner and isolated from the other business | PASS |

[Literal conversation/policy results](evidence/conversation-policy-final.json) verify DS202 selection, contact collection, the actual saved course and callback linkage, prerequisite citation and both refund conditions. [Focused actual-provider check](evidence/policy-recovery-final.json) deliberately omitted the initial quote selection and verified actual Gemini quote recovery. Regression tests cover invalid/malicious quotes, refusals, mixed corrections, absent callback consent and bounded semantic state repairs.

## Optional browser/routing checks

[Recorded 22-check browser integration result](evidence/browser-integration-final.json) covers four screens, responsive widths, ownership races, backend errors, document reader/addition and actual lead REST projections. It uses controlled model behavior, not live Gemini.

`node scripts/audit_integration_browser.mjs` reproduces this isolated local browser/API check on Windows with Chrome/Edge installed, built frontend dependencies and Python at `backend/.venv-system`. It uses ports 4018/3018, writes generated results under `docs/evidence/DIRECT_MESSAGE-browser.json`, and cleans its owned processes/database. Avoid port conflicts. `python scripts/check_mock_setup.py` verifies configured private mock keys in an owned database without model requests; configure both demonstration keys first.

`docs/evidence/SD08-chunker-before.py` is intentionally retained: the chunking regression imports it as a reference test fixture. It is not obsolete documentation. No fresh-OS/dependency-installation or production-load guarantee is inferred from these recorded host checks. See [limits](LIMITATIONS.md).

## Clean-copy verification

After segregation, the copied backend passed all 760 tests from its own source root using the already qualified Python environment. A separate disposable copy installed the locked frontend dependencies and passed lint/types, all 26 API checks and a production build. No caches/dependencies/build outputs were copied back into the handoff. Live tests were not rerun solely for packaging because the application/test source bytes are unchanged. [Verification record](evidence/clean-copy-verification.json). This is same-host verification, not a fresh-OS qualification.

Dependency installation also reported advisory entries. See [limitations](LIMITATIONS.md) and the included dependency audit; passing functionality does not mean an advisory-free dependency tree.
