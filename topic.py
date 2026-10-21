#!/usr/bin/env python3
"""Switch the explorer to any topic with one command.

    python3 topic.py "Cybernetics"            build it if needed, then open it in the explorer
    python3 topic.py "Cybernetics" --rebuild  build again (e.g. after editing its curation file)
    python3 topic.py                          open the explorer with the topics built so far
    python3 topic.py --list                   list the topics built so far

Graphs go in graphs/<slug>.json. Each topic reads its own curation file,
graphs/<slug>.curation.json, if it exists. Extra options after `--` are passed to
`wikigraph.py build` (for example `-- --max-nodes 600`). Only the standard library is used.
"""

import argparse
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GRAPHS = ROOT / "graphs"
# Wikimedia asks for a contact in the User-Agent. A project URL works, so no email is needed.
DEFAULT_CONTACT = "https://github.com/fellobello/wikigraph"


def slugify(topic):
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return slug or "topic"


def graph_path(topic):
    return GRAPHS / f"{slugify(topic)}.json"


def build(topic, contact, extra):
    GRAPHS.mkdir(exist_ok=True)
    out = graph_path(topic)
    cmd = [sys.executable, str(ROOT / "wikigraph.py"), "build", topic, "-o", str(out),
           "--contact", contact, "--cache", str(ROOT / ".wikigraph-cache"),
           "--curation", str(GRAPHS / f"{slugify(topic)}.curation.json"), *extra]
    print(f"Building {topic!r} -> {out.relative_to(ROOT)} (first build of a topic takes a few minutes)", flush=True)
    subprocess.run(cmd, check=True)
    return out


def write_index():
    """Rewrite graphs/index.json, which feeds the explorer's Topic menu."""
    GRAPHS.mkdir(exist_ok=True)
    topics = []
    for f in sorted(GRAPHS.glob("*.json")):
        if f.name == "index.json" or f.name.endswith(".curation.json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        nodes = data.get("nodes", []) if isinstance(data, dict) else data
        topics.append({"file": f"graphs/{f.name}",
                       "topic": ", ".join(meta.get("topics", [])) or f.stem,
                       "nodes": len(nodes)})
    (GRAPHS / "index.json").write_text(json.dumps(topics, indent=1), encoding="utf-8")
    return topics


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(port, open_path):
    handler = partial(QuietHandler, directory=str(ROOT))
    for p in range(port, port + 20):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", p), handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"No free port between {port} and {port + 19}.")
    url = f"http://localhost:{httpd.server_port}/{open_path}"
    print(f"Explorer: {url}\nPress Ctrl+C to stop.")
    threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description="Build (if needed) and open a topic in the explorer.")
    ap.add_argument("topic", nargs="?", help='Wikipedia article title, e.g. "Cybernetics"')
    ap.add_argument("--rebuild", action="store_true", help="build again even if the graph exists")
    ap.add_argument("--list", action="store_true", help="list built topics and exit")
    ap.add_argument("--no-open", action="store_true", help="build only; don't start the explorer")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--contact", default=os.environ.get("WIKIGRAPH_CONTACT", DEFAULT_CONTACT),
                    help="contact for the Wikipedia User-Agent (default: this project's URL)")
    opts = ap.parse_args(argv)

    if opts.list:
        for t in write_index():
            print(f"{t['topic']:40} {t['nodes']:5} concepts  {t['file']}")
        return

    open_path = "explorer.html"
    if opts.topic:
        out = graph_path(opts.topic)
        if opts.rebuild or not out.exists():
            build(opts.topic, opts.contact, extra)
        else:
            print(f"Using {out.relative_to(ROOT)} (add --rebuild to build it again)")
        open_path = f"explorer.html?graph=graphs/{out.name}"
    write_index()
    if not opts.no_open:
        serve(opts.port, open_path)


if __name__ == "__main__":
    main()
