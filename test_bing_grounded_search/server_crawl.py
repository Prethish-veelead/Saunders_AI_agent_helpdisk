"""Local HTTP backend for index_crawl.html — the crawl-and-match approach,
served on a different port (8766) than the Bing version (8765) so both can
run and be compared side by side.

Run: python server_crawl.py
Then open index_crawl.html in a browser.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bing_search import load_env_file
from crawl_search import CrawlAnswerer, FIXED_URLS, search_and_answer

load_env_file()

PORT = int(os.environ.get("CRAWL_SERVER_PORT", "8766"))

answerer = None


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
            self._send_json(200, {"seed_urls": FIXED_URLS})
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

            result = search_and_answer(question, answerer)
            self._send_json(200, result)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print("[server_crawl]", fmt % args)


def main():
    global answerer
    print("Seed URLs (crawl stays inside these domains only):")
    for u in FIXED_URLS:
        print(f"  - {u}")

    answerer = CrawlAnswerer()
    print(f"Serving on http://localhost:{PORT} — open index_crawl.html and ask away.")

    try:
        HTTPServer(("localhost", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down...")
        answerer.close()


if __name__ == "__main__":
    main()
