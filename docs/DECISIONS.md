# Main technical decisions

| Decision | Reason and trade-off |
|---|---|
| Python/FastAPI backend; Next.js/TypeScript frontend | Allowed assessment choices; clear REST and UI separation. Company stack examples are not all mandatory. |
| Gemini REST via HTTPX | Existing configured chat/embedding provider; one owned client/controller bounds retries and concurrency. Provider quota/format/latency remain external limits. |
| MongoDB persistent store and small-corpus cosine retrieval | Reuses one local store for scoped state, business records, documents and vectors; avoids unnecessary distributed infrastructure. Corpus size is bounded. |
| Policies from RAG, current facts/actions from tools | Prevents stale brochures and model prose from inventing fees, availability or successful writes. Sources retain document/version/chunk references. |
| Structured proposals plus application validation | Model output proposes facts/operations; code checks arguments, evidence, ownership and tool permissions. Unsupported facts are not invented. |
| Registered business behavior and guarded conversational wording | Shared orchestration dispatches domain behavior; evidence-linked drafts receive schema, fact and semantic checks. Rejected wording falls back to verified facts/receipts. Semantic review adds latency and remains probabilistic. |
| Discussion focus separate from selected interest | A factual question is not automatically a course choice. A subsequent unambiguous positive interest statement can checkpoint the discussed course without replacing meaningful prior interest with filler. |
| Focused exact-policy-quote recovery | If a broad reply omits supporting passages, one document-only structured extraction can recover them. Exact quotes, joint conditions and source ownership are rechecked; no whole-document dump or repeated write. |
| Durable message identity, session gate and receipts | Duplicate/repeated delivery reuses actual effects. Safe prepared responses can be replayed; ambiguous interruption windows remain explicit rather than promising distributed exactly-once delivery. |
| Leased local automation worker | A persisted lead event triggers summary/storage/local notification with stage reuse and retry; no Kafka or real CRM is needed for this exercise. |
| Common normalize/encode adapter contract | Web and mock payload details stay outside the core AI engine; a new channel is a separate integration. |
| File-based business configuration and registered tools | A trusted allowlist is enforced in code. New tool schema/handler/presentation registration extends the shared core; actual data still needs provisioning. |
| Functional trusted local UI | Four screens use real APIs. Production auth/deployment and extra admin editors are outside the requested prototype scope. |

See [architecture](ARCHITECTURE.md), [extension guide](EXTENDING.md), [tests](TESTING.md) and [limits](LIMITATIONS.md).
