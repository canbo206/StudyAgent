# AutoAgent

A self-prompting research agent. Give it one goal in plain English —
`"create a study guide on the causes of WWI"` or
`"summarize competitor pricing for standing desks"` — and it plans its own
sub-questions, decides for itself when it needs to search the web, and
decides for itself when it has enough to produce a final, goal-shaped
document (report, study guide, briefing, etc). No human input after the
initial goal.

## How it's self-prompting

After the first turn, no human writes any further instructions. The
orchestrator takes Claude's own prior output and any fresh tool results and
feeds them back in as the next turn's context — Claude is deciding its own
next action (search again, or finish) based on its own accumulated
reasoning, not a script. The Python loop in `src/orchestrator.py` provides
the *structure* (when to call the model, when to call a tool, when to force
a stop) — it does not tell Claude *what* to do at each step. That decision
is Claude's, every turn.

## Architecture

```
 goal (one human input)
        │
        ▼
┌────────────────────┐        ┌──────────────────────┐
│ orchestrator.py      │◄─────►│ Claude API             │
│  - owns the loop      │       │  - decides: search or  │
│  - enforces max steps │       │    finish              │
│  - logs every step     │      │  - infers output shape │
└──────────┬───────────┘       └──────────────────────┘
           │ (MCP protocol, stdio)
           ▼
┌────────────────────┐
│ search_server.py     │  MCP tool server exposing web_search,
│  (Tavily API)         │  backed by a real search API
└────────────────────┘
           │
           ▼
   runs/run_<timestamp>.json   →  open in trace_viewer.html
```

## Setup (One time)

1. **Get an Anthropic API key** — console.anthropic.com. This is pay-per-use,
   not a subscription; see cost notes below.
2. **Get a free Tavily API key** — tavily.com (free tier: 1,000 searches/month).
3. **Create a virtual environment** (isolates this project's packages from
   the rest of your system — do this once, from the project root):
   ```bash
      python3 -m venv venv
   ```
4. **Activate it:**
   ```bash
      source venv/bin/activate
   ```

5. Install dependencies (with venv active):
   ```bash
      pip install -r requirements.txt
   ```
6. Copy the env template and fill in your keys:
   ```bash
      cp .env.example .env
   # edit .env with your real ANTHROPIC_API_KEY and TAVILY_API_KEY
   ```

## Run it

**Every time you open a new terminal to work on this project**, you need to
re-activate the virtual environment first — it doesn't stay active across
terminal sessions:
 
```bash
   cd path/to/Studyagent      # the project root
   source venv/bin/activate  # look for (venv) to appear in your prompt
```

Then move into `src/` and run it, passing your goal as a quoted argument:
 
```bash
   cd src
   python3 orchestrator.py "Create a study guide on the causes of WWI"
```

Watch the terminal as it prints each reasoning step and tool call live. When
it finishes, the full trace is saved to `runs/run_<timestamp>.json`. Open
`trace_viewer.html` in a browser (just double-click it in Finder/Explorer)
and use the file picker to load that JSON file to step through the agent's
reasoning visually, with headers, bold text, and lists rendered properly
rather than as raw markdown symbols.
 
**When you're done working on the project**, you can deactivate the venv
(optional, just for cleanliness):
```bash
   deactivate
```

### Keeping costs low while developing

The default model in `.env.example` is `claude-haiku-4-5-20251001` cheap
enough that a full test run costs a fraction of a cent. Switch
`AUTOAGENT_MODEL` to `claude-sonnet-5` only when you want higher-quality
output (e.g. recording a demo). `AUTOAGENT_MAX_STEPS` hard-caps how many
reasoning/tool-call iterations a single run can take, so a bug can't run up
an unexpected bill.

There is no ongoing charge for this project existing you're billed only
for the tokens used during an actual run. See `console.anthropic.com` to set
a spend limit.


## Project structure

```
autoagent/
├── README.md
├── requirements.txt
├── .env.example
├── src/
│   ├── orchestrator.py     # the self-prompting loop
│   ├── prompts.py          # all prompt text, separated from logic
│   └── tools/
│       └── search_server.py  # MCP server exposing web_search (Tavily)
├── runs/                   # JSON trace logs, one per run (gitignored)
└── trace_viewer.html       # open in a browser to inspect a trace log
```

## Optional run-history dashboard

A small **Flask** app now browses existing JSON traces using **Bootstrap** tables,
status badges, buttons, and responsive layout. It never calls Claude or Tavily,
and needs no API keys. Your original agent and `trace_viewer.html` still work.

