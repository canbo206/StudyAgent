"""Agent-loop tests with a fake Claude client and a fake search tool.

Nothing here touches the network or needs API keys.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import httpx
from anthropic import APIConnectionError

from src import orchestrator
from src.orchestrator import Config, ConfigError, TraceLogger, parse_coverage, run_loop
from src.prompts import (CONTINUE_PROMPT, FORCE_FINISH_PROMPT,
                         INITIAL_USER_PROMPT, SYSTEM_PROMPT)

TOOLS = [{"name": "web_search", "description": "", "input_schema": {"type": "object"}}]


def text(value):
    return NS(type="text", text=value)


def search(query, call_id="t1"):
    return NS(type="tool_use", id=call_id, name="web_search", input={"query": query})


def reply(*blocks, stop_reason="end_turn"):
    return NS(content=list(blocks), stop_reason=stop_reason,
              usage=NS(input_tokens=100, output_tokens=10))


class FakeClient:
    """Returns scripted responses and records what each request contained."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        # The loop mutates its message list; keep a snapshot of this request.
        self.requests.append(json.loads(json.dumps(kwargs, default=vars)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class LoopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runs = Path(temp.name)
        self.searches = []

    async def fake_tool(self, name, arguments):
        self.searches.append(arguments["query"])
        if arguments["query"] == "boom":
            raise RuntimeError("server died")
        return f"results for {arguments['query']}"

    def run_agent(self, client, max_steps=4, goal="Study graphs"):
        config = Config(model="test-model", max_steps=max_steps, max_tokens=1000)
        self.tracer = TraceLogger(goal, config, self.runs)
        with patch("builtins.print"):
            return asyncio.run(run_loop(client, self.fake_tool, TOOLS, goal,
                                        self.tracer, config))

    def saved(self):
        return json.loads(self.tracer.path.read_text(encoding="utf-8"))

    def test_search_then_finish_saves_trace_and_document(self):
        client = FakeClient(
            reply(text("1. What is BFS?"), search("bfs", "a"), search("dfs", "b"),
                  stop_reason="tool_use"),
            reply(text("# Graphs\n\nBody.\n\nCOVERAGE: complete — all covered")),
        )
        result = self.run_agent(client)
        self.assertTrue(result.startswith("# Graphs"))
        self.assertEqual(self.searches, ["bfs", "dfs"])

        trace = self.saved()
        self.assertEqual(trace["stopped_reason"], "self_terminated")
        self.assertEqual(trace["coverage"], {"level": "complete", "note": "all covered"})
        self.assertEqual(trace["usage"], {"input_tokens": 200, "output_tokens": 20})
        self.assertEqual([s["kind"] for s in trace["steps"]],
                         ["reasoning", "tool_call", "tool_result", "tool_call",
                          "tool_result", "reasoning"])
        self.assertEqual(self.tracer.document_path.read_text(encoding="utf-8"),
                         result + "\n")

        # Tool results go back in one user turn with the remaining-step count.
        followup = client.requests[1]["messages"][-1]["content"]
        self.assertEqual([b["type"] for b in followup],
                         ["tool_result", "tool_result", "text"])
        self.assertEqual(followup[0]["content"], "results for bfs")
        self.assertIn("3 steps remain", followup[-1]["text"])
        self.assertNotIn("tool_choice", client.requests[0])

    def test_last_step_disables_tools_and_forces_the_document(self):
        client = FakeClient(
            reply(search("one"), stop_reason="tool_use"),
            reply(search("two"), stop_reason="tool_use"),
            reply(text("# Guide\n\nCOVERAGE: partial — ran out of steps")),
        )
        self.run_agent(client, max_steps=3)
        last = client.requests[-1]
        self.assertEqual(last["tool_choice"], {"type": "none"})
        self.assertEqual(last["tools"], TOOLS)  # still defined for earlier turns
        self.assertEqual(last["messages"][-1]["content"][-1]["text"], FORCE_FINISH_PROMPT)
        trace = self.saved()
        self.assertEqual(trace["stopped_reason"], "forced_finish")
        self.assertEqual(trace["coverage"]["level"], "partial")
        # Roles must alternate or the API rejects the request.
        roles = [m["role"] for m in last["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant", "user"])

    def test_single_step_budget_goes_straight_to_writing(self):
        client = FakeClient(reply(text("Paris.\n\nCOVERAGE: complete — simple fact")))
        self.run_agent(client, max_steps=1)
        request = client.requests[0]
        self.assertEqual(request["tool_choice"], {"type": "none"})
        self.assertIn(FORCE_FINISH_PROMPT, request["messages"][0]["content"])

    def test_truncated_document_is_continued_and_joined(self):
        client = FakeClient(
            reply(text("# Guide\n\nThe first ha"), stop_reason="max_tokens"),
            reply(text("lf, and the rest.\n\nCOVERAGE: complete — done")),
        )
        result = self.run_agent(client)
        self.assertEqual(result, "# Guide\n\nThe first half, and the rest.\n\n"
                                 "COVERAGE: complete — done")
        second = client.requests[1]
        self.assertEqual(second["messages"][-1]["content"], CONTINUE_PROMPT)
        self.assertEqual(second["tool_choice"], {"type": "none"})
        self.assertEqual(self.saved()["final_output"], result)

    def test_continuation_attempts_are_bounded(self):
        cut = lambda: reply(text("more "), stop_reason="max_tokens")
        client = FakeClient(*[cut() for _ in range(1 + orchestrator.MAX_CONTINUATIONS)])
        result = self.run_agent(client)
        self.assertEqual(result, "more " * (1 + orchestrator.MAX_CONTINUATIONS))
        self.assertEqual(client.responses, [])
        warnings = [s["content"] for s in self.saved()["steps"] if s["kind"] == "warning"]
        self.assertIn("final document truncated at max_tokens", warnings)

    def test_failed_tool_call_is_reported_to_the_model_not_raised(self):
        client = FakeClient(
            reply(search("boom"), stop_reason="tool_use"),
            reply(text("# Guide\n\nCOVERAGE: partial — search failed")),
        )
        self.run_agent(client)
        tool_result = client.requests[1]["messages"][-1]["content"][0]["content"]
        self.assertIn("Tool call failed: RuntimeError: server died", tool_result)
        self.assertEqual(self.saved()["stopped_reason"], "self_terminated")

    def test_api_error_closes_the_trace_without_a_document(self):
        error = APIConnectionError(request=httpx.Request("POST", "https://example.test"))
        client = FakeClient(reply(search("one"), stop_reason="tool_use"), error)
        result = self.run_agent(client)
        self.assertIn("Anthropic API error", result)
        trace = self.saved()
        self.assertEqual(trace["stopped_reason"], "api_error")
        self.assertEqual(len(trace["steps"]), 2)  # the search before the failure
        self.assertIsNone(self.tracer.document_path)

    def test_first_request_carries_goal_budget_and_system_prompt(self):
        client = FakeClient(reply(text("Done.\n\nCOVERAGE: complete — n/a")))
        self.run_agent(client, max_steps=6, goal="Explain {braces} & <tags>")
        request = client.requests[0]
        self.assertEqual(request["system"], SYSTEM_PROMPT)
        first = request["messages"][0]["content"]
        self.assertIn("<goal>\nExplain {braces} & <tags>\n</goal>", first)
        self.assertIn("6 steps in total", first)
        self.assertIn("at most 5 for searching", first)


class HelperTests(unittest.TestCase):
    def test_coverage_line_is_parsed_leniently(self):
        self.assertEqual(parse_coverage("x\nCOVERAGE: **complete** — covers all"),
                         {"level": "complete", "note": "covers all"})
        self.assertEqual(parse_coverage("COVERAGE: Partial - missing X\n")["level"], "partial")
        self.assertEqual(parse_coverage("COVERAGE: partial — a\nmore\nCOVERAGE: complete — b")["note"], "b")
        self.assertIsNone(parse_coverage("Our coverage: complete nonsense is not at line start?"))
        self.assertIsNone(parse_coverage("no line here"))
        self.assertIsNone(parse_coverage(None))

    def test_config_prefers_new_names_and_accepts_legacy_ones(self):
        with patch.dict(os.environ, {"AUTOAGENT_MODEL": "old", "AUTOAGENT_MAX_STEPS": "3"}, clear=True):
            config = orchestrator.load_config()
            self.assertEqual((config.model, config.max_steps), ("old", 3))
            self.assertEqual(config.max_tokens, orchestrator.DEFAULT_MAX_TOKENS)
        with patch.dict(os.environ, {"AUTOAGENT_MODEL": "old", "STUDYAGENT_MODEL": "new"}, clear=True):
            self.assertEqual(orchestrator.load_config().model, "new")

    def test_invalid_settings_are_rejected_with_a_clear_error(self):
        for env in ({"STUDYAGENT_MAX_STEPS": "eight"}, {"STUDYAGENT_MAX_STEPS": "0"},
                    {"STUDYAGENT_MAX_TOKENS": "999999"}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigError):
                orchestrator.load_config()

    def test_goal_can_come_from_arguments_or_a_file(self):
        self.assertEqual(orchestrator.read_goal(["study", "graphs"]), "study graphs")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "goal.txt"
            path.write_text("Line one\nLine two\n", encoding="utf-8")
            self.assertEqual(orchestrator.read_goal(["--goal-file", str(path)]),
                             "Line one\nLine two")

    def test_prompt_templates_only_use_known_placeholders(self):
        INITIAL_USER_PROMPT.format(goal="g", today="t", max_steps=2, research_steps=1)
        self.assertIn("COVERAGE:", SYSTEM_PROMPT)
