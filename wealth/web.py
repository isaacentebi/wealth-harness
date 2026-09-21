"""Loopback-only browser chat backed by the same Wealth agent and client memory."""
from __future__ import annotations

import argparse
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import AgentError, run_turn, seed_demo, resolve_model
from .behavior import ONBOARDING_WELCOME
from .service import WealthService, database_path
from .store import ClientNotFoundError, ClientExistsError, StoreError


class Chat:
    def __init__(self, db, client_id, model="sol", web_search=True):
        self.db, self.client_id, self.model = db, client_id, model
        self.web_search = web_search
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.messages = []
        service = WealthService(db)
        try:
            service.inspect(client_id)
        except ClientNotFoundError:
            try:
                service.create(client_id, client_id)
            except ClientExistsError:
                pass
        self.welcome = ONBOARDING_WELCOME if not service.inspect(client_id)["facts"] else ""

    def state(self):
        return {"client_id": self.client_id, "model": resolve_model(self.model),
                "csrf_token": self.token, "messages": list(self.messages), "welcome": self.welcome,
                "capabilities": {"python_analytics": True, "persistent_memory": True,
                                 "live_market_data": True, "web_search": self.web_search}}

    def ask(self, message):
        if not self.lock.acquire(blocking=False):
            raise BlockingIOError("A response is already in progress. Please wait.")
        try:
            history = [(m["role"], m["content"]) for m in self.messages]
            if self.welcome:
                history.insert(0, ("assistant", self.welcome))
            answer = run_turn(message, client_id=self.client_id, db_path=self.db,
                              model=self.model, history=history, web_search=self.web_search,
                              profile_empty=not WealthService(self.db).inspect(self.client_id)["facts"])
            self.messages.extend([{"role": "user", "content": message},
                                  {"role": "assistant", "content": answer}])
            self.messages = self.messages[-100:]
            return answer
        finally:
            self.lock.release()


def create_server(chat, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log financial prompts or URL payloads.

        def local_request(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            return self.headers.get("Host") in hosts and (
                not self.headers.get("Origin") or self.headers["Origin"] in {"http://" + h for h in hosts})

        def respond(self, status, value, content_type="application/json"):
            data = json.dumps(value).encode() if content_type == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.local_request():
                return self.respond(403, {"error": "Local origin required."})
            if self.path == "/":
                return self.respond(200, Path(__file__).with_name("chat.html").read_bytes(), "text/html")
            if self.path == "/api/state":
                return self.respond(200, chat.state())
            self.respond(404, {"error": "Not found."})

        def do_POST(self):
            if not self.local_request() or not secrets.compare_digest(self.headers.get("X-Wealth-Token", ""), chat.token):
                return self.respond(403, {"error": "Reload the local chat page to reconnect."})
            if self.path != "/api/chat":
                return self.respond(404, {"error": "Not found."})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 40000 or self.headers.get_content_type() != "application/json":
                    return self.respond(400, {"error": "Send a JSON message under 40 KB."})
                body = json.loads(self.rfile.read(size))
                message = body.get("message") if isinstance(body, dict) else None
                if not isinstance(message, str) or not message.strip() or len(message) > 12000:
                    return self.respond(400, {"error": "Enter a message of 1–12,000 characters."})
                answer = chat.ask(message.strip())
            except BlockingIOError as exc:
                return self.respond(409, {"error": str(exc)})
            except (ValueError, UnicodeError):
                return self.respond(400, {"error": "Invalid request or client data."})
            except (AgentError, StoreError, OSError):
                return self.respond(502, {"error": "The agent could not complete this response. Check Codex login and try again. Any facts already saved remain in memory."})
            return self.respond(200, {"answer": answer})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Wealth browser chat")
    parser.add_argument("--client", default="personal")
    parser.add_argument("--model", default="sol")
    parser.add_argument("--db")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--web-search", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)
    db = database_path(args.db)
    if args.demo:
        if args.db is None:
            db = db.parent / "agent-demo.sqlite3"
        args.client = "fictional-demo"
        seed_demo(db, args.client)
    chat = Chat(db, args.client, args.model, args.web_search)
    server = create_server(chat, args.port)
    print(f"Wealth chat: http://127.0.0.1:{server.server_port} · client {args.client}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
