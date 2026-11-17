"""
StudyAgent orchestrator.

Give it one goal. It plans its own sub-questions, calls a real web-search tool
(over MCP) whenever *it* decides it needs to, and decides for itself when it
has enough to write a final, goal-shaped document (study guide, report,
briefing, etc). Every step is written to a JSON trace so the run can be
inspected afterward, and the finished document is also saved as Markdown.

Usage:
    python src/orchestrator.py "Create a study guide on the causes of WWI"
    python src/orchestrator.py --goal-file goal.txt
"""

# Reminder: activate the venv first —
#   cd StudyAgent && source venv/bin/activate
import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from anthropic import Anthropic, APIError
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from .prompts import (CONTINUE_PROMPT, FORCE_FINISH_PROMPT,
                          INITIAL_USER_PROMPT, STEPS_REMAINING_NOTE,
                          SYSTEM_PROMPT)
except ImportError:  # Support `python src/orchestrator.py` as well as imports.
    from prompts import (CONTINUE_PROMPT, FORCE_FINISH_PROMPT,
                         INITIAL_USER_PROMPT, STEPS_REMAINING_NOTE,
                         SYSTEM_PROMPT)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_SERVER_PATH = REPO_ROOT / "src" / "tools" / "search_server.py"
RUNS_DIR = REPO_ROOT / "runs"

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_TOKENS = 16000
# Non-streaming requests are rejected by the SDK when max_tokens is large
# enough that the call could exceed its 10-minute timeout.
MAX_TOKENS_CEILING = 20000
# How many times a final document cut off by the token limit is resumed.
MAX_CONTINUATIONS = 2

# Tolerates stray Markdown ("COVERAGE: **complete** - ...") from the model.
COVERAGE_RE = re.compile(
    r"^\W*COVERAGE\W*?:\W*(complete|partial)\b\W*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


class ConfigError(Exception):
    """A setting in .env or the environment is missing or invalid."""


@dataclass(frozen=True)
class Config:
    model: str = DEFAULT_MODEL
    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = DEFAULT_MAX_TOKENS


def _env(name: str, default: str) -> str:
    """Read STUDYAGENT_<name>, falling back to the older AUTOAGENT_<name>."""
    return (os.environ.get(f"STUDYAGENT_{name}")
            or os.environ.get(f"AUTOAGENT_{name}")
            or default)


def _int_setting(name: str, default: int, low: int, high: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"STUDYAGENT_{name} must be a whole number, got {raw!r}.")
    if not low <= value <= high:
        raise ConfigError(
            f"STUDYAGENT_{name} must be between {low} and {high}, got {value}.")
    return value


def load_config() -> Config:
    return Config(
        model=_env("MODEL", DEFAULT_MODEL),
        max_steps=_int_setting("MAX_STEPS", DEFAULT_MAX_STEPS, 1, 50),
        max_tokens=_int_setting("MAX_TOKENS", DEFAULT_MAX_TOKENS, 1000,
                                MAX_TOKENS_CEILING),
    )


def parse_coverage(text: str):
    """Extract the model's self-reported COVERAGE line, or None if absent."""
    matches = list(COVERAGE_RE.finditer(text or ""))
    if not matches:
        return None
    last = matches[-1]
    return {"level": last.group(1).lower(), "note": last.group(2).strip()}


def mcp_tool_to_anthropic_schema(mcp_tool) -> dict:
    """Convert an MCP tool definition into the shape Anthropic's API expects."""
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description or "",
        "input_schema": mcp_tool.inputSchema,
    }


