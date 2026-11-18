# Known limitations and incomplete production work

No assessment-required feature is intentionally left as a placeholder in this snapshot. The passing tests demonstrate their recorded scope; they do not certify every model response or the candidate's interview performance.

- Gemini generation, embeddings and summaries require the reviewer's valid key and available project/model quota. Latency varies. Invalid model output is validated and repaired only within bounded attempts, then fails honestly; it can still require a new customer message.
- Current fees, schedules and availability come from business tools. A document cannot confirm a successful operation, payment, reservation or live inventory. Course knowledge upload does not create a course catalogue record.
- Local trusted operator UI/APIs have no production user/role authorization layer. Do not expose this prototype publicly as-is. Web session tokens live in browser memory; reload starts a new writable session while saved history remains inspectable.
- One initial saved enquiry snapshot is retained per session. Later corrections affect future searches/state but do not silently rewrite a saved lead or retarget its callback. Start a new conversation for a fresh enquiry; a production update/cancel workflow would need explicit design.
- Mock external messaging and CRM notifications are local simulations. A callback request is not a scheduled/completed phone call. Real messaging delivery, signatures/media and CRM delivery are not implemented or required.
- No payments, direct enrollment, seat reservation, business editor, course editor, analytics or workflow builder is included in the required UI.
- Retrieval and worker capacity are bounded for a small local assessment corpus (499-chunk cap). There is no distributed throughput or production disaster-recovery claim. Ambiguous pre-prepared-response write interruptions can require operator review rather than unsafe automatic retries.
- Trusted configuration/files are authoritative. New businesses need their own documents/data and registered tools; this is not a customer-uploaded executable plugin system. Source policy truth still depends on trusted document ownership.
- Python 3.12/Node 24 and the pinned dependencies were qualified on the development host. Reviewers need to install prerequisites; cached same-host verification is not proof of a clean operating-system installation.

Main technical decisions and extension approaches are in [DECISIONS](DECISIONS.md) and [EXTENDING](EXTENDING.md). Production hosting/authentication, maintained-runtime rollout, cross-channel customer identity and real integrations would need additional work after the assessment. Publishing the source repository does not deploy the application. No automatic account billing change is performed.

## Dependency audit at segregation time

The locked frontend install reported **12 high-severity dependency entries**, zero critical, in `npm audit`. All reported affected nodes are marked development dependencies in the lockfile (lint/glob tooling). These are advisory records, not proof of 12 distinct exploitable application endpoints. They still require dependency/security review; no risk dismissal is claimed. Package versions were left unchanged to preserve the verified application during segregation. Do not run a forced major update without reviewing its compatibility and rerunning checks. [Raw advisory report](evidence/dependency-audit.json).
