"""Minimal callback receiver for end-to-end testing of task requests.

ARI's task flow (POST /v1/tasks) delivers its result asynchronously to
whatever `config.callback.url` the request named — there's no receiver
built into ARI itself, since a real caller (e.g. ari-mcp-service) would run
its own. This script stands in for that caller during local testing: it
listens on a port, logs every callback it receives (headers + body) to a
log file and stdout, and always returns 200.

Usage:
    python scripts/e2e_callback_receiver.py --port 9100 --log logs/callback.log
"""

import argparse
import datetime
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(log_path: str):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                parsed = json.loads(body)
                pretty = json.dumps(parsed, indent=2)
            except json.JSONDecodeError:
                pretty = body.decode(errors="replace")

            entry = (
                f"\n=== callback received {datetime.datetime.now(datetime.timezone.utc).isoformat()} ===\n"
                f"path: {self.path}\n"
                f"headers: {dict(self.headers)}\n"
                f"body:\n{pretty}\n"
            )
            print(entry, flush=True)
            with open(log_path, "a") as f:
                f.write(entry)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "received"}')

        def log_message(self, format, *args):
            pass  # quiet the default access log; we log our own structured entry above

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--log", default="logs/callback.log")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(args.log))
    print(f"callback receiver listening on :{args.port}, logging to {args.log}")
    server.serve_forever()


if __name__ == "__main__":
    main()
