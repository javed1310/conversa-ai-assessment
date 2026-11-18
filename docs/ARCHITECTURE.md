# Architecture

```mermaid
flowchart TD
  UI[Next.js: Chat / Conversations / Leads / Knowledge] --> Proxy[Same-origin API proxy]
  Proxy --> API[FastAPI routes and validated projections]
  External[Mock event + private channel key] --> Adapter[Resolve business / sender mapping]
  API --> Web[Web session token / adapter]
  Web --> Gateway[Shared normalized gateway / lease / replay]
  Adapter --> Gateway
  Config[Validated business files: role / KB / allowlist] --> Agent[GeminiAgent]
  Gateway --> Agent
  Agent --> State[Persistent evidence-backed customer state]
  Agent --> RAG[Own ready compatible chunks / calibrated cosine]
  Agent --> Tools[Validated scoped tool executor]
  Agent --> Model[Gemini generation adapter]
  Model --> Budget[Shared provider admission / deadline / retry policy]
  Budget --> Remote[Gemini REST API]
  Tools --> Reads[Course / membership data]
  Tools --> Lead[One qualified enquiry / pending event]
  Lead --> Callback[Persist actual callback request]
  Lead --> Worker[Automatic leased summary worker]
  Worker --> Summary[Actual six-field summary]
  Summary --> Notify[Stored simulated CRM notification]
  Agent --> Seal[Immutable prepared reply]
  Seal --> Gateway
  Gateway --> Origin[Encode reply to actual origin / recipient]
  API --> Ingest[Document text / Markdown ingestion]
  Ingest --> Embedding[Gemini retrieval embeddings]
  Embedding --> Budget
  Embedding --> DB[(MongoDB)]
  State --> DB
  RAG --> DB
  Reads --> DB
  Lead --> DB
  Callback --> DB
  Worker --> DB
  Seal --> DB
  classDef ui fill:#dbeafe,stroke:#2563eb,color:#17324d
  classDef core fill:#dcfce7,stroke:#15803d,color:#14532d
  classDef ai fill:#f3e8ff,stroke:#9333ea,color:#581c87
  classDef data fill:#ffedd5,stroke:#ea580c,color:#7c2d12
  class UI,Proxy,External,API,Web,Adapter,Origin ui
  class Gateway,Config,Tools,Lead,Callback,Worker,Summary,Notify,Seal core
  class Agent,Model,RAG,Ingest,Embedding ai
  class Budget core
  class Remote ai
  class DB,State,Reads data
```


## Responsibilities and workflow

1. Next.js presents Chat, Conversations, Leads and Knowledge base using a same-origin REST proxy.
2. Web/mock adapters normalize their own payloads. The gateway derives trusted business/session/sender ownership, serializes turns and routes replies back to their actual origin.
3. The shared agent combines file-authoritative business instructions, persistent customer details/history, retrieval and allowed tools. Changes require original customer evidence; course order/discussion focus supports references and corrections.
4. RAG chunks and embeds scoped text/Markdown, retrieves the latest ready compatible document versions and returns sources. Validated exact quotes support policy answers; a bounded focused policy extraction can recover omitted passages.
5. The tool executor enforces the business allowlist and argument/result contracts. Courses/memberships supply current facts. Lead/callback receipts come from actual persisted operations, not model prose.
6. Lead creation stores an event/snapshot. A leased background worker generates a structured counselor summary from actual data and stores a simulated notification. Failures retain the lead and stages avoid duplicate effects.
7. Persistent message identity and immutable prepared replies support safe replay. Unsafe uncertain write windows remain review-required. Inspection APIs expose bounded DTOs; the UI ignores aborted/stale foreign-owner responses.

Mongo holds scoped business records, sessions, turns, lead/events, documents/chunks and execution records. Configuration remains file-authoritative. There is no real external messaging/CRM delivery or payment/enrollment integration. See [extensions](EXTENDING.md), [decisions](DECISIONS.md) and [limitations](LIMITATIONS.md).
