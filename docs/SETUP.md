# Setup outside Windows

Use Python 3.12, Node 24 and MongoDB 8. These are the declared supported runtimes;
the version bounds have not been widened without compatibility testing.

Run the following from a clone's root on Linux/macOS. Install the pinned uv version
in an environment where `python3.12` and `uv` are available on PATH.

```sh
python3.12 -m pip install uv==0.13.8
uv sync --project backend --python python3.12 --locked --all-groups
npm --prefix frontend ci
test -f .env || cp .env.example .env
docker compose -f compose.yaml up -d --wait --wait-timeout 60 mongo
uv --directory backend run --locked python -m app.seed
```

Privately configure the root `.env` using `.env.example`. Seed loads fictional
catalogue/configuration records; knowledge ingestion additionally requires an actual
Gemini key and available embedding quota.

```sh
uv --directory backend run --locked python -m app.knowledge_cli ingest --version 2
uv --directory backend run --locked python -m app
```

In a second terminal, from the root:

```sh
npm --prefix frontend run dev
```

Open http://localhost:3000. The backend binds to loopback on port 4000. A native
MongoDB instance on port 27017 can replace Docker Compose. No application Dockerfile
or publicly exposed deployment is needed for the assessment.

## Portable deterministic verification

```sh
backend/.venv/bin/python scripts/check.py
```

For the existing Windows environment:

```powershell
backend/.venv-system/Scripts/python.exe scripts/check.py
```

The runner uses its own Python interpreter and discovers the platform's npm executable.
It performs the same backend lint/format/types/tests/contracts and frontend
lint/types/API checks/build as `check.ps1`. MongoDB must already be running.
`--skip-frontend-build` and `--backend-only` support explicitly scoped checks.
No private key, live API call, dependency installation or service startup is performed.

## Qualification boundary

The original host qualification is Windows. These portable commands and runner are
provided for reviewers; Linux/macOS execution is not claimed until actually run there.
The browser integration harness still uses Windows browser/process paths. The portable
runner does not pretend to execute that optional browser harness on other systems.

Default backend tests use controlled model substitutes, with some real MongoDB integration
tests. They verify application invariants, not the reasoning of a live model. Frontend
API checks are separate from the rendered component tests. Live model and real-browser evaluations are
separate evidence categories.

The working-directory distinction follows the [uv CLI reference](https://docs.astral.sh/uv/reference/cli/): `--directory` changes where module commands execute; `--project` selects project metadata/environment.
