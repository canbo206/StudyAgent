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

## Setup

1. **Get an Anthropic API key** — console.anthropic.com. This is pay-per-use,
   not a subscription; see cost notes below.
2. **Get a free Tavily API key** — tavily.com (free tier: 1,000 searches/month).
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy the env template and fill in your keys:
   ```bash
   cp .env.example .env
   # edit .env with your real ANTHROPIC_API_KEY and TAVILY_API_KEY
   ```

## Run it

```bash
cd src
python orchestrator.py "Create a study guide on the causes of WWI"
```

Watch the terminal as it prints each reasoning step and tool call live. When
it finishes, the full trace is saved to `runs/run_<timestamp>.json`. Find in files and open `trace_viewer.html` in a browser, load that file to step through the
agent's reasoning visually.

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