```bash
bash scripts/dev.sh setup
bash scripts/dev.sh test
bash scripts/dev.sh serve
# Second terminal:
bash scripts/dev.sh health
```

Open `http://127.0.0.1:5000`. Click a goal to inspect the final answer and trace
entries or download its JSON. Invalid/in-progress JSON is skipped in the list.
Bootstrap CSS comes from its CDN with an integrity hash; reading the trace still
works without the stylesheet if you are offline. Trace text is escaped as text.
The dashboard is local and read-only; do not expose the development server online.

PowerShell equivalents:

```powershell
./scripts/dev.ps1 -Action setup
./scripts/dev.ps1 -Action test
./scripts/dev.ps1 -Action serve -Port 5000
# Second terminal:
./scripts/dev.ps1 -Action health -Port 5000
```

The dashboard setup installs `requirements-dev.txt` (web/cloud adapters and their
tests). Install the original `requirements.txt` separately to run the research
agent. For only the dashboard, `pip install -r requirements-web.txt` suffices.

Small skills statement: **Wrote Bash and PowerShell scripts to automate project
setup, tests, local startup, and HTTP health checks.** `scripts/dev.sh` uses
variables, quoting, exit codes, and branches; `scripts/dev.ps1` uses typed
parameters, functions, exceptions, and `Invoke-RestMethod`. The YAML workflow in
`.github/workflows/checks.yml` configures Linux/Windows tests when pushed.

## Optional cloud trace export

`src/archive_run.py` validates one saved trace and exports it only when explicitly
invoked. **AWS S3** and **Azure Blob Storage** store the full JSON file; **Firebase
Cloud Firestore** stores bounded metadata and a final-answer preview (not the full
trace). A content hash identifies each run, so retries use the same destination.
No cloud resource is provisioned and no upload occurs during dashboard use, agent
runs, setup, or tests. These are SDK integrations, not hosted deployments.

Install `requirements-cloud.txt` if you did not run setup. Replace the sample
trace path and destination names below. Try `--dry-run` first: it validates and
prints a destination without loading cloud credentials or contacting a provider.
Removing that flag uploads the trace or summary and may incur provider charges.

```bash
python src/archive_run.py runs/run_YOUR_TIMESTAMP.json --provider aws --destination YOUR_PRIVATE_BUCKET --dry-run
python src/archive_run.py runs/run_YOUR_TIMESTAMP.json --provider azure --destination YOUR_PRIVATE_CONTAINER --account-url https://YOUR_ACCOUNT.blob.core.windows.net --dry-run
python src/archive_run.py runs/run_YOUR_TIMESTAMP.json --provider firebase --destination research_runs --dry-run
```

- **AWS:** use an existing private S3 bucket and an AWS CLI profile/SSO session
  or IAM role. Boto3 uses its default credential chain. The identity needs
  `s3:PutObject` on the bucket's `runs/*` prefix (and any relevant KMS permission).
- **Azure:** use an existing storage account and private container. Sign in with
  `az login` for local development; `DefaultAzureCredential` can use that identity.
  Assign an appropriate role such as Storage Blob Data Contributor on the target
  container. The command writes blobs without an account key in source code.
- **Firebase:** create a Firestore database and configure Application Default
  Credentials with permission to write documents. For a local experiment,
  `gcloud auth application-default login` and a configured Google Cloud project
  can supply credentials. The Firebase Admin SDK is a privileged server client;
  Firestore client security rules do not restrict its access. Use a limited IAM
  identity and keep credential files outside this repository.

Before a real export, inspect the trace for research content you want to keep
local. Upload commands send the full trace to S3/Azure and only the displayed
summary fields to Firestore. Mocked SDK tests verify request construction; a live
upload/download is still needed to verify your account, permissions, and region.

Learn: [Flask](https://flask.palletsprojects.com/en/stable/quickstart/),
[Bootstrap](https://getbootstrap.com/docs/5.3/getting-started/introduction/),
[AWS S3 with Python](https://docs.aws.amazon.com/boto3/latest/guide/s3-uploading-files.html),
[Azure Blob Storage with Python](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-quickstart-blobs-python),
[Firebase Firestore](https://firebase.google.com/docs/firestore/manage-data/add-data),
[Firebase Admin setup](https://firebase.google.com/docs/admin/setup).

**Django remains a future option**, not an implemented skill in this project.
It would fit a larger multi-user research library with authentication, a database,
and an admin interface. [Start the Django tutorial](https://docs.djangoproject.com/en/5.2/intro/tutorial01/)
if you decide to build that feature; adding a third web framework now would not
help the current workflows.
