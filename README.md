# StudyAgent

StudyAgent is a Python research agent that turns a plain-English goal into a study guide, report, or briefing. It uses Claude to choose searches and write an answer, with Tavily web search exposed through a local Model Context Protocol (MCP) server. Each run saves a JSON trace for later inspection.

## How it works

1. You supply a goal. The prompt asks Claude to identify sub-questions and choose its next action.
2. The orchestrator sends requested searches to the MCP server, then adds the results to the conversation for the next model call.
3. A response without a tool call becomes the final answer. On the last allowed iteration, the orchestrator removes tools and asks Claude to finish with the available information.
4. Model text, tool calls, results, and the final output are saved in `runs/run_<UTC timestamp>.json`.

The prompts request citations and a `COVERAGE` assessment; these are model-generated, not independently verified. Trace entries labeled `reasoning` contain visible response text, not private model reasoning.

## Setup and run

From the project root (macOS/Linux):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add your `ANTHROPIC_API_KEY` and `TAVILY_API_KEY` to `.env`, then run:

```bash
python src/orchestrator.py "Create a study guide on the causes of WWI"
```

On Windows PowerShell, create the environment with `python -m venv venv`, activate it with `./venv/Scripts/Activate.ps1`, and copy the template with `Copy-Item .env.example .env`. The install and run commands are the same.

Reactivate the environment in each new terminal. Use `deactivate` when finished.

Configuration retains the original `AUTOAGENT_` names:

- `AUTOAGENT_MODEL`: defaults to `claude-haiku-4-5-20251001`.
- `AUTOAGENT_MAX_STEPS`: defaults to `8`; use a positive integer. Includes the final synthesis iteration.
- `TAVILY_SEARCH_DEPTH`: defaults to `advanced`; can be set to `basic`.

Each model response is capped at 5,000 tokens in the code. The iteration cap bounds the loop, but is not a dollar budget or a limit on individual searches: one response can request multiple tools. API usage may incur charges.

## View saved runs

**Standalone viewer:** open `trace_viewer.html` in a browser and select a JSON file from `runs/`. It shows trace entries and final output as plain text; it does not render Markdown.

**Local dashboard:** browse runs, inspect entries, and download JSON through Flask:

```bash
pip install -r requirements-web.txt
python src/dashboard.py
```

Open `http://127.0.0.1:5000`. The dashboard reads saved files without calling the agent or requiring API keys. Invalid JSON is skipped in the listing; valid unfinished traces appear as `in_progress`. It uses Bootstrap CSS from a CDN and is intended for local use.

## Tests and developer scripts

```bash
bash scripts/dev.sh setup   # installs dashboard + cloud dependencies
bash scripts/dev.sh test
bash scripts/dev.sh serve
# In a second terminal while the server is running:
bash scripts/dev.sh health
```

PowerShell equivalents use `./scripts/dev.ps1 -Action setup` (or `test`, `serve`, `health`). These scripts do not install the research agent's `requirements.txt`.

The unittest suite covers the dashboard, trace validation, and mocked cloud exports. GitHub Actions is configured to run it on Linux and Windows with Python 3.12. It does not test live Claude/Tavily calls or real cloud uploads.

## Optional cloud export

Install `requirements-cloud.txt`, then explicitly export a saved run. S3 and Azure Blob Storage receive the full trace; Firebase Cloud Firestore receives bounded metadata and a final-answer preview. Destinations use a content hash so repeated exports of the same serialized content target the same object or document.

```bash
pip install -r requirements-cloud.txt
python src/archive_run.py runs/run_YOUR_TIMESTAMP.json --provider aws --destination YOUR_BUCKET --dry-run
python src/archive_run.py runs/run_YOUR_TIMESTAMP.json --provider azure --destination YOUR_CONTAINER --account-url https://YOUR_ACCOUNT.blob.core.windows.net --dry-run
python src/archive_run.py runs/run_YOUR_TIMESTAMP.json --provider firebase --destination research_runs --dry-run
```

`--dry-run` validates the trace and prints the target without contacting a provider. Remove it to upload using configured provider credentials: AWS's default credential chain, Azure's `DefaultAzureCredential`, or Firebase's application default credentials. Configure the destination and access beforehand; this project does not provision or host cloud infrastructure. Agent runs and dashboard use never upload automatically.

## Project layout

- `src/orchestrator.py`, `src/prompts.py` — agent loop, trace logging, and prompts.
- `src/tools/search_server.py` — Tavily-backed MCP search tool.
- `src/trace_store.py` — shared trace validation and summaries.
- `src/dashboard.py`, `src/templates/` — Flask run-history dashboard.
- `src/archive_run.py` — optional cloud export CLI.
- `trace_viewer.html` — standalone browser viewer.
- `tests/`, `scripts/`, `.github/workflows/checks.yml` — tests, development commands, and CI.
- `runs/` — generated traces, excluded from Git.

Some internal prompts and UI labels still use the earlier name **AutoAgent**.

Licensed under the [MIT License](LICENSE).
