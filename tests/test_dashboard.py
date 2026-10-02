import json
from pathlib import Path
import tempfile
import unittest

from src.dashboard import create_app
from src.trace_store import read_trace, trace_summary


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.trace = {"goal": "<script>alert(1)</script>", "model": "test-model",
                      "steps": [{"kind": "reasoning", "content": "test"}],
                      "final_output": "Finished", "stopped_reason": "self_terminated"}
        (self.runs / "run_test.json").write_text(json.dumps(self.trace))
        self.client = create_app(self.runs).test_client()

    def test_browse_and_download_without_agent_keys(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"&lt;script&gt;", page.data)
        self.assertNotIn(b"<script>alert", page.data)
        self.assertIn(b"Finished", self.client.get("/runs/run_test.json").data)
        download = self.client.get("/runs/run_test.json/download")
        self.assertEqual(download.json, self.trace)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        download.close()

    def test_incomplete_trace_does_not_break_listing(self):
        (self.runs / "run_bad.json").write_text('{"goal":')
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/runs/run_bad.json").status_code, 422)

    def test_outside_files_and_symlinks_are_not_served(self):
        outside = self.root / "private.json"
        outside.write_text(json.dumps(self.trace))
        self.assertEqual(self.client.get("/runs/../private.json").status_code, 404)
        self.assertEqual(self.client.get("/runs/missing.json").status_code, 404)
        try:
            (self.runs / "link.json").symlink_to(outside)
        except OSError:
            return  # Windows hosts may require privileges to create symlinks.
        self.assertEqual(self.client.get("/runs/link.json").status_code, 404)
        self.assertNotIn(b"link.json", self.client.get("/").data)

    def test_trace_shape_is_validated(self):
        (self.runs / "invalid.json").write_text('{"goal":"x","steps":{},"model":3}')
        with self.assertRaises(ValueError):
            read_trace(self.runs / "invalid.json")

    def test_summary_is_bounded(self):
        self.trace["goal"] = "a" * 3000
        self.trace["final_output"] = "b" * 10000
        result = trace_summary(self.trace)
        self.assertEqual(len(result["goal"]), 2000)
        self.assertEqual(len(result["final_output_preview"]), 4000)
        self.assertEqual(result["step_count"], 1)
