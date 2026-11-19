# Main technical decisions

| Decision | Reason and trade-off |
|---|---|
| Python/FastAPI backend; Next.js/TypeScript frontend | Allowed assessment choices; clear REST and UI separation. Company stack examples are not all mandatory. |
| Gemini REST via HTTPX | Existing configured chat/embedding provider; one owned client/controller bounds retries and concurrency. Provider quota/format/latency remain external limits. |
| MongoDB persistent store and small-corpus cosine retrieval | Reuses one local store for scoped state, business records, documents and vectors; avoids unnecessary distributed infrastructure. Corpus size is bounded. |
| Policies from RAG, current facts/actions from tools | Prevents stale brochures and model prose from inventing fees, availability or successful writes. Sources retain document/version/chunk references. |
| Structured proposals plus application validation | Model output proposes facts/operations; code checks arguments, evidence, ownership and tool permissions. Unsupported facts are not invented. |
| Registered business behavior and guarded conversational wording | Shared orchestration dispatches domain behavior; evidence-linked drafts receive schema, fact and semantic checks. Rejected wording falls back to verified facts/receipts. Semantic review adds latency and remains probabilistic. |
| Canonical budget normalization | One validated amount grammar is shared by extraction and provenance checks. Common forms such as 20k, 20,000 and INR 20000 retain their actual numeric meaning. Unsupported or unproven values are rejected. |
| Stable ordered course lists | Server-rendered search rows keep the same order as persistent ordinal references. Conversational wording cannot reorder those rows; detail/policy answers retain the checked conversational path. |
| Discussion focus separate from selected interest | A factual question is not automatically a course choice. A subsequent unambiguous positive interest statement can checkpoint the discussed course without replacing meaningful prior interest with filler. |
| Bounded intent spelling hints | One-edit insertion/deletion/substitution/transposition matching handles enquiry keywords and whole-message ordinal selections. Original input remains the provenance source. Customer values and course IDs are never autocorrected; negation, quotation and ambiguity checks still apply. |
| Clear enquiry requests use the owned dialogue transition | Whole-message requests such as "I want to enquire about it" reuse the session's validated course and contact-collection state, without requiring model classification. Mixed, quoted, conditional or differently named requests retain normal interpretation. This removes avoidable provider latency from the handoff; callback consent and actual write receipts remain mandatory. |
| Shared callback wording and saved-lead authorization | Normalize call back/call-back/callback in one consent policy. Explicit requests against an owned saved lead use its returned ID directly; status questions, quotations and revocations remain non-authorizing. Repeated completed requests acknowledge the saved callback without another write. |
| Focused exact-policy-quote recovery | If a broad reply omits supporting passages, one document-only structured extraction can recover them. Exact quotes, joint conditions and source ownership are rechecked; no whole-document dump or repeated write. |
| Durable message identity, session gate and receipts | Duplicate/repeated delivery reuses actual effects. Safe prepared responses can be replayed; ambiguous interruption windows remain explicit rather than promising distributed exactly-once delivery. |
| Leased local automation worker | A persisted lead event triggers summary/storage/local notification with stage reuse and retry; no Kafka or real CRM is needed for this exercise. |
| Common normalize/encode adapter contract | Web and mock payload details stay outside the core AI engine; a new channel is a separate integration. |
| File-based business configuration and registered tools | A trusted allowlist is enforced in code. New tool schema/handler/presentation registration extends the shared core; actual data still needs provisioning. |
| Functional trusted local UI | Four screens use real APIs. Production auth/deployment and extra admin editors are outside the requested prototype scope. |

See [architecture](ARCHITECTURE.md), [extension guide](EXTENDING.md), [tests](TESTING.md) and [limits](LIMITATIONS.md).

## Read-path and tooling refinements

- Clear catalogue searches use literal, evidence-validated state extraction to
  avoid a redundant provider call. Tool choice remains with the agent. Registered
  read completion avoids further model calls after a sufficient validated result.
  Neither change skips write validation, business boundaries or consent checks.
- Membership focus is reconstructed from the owning session's displayed receipts
  and refreshed through its own tool. No training workflow is enabled for fitness.
- Biome replaces the ESLint dependency chains affected by the unpatched picomatch
  advisory. TypeScript checking, component tests and build remain separate gates.
  The configuration deliberately excludes additional stylistic/accessibility
  policies that the previous lint stack did not enforce; this is a tooling change,
  not a UI rewrite. Biome's recommended checks and Next.js rules otherwise apply.
  No npm audit finding is ignored or suppressed.