class TraceLogger:
    """Writes every step of the run to a JSON file for later inspection."""

    def __init__(self, goal: str, config: Config, runs_dir: Path = RUNS_DIR):
        runs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = runs_dir / f"run_{timestamp}.json"
        self.document_path = None
        self.data = {
            "goal": goal,
            "model": config.model,
            "max_steps": config.max_steps,
            "started_at": timestamp,
            "steps": [],
            "final_output": None,
            "stopped_reason": None,
            "coverage": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self._flush()

    @property
    def finished(self) -> bool:
        return self.data["stopped_reason"] is not None

    def log_step(self, step_number: int, kind: str, content):
        self.data["steps"].append(
            {
                "step": step_number,
                # "reasoning" | "tool_call" | "tool_result" | "warning"
                "kind": kind,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": content,
            }
        )
        self._flush()

    def add_usage(self, usage):
        for key in ("input_tokens", "output_tokens"):
            self.data["usage"][key] += getattr(usage, key, 0) or 0

    def finish(self, final_output: str, stopped_reason: str):
        self.data["final_output"] = final_output
        self.data["stopped_reason"] = stopped_reason
        self.data["coverage"] = parse_coverage(final_output)
        self._flush()
        # Save real documents (not error notices) as Markdown next to the trace.
        if final_output and stopped_reason in ("self_terminated", "forced_finish"):
            self.document_path = self.path.with_suffix(".md")
            self.document_path.write_text(final_output + "\n", encoding="utf-8")

    def _flush(self):
        # Write to a temp file and swap it in, so the dashboard never reads a
        # half-written trace while a run is in progress.
        temp = self.path.with_suffix(".json.tmp")
        payload = json.dumps(self.data, indent=2)
        try:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            # e.g. Windows refusing to replace a file another program has open.
            self.path.write_text(payload, encoding="utf-8")


def _text_of(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text")


async def run_loop(client, call_tool, tools_schema, goal: str,
                   tracer: TraceLogger, config: Config) -> str:
    """The agent loop. `call_tool(name, arguments)` is an async callable that
    returns the tool's result as text; it is injected so tests can fake it."""
    max_steps = config.max_steps

    def ask(messages, allow_tools: bool):
        kwargs = dict(model=config.model, max_tokens=config.max_tokens,
                      system=SYSTEM_PROMPT, messages=messages)
        if tools_schema:
            # Keep the tool definitions on every call (earlier turns reference
            # them) and switch tool use off when the model has to write.
            kwargs["tools"] = tools_schema
            if not allow_tools:
                kwargs["tool_choice"] = {"type": "none"}
        response = client.messages.create(**kwargs)
        tracer.add_usage(getattr(response, "usage", None))
        return response

    first_message = INITIAL_USER_PROMPT.format(
        goal=goal,
        today=date.today().strftime("%A, %B %d, %Y"),
        max_steps=max_steps,
        research_steps=max_steps - 1,
    )
    if max_steps == 1:
        first_message += "\n\n" + FORCE_FINISH_PROMPT
    messages = [{"role": "user", "content": first_message}]

    for step in range(1, max_steps + 1):
        forced_finish = step == max_steps

        print(f"\n[step {step}/{max_steps}] calling Claude "
              f"({'forced finish' if forced_finish else 'reasoning'})...")
        try:
            response = ask(messages, allow_tools=not forced_finish)
            text = _text_of(response)
            if text:
                print(f"[step {step}] Claude: {text[:300]}"
                      f"{'...' if len(text) > 300 else ''}")
                tracer.log_step(step, "reasoning", text)

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_use_blocks:
                # No tool call this turn = Claude has decided it's done. If the
                # document ran into the token limit, ask it to pick up where it
                # stopped instead of saving a guide that ends mid-sentence.
                final_text = text
                continuations = 0
                while (response.stop_reason == "max_tokens" and text
                       and continuations < MAX_CONTINUATIONS):
                    continuations += 1
                    print(f"[step {step}] document hit the token limit; "
                          f"continuing ({continuations}/{MAX_CONTINUATIONS})...")
                    tracer.log_step(step, "warning",
                                    "final document hit max_tokens; continuing")
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": CONTINUE_PROMPT})
                    response = ask(messages, allow_tools=False)
                    text = _text_of(response)
                    if text:
                        tracer.log_step(step, "reasoning", text)
                    final_text += text
                if response.stop_reason == "max_tokens":
                    print("[warning] the document is still cut off. Raise "
                          "STUDYAGENT_MAX_TOKENS or narrow the goal.")
                    tracer.log_step(step, "warning",
                                    "final document truncated at max_tokens")

                tracer.finish(
                    final_text,
                    "forced_finish" if forced_finish else "self_terminated",
                )
                print(f"\n[done] agent finished on step {step} "
                      f"({'forced' if forced_finish else 'self-decided'}).")
                return final_text
        except APIError as e:
            # Record what happened and stop cleanly instead of crashing mid run;
            # everything found so far is already in the trace.
            print(f"\n[error] Anthropic API call failed on step {step}: {e}")
            output = (
                f"[Run stopped on step {step} due to an Anthropic API error: {e}]\n"
                f"Anything found before this point is in the trace log."
            )
            tracer.finish(output, "api_error")
            return output

        if response.stop_reason == "max_tokens":
            print(f"[warning] step {step} was truncated by the token limit "
                  f"while requesting searches.")
            tracer.log_step(step, "warning", "response truncated: hit max_tokens")

        # Claude decided it needs a tool. Append its request to history,
        # execute the tool via MCP, and feed the result back as the next
        # turn — this is the self-prompting step: Claude's own output plus
        # fresh data becomes the input to its next decision.
        messages.append({"role": "assistant", "content": response.content})

        next_turn = []
        for tool_call in tool_use_blocks:
            print(f"[step {step}] tool call: {tool_call.name}"
                  f"({json.dumps(tool_call.input)})")
            tracer.log_step(
                step, "tool_call",
                {"name": tool_call.name, "input": tool_call.input},
            )
            try:
                result_text = await call_tool(tool_call.name, tool_call.input)
            except Exception as e:  # One failed search shouldn't end the run.
                result_text = (f"Tool call failed: {type(e).__name__}: {e}. "
                               f"Try a different query or note the gap.")
            tracer.log_step(step, "tool_result", result_text)
            next_turn.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": result_text or "(empty result)",
                }
            )

        # Tell the agent where it is in its budget; on the last research step
        # this is the instruction to stop searching and write.
        remaining = max_steps - step
        next_turn.append({
            "type": "text",
            "text": (FORCE_FINISH_PROMPT if remaining == 1
                     else STEPS_REMAINING_NOTE.format(remaining=remaining)),
        })
        messages.append({"role": "user", "content": next_turn})

    # Unreachable in practice: tool use is disabled on the last iteration, so
    # it always returns above. Guard anyway.
    tracer.finish("", "max_steps_exhausted_without_response")
    return ""


