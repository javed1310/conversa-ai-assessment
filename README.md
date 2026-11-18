# Conversa - AI customer engagement assessment

Python/FastAPI backend, Next.js/TypeScript frontend, MongoDB and Gemini. The training institute and fitness studio share an agent, RAG pipeline and messaging gateway, with business-scoped knowledge/tools. This repository contains the assessment application and its setup, API, architecture and testing documentation.

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

## Run the checks

```powershell
powershell -NoProfile -File scripts/check.ps1
```

Or use the installed backend Python: `python scripts/check.py`. MongoDB is
required. The default checks need no model key: they use controlled model
responses, real MongoDB integration, frontend API checks and rendered React tests.
See [test scenarios and expected/actual results](docs/TESTING.md).

## Documentation

- [API examples](docs/API_EXAMPLES.md): complete enquiry-to-callback flow and mock channel.
- [Architecture](docs/ARCHITECTURE.md): agent, retrieval, tools, gateway, state and automation.
- [Add a business, tool or channel](docs/EXTENDING.md).
- [Technical decisions](docs/DECISIONS.md).
- [Limitations and incomplete work](docs/LIMITATIONS.md).
- [Portable setup](docs/SETUP.md).
- [OpenAPI and domain schemas](docs/contracts).

The application is a local assessment prototype. External messaging and CRM
notifications are simulated; no payment or direct enrolment is implemented.
Current prices and availability come from tools, while policy answers cite
business documents. A callback request is confirmed only from a saved tool result.

The full development dependency audit retains 12 high affected-package entries
for an upstream unpatched advisory; the production-only audit reports zero.
See the limitations before exposing this trusted local workspace publicly.
