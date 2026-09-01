"""Local HTTP backend for index.html — lets you ask questions from a
browser instead of the CLI. Built on the stdlib http.server (no Flask)
to keep this test folder's dependency list minimal.

Run: python server.py
Then open index.html in a browser (it talks to http://localhost:8765).
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bing_search import BingGroundedTester, build_scoped_query, get_domains, load_env_file

load_env_file()

PORT = int(os.environ.get("TEST_SERVER_PORT", "8765"))

tester = None
domains = []


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/domains":
            self._send_json(200, {"domains": domains})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ask":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            question = (data.get("question") or "").strip()
            if not question:
                self._send_json(400, {"error": "question is required"})
                return

            result = tester.ask(question, domains)
            in_domain = [c for c in result["citations"] if any(d in c for d in domains)]
            out_domain = [c for c in result["citations"] if c not in in_domain]

            self._send_json(200, {
                **result,
                "domains_under_test": domains,
                "citations_pass": in_domain,
                "citations_fail": out_domain,
            })
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print("[server]", fmt % args)


def main():
    global tester, domains
    domains = get_domains()
    print("Domains under test:", domains)

    tester = BingGroundedTester()
    print(f"Agent created: {tester.agent.id}")
    print(f"Serving on http://localhost:{PORT} — open index.html and ask away.")

    try:
        HTTPServer(("localhost", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down — cleaning up test agent...")
        tester.cleanup()


if __name__ == "__main__":
    main()
