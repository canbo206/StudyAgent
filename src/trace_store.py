"""Read and validate saved traces without running the agent or loading API keys."""

import json
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
MAX_TRACE_BYTES = 10 * 1024 * 1024


def read_trace(path):
    path = Path(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_TRACE_BYTES + 1)
    if len(raw) > MAX_TRACE_BYTES:
        raise ValueError("Trace exceeds the 10 MiB limit.")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("goal"), str):
        raise ValueError("Trace must contain a goal string.")
    if not isinstance(data.get("steps"), list):
        raise ValueError("Trace must contain a steps list.")
    for key in ("model", "started_at", "stopped_reason", "final_output"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError(f"{key} must be text or null.")
    return data


def trace_summary(data):
    """Keep Firestore documents bounded; raw steps stay in the local/blob trace."""
    return {
        "goal": data["goal"][:2000],
        "model": (data.get("model") or "unknown")[:200],
        "started_at": (data.get("started_at") or "")[:100],
        "stopped_reason": (data.get("stopped_reason") or "in_progress")[:200],
        "step_count": len(data["steps"]),
        "final_output_preview": (data.get("final_output") or "")[:4000],
    }
