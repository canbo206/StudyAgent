# StudyAgent

StudyAgent is a Python research agent that turns a plain-English goal into a study guide, report, or briefing. It uses Claude to choose searches and write an answer, with Tavily web search exposed through a local Model Context Protocol (MCP) server. Each run saves a JSON trace for later inspection.

## How it works

1. You supply a goal. The prompt tells Claude today's date and its step budget, and asks it to list sub-questions and run its first searches.
2. The orchestrator sends requested searches to the MCP server, then adds the results, plus a note on how many steps remain, to the conversation for the next model call. Several searches in one response cost one step.
3. A response without a tool call becomes the final document. After the last research step, the orchestrator disables tool use and asks Claude to write with what it has.
4. If the document is cut off by the output-token cap, the orchestrator asks Claude to continue from where it stopped (up to two times) and joins the pieces.
5. Model text, tool calls, results, token usage, and the final output are saved in `runs/run_<UTC timestamp>.json`. The finished document is also saved next to it as `run_<UTC timestamp>.md`.

The prompts request citations and a closing `COVERAGE: complete|partial` line, which is parsed into the trace's `coverage` field; these are model-generated, not independently verified. Trace entries labeled `reasoning` contain visible response text, not private model reasoning.

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

For a long goal (a pasted assignment, job description, or interview invite), save it to a text file and pass `--goal-file goal.txt` instead of quoting it on the command line.

On Windows PowerShell, create the environment with `python -m venv venv`, activate it with `./venv/Scripts/Activate.ps1`, and copy the template with `Copy-Item .env.example .env`. The install and run commands are the same.

Reactivate the environment in each new terminal. Use `deactivate` when finished.

Optional settings in `.env` (the original `AUTOAGENT_MODEL` and `AUTOAGENT_MAX_STEPS` names are still read as fallbacks):

- `STUDYAGENT_MODEL`: defaults to `claude-haiku-4-5-20251001`.
- `STUDYAGENT_MAX_STEPS`: defaults to `8`; 1 to 50. Includes the final writing step.
- `STUDYAGENT_MAX_TOKENS`: output cap per model response; defaults to `16000`, 1000 to 20000.
- `STUDYAGENT_SNIPPET_CHARS`: characters of page content kept per search result; defaults to `1000`.
- `TAVILY_SEARCH_DEPTH`: defaults to `advanced`; can be set to `basic`.

Invalid values stop the run with a one-line message before any API call. The step cap bounds the loop, but is not a dollar budget or a limit on individual searches: one response can request multiple searches. Each run prints its total input and output tokens and records them in the trace. API usage may incur charges.

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
bash scripts/dev.sh setup   # installs agent + dashboard + cloud dependencies
bash scripts/dev.sh test
bash scripts/dev.sh serve
# In a second terminal while the server is running:
bash scripts/dev.sh health
```

PowerShell equivalents use `./scripts/dev.ps1 -Action setup` (or `test`, `serve`, `health`).

The unittest suite covers the agent loop (with a scripted fake model and search tool), the dashboard, trace validation, and mocked cloud exports. GitHub Actions is configured to run it on Linux and Windows with Python 3.12. It does not test live Claude/Tavily calls or real cloud uploads.

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
- `runs/` — generated traces and documents, excluded from Git.

Licensed under the [MIT License](LICENSE).
