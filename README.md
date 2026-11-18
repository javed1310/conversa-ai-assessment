# Conversa - AI customer engagement assessment

Python/FastAPI backend, Next.js/TypeScript frontend, MongoDB and Gemini. The training institute and fitness studio share an agent, RAG pipeline and messaging gateway, with business-scoped knowledge/tools. This directory is the clean submission snapshot; development logs, old plans and private credentials are excluded.

## Local setup

Prerequisites: Python 3.12, Node 24.x/npm, MongoDB 8 (native or Docker), and a Gemini API key with generation/embedding quota. PowerShell commands below start from this repository root.

```powershell
python -m pip install --target .tools/uv --no-warn-script-location uv==0.13.8
powershell -NoProfile -File scripts/uv.ps1 sync --locked --all-groups
npm.cmd --prefix frontend ci --cache .cache/npm --no-fund
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
docker compose -f compose.yaml up -d --wait --wait-timeout 60 mongo
powershell -NoProfile -File scripts/uv.ps1 run python -m app.seed
```

Native Mongo on `127.0.0.1:27017` can replace the Compose command. Configure root `.env` privately before ingestion/chat:

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Reviewer-provided backend-only key; never a browser/public variable |
| `GEMINI_CHAT_MODEL`, `GEMINI_SUMMARY_MODEL` | Configured generation models |
| `GEMINI_EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | Retrieval embedding identity/dimensions |
| `MONGODB_URI` | Persistent database; default local `ai_engagement` |
| `RAG_MIN_SCORE` | Supplied-corpus cutoff, preconfigured in the example; recalibrate for a changed corpus |
| `MOCK_CHANNEL_KEYS` | Private JSON mapping from business IDs to distinct 32-128 character ASCII secrets; `{}` disables mock ingress |
| `AUTOMATION_ENABLED` | Automatic summary/notification worker |
| `API_PORT`, `WEB_ORIGIN` | Backend and allowed frontend origin |

The complete defaults and bounded retry/worker settings are in [.env.example](.env.example). Optional frontend override: copy `frontend/.env.example` to `frontend/.env.local` and set `ASSESSMENT_API_ORIGIN`; default is `http://127.0.0.1:4000`. Private environment files are excluded from Git.

```powershell
powershell -NoProfile -File scripts/uv.ps1 run python -m app.knowledge_cli ingest --version 2
powershell -NoProfile -File scripts/uv.ps1 run python -m app
```

Second terminal:

```powershell
npm.cmd --prefix frontend run dev
```

Open **http://localhost:3000** and backend OpenAPI UI **http://127.0.0.1:4000/docs**. Start with Learning Path Training Institute, search for data science under INR 30000, ask about the second option, request a callback and provide fictional contact details. Inspect Conversations and Leads, including the eventual counselor summary. Switch to fitness to test membership/policy isolation. Knowledge base accepts text/Markdown; only ready documents are retrievable.

For a local production build: `npm.cmd --prefix frontend run build`, then `npm.cmd --prefix frontend run start`. Stop foreground services with Ctrl+C. Stop Mongo with `docker compose -f compose.yaml stop mongo`; do not delete its volume to restart. Readiness checks Mongo, not model quota. No user/session/lead database export is supplied: seed/ingest create the fictional fixtures.

## Verify

```powershell
powershell -NoProfile -File scripts/check.ps1
```

This runs lint, formatting, strict types, backend tests, generated-contract consistency, frontend API checks and build. Mongo is required; the default suite uses controlled model substitutes and needs no live key. The recorded application gate is **760 backend tests + 26 frontend API checks**. Real Gemini evidence records **10/10 acceptance scenarios**, **7/7 literal conversation/policy checks** and two focused policy-recovery checks. Evidence is a scoped observation, not a guarantee of every future model reply.

See [test commands and expected/actual results](docs/TESTING.md) before running optional live tests: they consume quota and clean up their own fictional databases. No live rerun is needed merely to browse this source.

## Reviewer guide

- [REST examples](docs/API_EXAMPLES.md): full enquiry-to-callback and mock-channel request.
- [Architecture](docs/ARCHITECTURE.md): agent, retrieval, tools, gateway, state and automation.
- [Add a business, tool or channel](docs/EXTENDING.md).
- [Main technical decisions](docs/DECISIONS.md).
- [Known limitations and scope](docs/LIMITATIONS.md).
- [Requirement coverage](docs/REQUIREMENTS.md).
- [Machine-readable API/domain contracts](docs/contracts).

This source has its own clean Git snapshot. No remote publication is performed. The companion `.bundle` is cloneable Git source/history; the ZIP is the same source without `.git`. API keys, installed dependencies, local databases, build outputs and old development history are not part of either handoff.

Dependency advisories discovered during clean-copy verification are disclosed in [LIMITATIONS](docs/LIMITATIONS.md); segregation does not constitute a security-clean or production-readiness claim.
