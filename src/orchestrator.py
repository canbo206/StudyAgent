"""
AutoAgent orchestrator.

Give it one goal. It plans its own sub-steps, calls a real web-search tool
(over MCP) whenever *it* decides it needs to, and decides for itself when
it has enough to produce a final, goal-shaped document (report, study
guide, briefing, etc). Every step is written to a JSON trace log so the
whole reasoning process is inspectable afterward.

Usage:
    python src/orchestrator.py "Create a study guide on the causes of WWI"
"""

"""REMEMBER TO cd StudyAgent && source venv/bin/activate before do orchestrator.py"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic, APIError
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from prompts import FORCE_FINISH_PROMPT, INITIAL_USER_PROMPT, SYSTEM_PROMPT

load_dotenv()

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add "
        "your key from console.anthropic.com. WILL NOT start without it."
    )

MODEL = os.environ.get("AUTOAGENT_MODEL", "claude-haiku-4-5-20251001")
MAX_STEPS = int(os.environ.get("AUTOAGENT_MAX_STEPS", "8"))
MAX_TOKENS = 5000

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_SERVER_PATH = REPO_ROOT / "src" / "tools" / "search_server.py"
RUNS_DIR = REPO_ROOT / "runs"


def mcp_tool_to_anthropic_schema(mcp_tool) -> dict:
    """Convert an MCP tool definition into the shape Anthropic's API expects."""
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description or "",
        "input_schema": mcp_tool.inputSchema,
    }


class TraceLogger:
    """Writes every step of the run to a JSON file for later inspection."""

    def __init__(self, goal: str):
        RUNS_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = RUNS_DIR / f"run_{timestamp}.json"
        self.data = {
            "goal": goal,
            "model": MODEL,
            "max_steps": MAX_STEPS,
            "started_at": timestamp,
            "steps": [],
            "final_output": None,
            "stopped_reason": None,
        }
        self._flush()

    def log_step(self, step_number: int, kind: str, content):
        self.data["steps"].append(
            {
                "step": step_number,
                "kind": kind,  # "reasoning" | "tool_call" | "tool_result" | "final"
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": content,
            }
        )
        self._flush()

    def finish(self, final_output: str, stopped_reason: str):
        self.data["final_output"] = final_output
        self.data["stopped_reason"] = stopped_reason
        self._flush()

    def _flush(self):
        self.path.write_text(json.dumps(self.data, indent=2))


async def run_agent(goal: str) -> str:
    client = Anthropic()  # reads ANTHROPIC_API_KEY from env
    tracer = TraceLogger(goal)

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

            messages = [
                {"role": "user", "content": INITIAL_USER_PROMPT.format(goal=goal)}
            ]

            for step in range(1, MAX_STEPS + 1):
                forced_finish = step == MAX_STEPS
                active_tools = [] if forced_finish else tools_schema

                if forced_finish:
                    messages.append({"role": "user", "content": FORCE_FINISH_PROMPT})

                print(f"\n[step {step}/{MAX_STEPS}] calling Claude "
                      f"({'forced finish' if forced_finish else 'reasoning'})...")
                try:
                    response = client.messages.create(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=SYSTEM_PROMPT,
                        tools=active_tools if active_tools else None,
                        messages=messages,
                    )
                except APIError as e:
                    # Don't lose everything found and record what you
                    # have and stop cleanly instead of crashing mid run.
                    print(f"\n[error] Anthropic API call failed on step "
                          f"{step}: {e}")
                    partial_output = (
                        f"[Run stopped early due to an API error: {e}]\n"
                        f"Partial progress was made before this point:"
                        f"see the trace log for what was found."
                    )
                    tracer.finish(partial_output, "api_error")
                    return partial_output

                # Log whatever text reasoning Claude produced this turn
                text_parts = [b.text for b in response.content if b.type == "text"]
                if text_parts:
                    reasoning_text = "\n".join(text_parts)
                    print(f"[step {step}] Claude: {reasoning_text[:300]}"
                          f"{'...' if len(reasoning_text) > 300 else ''}")
                    tracer.log_step(step, "reasoning", reasoning_text)

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                if not tool_use_blocks:
                    # No tool call this turn = Claude has decided it's done.
                    final_text = "\n".join(text_parts) if text_parts else ""
                    tracer.finish(
                        final_text,
                        "forced_finish" if forced_finish else "self_terminated",
                    )
                    print(f"\n[done] agent finished on step {step} "
                          f"({'forced' if forced_finish else 'self-decided'}).")
                    return final_text

                # Claude decided it needs a tool. Append its request to history,
                # execute the tool via MCP, and feed the result back as the
                # next turn — this is the self-prompting step: Claude's own
                # output plus fresh data becomes the input to its next decision.
                messages.append({"role": "assistant", "content": response.content})

                tool_result_blocks = []
                for tool_call in tool_use_blocks:
                    print(f"[step {step}] tool call: {tool_call.name}"
                          f"({json.dumps(tool_call.input)})")
                    tracer.log_step(
                        step, "tool_call",
                        {"name": tool_call.name, "input": tool_call.input},
                    )

                    mcp_result = await session.call_tool(
                        tool_call.name, tool_call.input
                    )
                    result_text = "\n".join(
                        c.text for c in mcp_result.content if hasattr(c, "text")
                    )

                    tracer.log_step(step, "tool_result", result_text)

                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call.id,
                            "content": result_text,
                        }
                    )

                messages.append({"role": "user", "content": tool_result_blocks})

    # Should be unreachable (forced_finish on the last iteration always returns),
    # but guard anyway.
    tracer.finish("", "max_steps_exhausted_without_response")
    return ""


def main():
    if len(sys.argv) < 2:
        print('Usage: python src/orchestrator.py "your goal here"')
        sys.exit(1)

    goal = " ".join(sys.argv[1:])
    print(f"[goal] {goal}")
    print(f"[config] model={MODEL} max_steps={MAX_STEPS}\n")

    start = time.time()
    final_output = asyncio.run(run_agent(goal))
    elapsed = time.time() - start

    print("\n" + "=" * 70)
    print("FINAL OUTPUT")
    print("=" * 70)
    print(final_output)
    print("=" * 70)
    print(f"\n[done] took {elapsed:.1f}s — see runs/ for the full trace log")


if __name__ == "__main__":
    main()