async def run_agent(goal: str, tracer: TraceLogger, config: Config) -> str:
    client = Anthropic()  # reads ANTHROPIC_API_KEY from env

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(SEARCH_SERVER_PATH)],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = (await session.list_tools()).tools
            tools_schema = [mcp_tool_to_anthropic_schema(t) for t in mcp_tools]
            print(f"[setup] connected to MCP server, tools available: "
                  f"{[t.name for t in mcp_tools]}")

            async def call_tool(name, arguments):
                result = await session.call_tool(name, arguments)
                return "\n".join(
                    c.text for c in result.content if hasattr(c, "text")
                )

            return await run_loop(client, call_tool, tools_schema, goal,
                                  tracer, config)


def read_goal(argv) -> str:
    parser = argparse.ArgumentParser(
        description="Research a goal and write a study guide, report, or briefing.")
    parser.add_argument("goal", nargs="*", help="The goal, in plain English.")
    parser.add_argument("--goal-file", type=Path,
                        help="Read the goal from a text file (handy for long, "
                             "multi-paragraph goals). Use - for standard input.")
    args = parser.parse_args(argv)
    if args.goal_file is not None:
        if str(args.goal_file) == "-":
            goal = sys.stdin.read()
        else:
            goal = args.goal_file.read_text(encoding="utf-8")
    else:
        goal = " ".join(args.goal)
    goal = goal.strip()
    if not goal:
        parser.error('give a goal, e.g. python src/orchestrator.py "your goal here"')
    return goal


def main():
    # Guides are full of arrows, dashes, and non-English names; don't let a
    # legacy Windows console encoding crash the run while printing them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    goal = read_goal(sys.argv[1:])
    load_dotenv()

    try:
        config = load_config()
        for key in ("ANTHROPIC_API_KEY", "TAVILY_API_KEY"):
            if not os.environ.get(key):
                raise ConfigError(f"{key} is not set. Copy .env.example to .env "
                                  f"and add your key.")
    except ConfigError as e:
        sys.exit(f"[config] {e}")

    print(f"[goal] {goal}")
    print(f"[config] model={config.model} max_steps={config.max_steps} "
          f"max_tokens={config.max_tokens}\n")

    tracer = TraceLogger(goal, config)
    start = time.time()
    try:
        final_output = asyncio.run(run_agent(goal, tracer, config))
    except KeyboardInterrupt:
        if not tracer.finished:
            tracer.finish("", "user_interrupted")
        print("\n\n[interrupted] Run cancelled by user (Ctrl+C).")
        print(f"[interrupted] Partial trace saved to {tracer.path}")
        sys.exit(130)
    except Exception as e:
        # e.g. the search server failed to start. Close out the trace so it
        # doesn't sit in the dashboard as "in_progress" forever.
        if not tracer.finished:
            tracer.finish(f"[Run failed: {type(e).__name__}: {e}]", "error")
        print(f"\n[error] run failed: {type(e).__name__}: {e}")
        print(f"[error] trace saved to {tracer.path}")
        sys.exit(1)
    elapsed = time.time() - start

    print("\n" + "=" * 70)
    print("FINAL OUTPUT")
    print("=" * 70)
    print(final_output)
    print("=" * 70)
    usage = tracer.data["usage"]
    print(f"\n[done] took {elapsed:.1f}s, {usage['input_tokens']:,} input / "
          f"{usage['output_tokens']:,} output tokens")
    if tracer.data["coverage"]:
        print(f"[done] self-reported coverage: {tracer.data['coverage']['level']}")
    if tracer.document_path:
        print(f"[done] document: {tracer.document_path}")
    print(f"[done] trace:    {tracer.path}")


if __name__ == "__main__":
    main()
