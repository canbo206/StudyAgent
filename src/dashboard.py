"""Local, read-only Flask browser for existing research traces."""

from pathlib import Path
from flask import Flask, abort, jsonify, render_template, send_file

try:
    from .trace_store import RUNS_DIR, read_trace, trace_summary
except ImportError:  # Support `python src/dashboard.py` as well as module imports.
    from trace_store import RUNS_DIR, read_trace, trace_summary


def create_app(runs_dir=None):
    app = Flask(__name__)
    root = Path(runs_dir) if runs_dir is not None else RUNS_DIR

    def trace_path(filename):
        # Only a direct JSON child, never a symlink or arbitrary filesystem path.
        path = root / filename
        if (filename != Path(filename).name or not filename.endswith(".json")
                or path.is_symlink() or path.resolve().parent != root.resolve()
                or not path.is_file()):
            abort(404)
        return path

    def load(filename):
        path = trace_path(filename)
        try:
            return path, read_trace(path)
        except (OSError, ValueError, UnicodeError):
            abort(422, description="This trace is incomplete, invalid, or too large.")

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/")
    def index():
        traces = []
        unreadable = 0
        for path in sorted(root.glob("*.json"), reverse=True):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                traces.append(dict(filename=path.name, **trace_summary(read_trace(path))))
            except (OSError, ValueError, UnicodeError):
                unreadable += 1
        return render_template("traces.html", traces=traces, unreadable=unreadable)

    @app.get("/runs/<filename>")
    def detail(filename):
        _, trace = load(filename)
        return render_template("trace.html", trace=trace, filename=filename)

    @app.get("/runs/<filename>/download")
    def download(filename):
        path, _ = load(filename)
        return send_file(path, as_attachment=True, download_name=filename, mimetype="application/json")

    return app


if __name__ == "__main__":
    # This dashboard contains local research. Keep it on loopback without debug.
    create_app().run(host="127.0.0.1", port=5000, debug=False)
