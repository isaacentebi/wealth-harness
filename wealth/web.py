"""Loopback-only browser chat backed by the same Wealth agent and client memory.

Turns run in a background thread and publish display-safe events (progress
steps, memory receipts, the answer or a classified error). The page reads them
over a token-protected server-sent-event stream and can stop a turn, which kills
the Codex process group.

The conversation is kept in the client's SQLite database (``conversations`` and
``conversation_messages``, see ``store.py``), so a restart shows the same messages
and resumes the same Codex thread. Messages are redacted before they are written
(RFC, CURP and SSN removed; CLABE, card and account numbers masked to the last four
digits); the page keeps the words as typed for the session. "Nueva conversación"
starts a new conversation and keeps the old one; export includes them and forget
deletes them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import agent as _agent
from . import onboarding as _onboarding
from .agent import (
    AgentError, REASONING_LEVELS, TurnControl, TurnEvent, profile_state, remember_exchange, resolve_model,
    situation_context,
    run_turn, seed_demo, stream_turn,
)
from .views import placed_ids, png_available, render_png, render_svg
from .service import WealthService, database_path, upload_dir
from .profile import (connections_view, export_payload, fact_action, fact_detail, form_facts, profile_view,
                      review_view, today_view)
from .store import (ClientExistsError, ClientNotFoundError, ContradictionNotFoundError, DecisionNotFoundError,
                    IneligibleEvidenceError, RequestConflictError, StaleRevisionError, StoreError, ValidationError)
from .store import CONVERSATION_LIMIT, WealthStore
from .execution import tickets as _tickets

STARTERS = (
    {"label": "Upload a statement", "prompt": "", "send": False, "attach": True},
    {"label": "Where does my money go each month?",
     "prompt": "Where does my money go each month, and how much could I invest?", "send": True},
    {"label": "Should I invest in something?",
     "prompt": "I’m thinking about investing in ", "send": False},
)
ERROR_TEXT = {
    "not_installed": "Codex isn’t installed on this computer. Install the Codex CLI, then retry.",
    "not_logged_in": "Codex is signed out. Run `codex login` in a terminal, then retry.",
    "timeout": "This took too long and was stopped. Try again, or choose Fast for a quicker answer.",
    "model_error": "The configured model was rejected. Restart Wealth with a supported --model.",
    "cancelled": "Stopped. Anything already saved to memory stays saved.",
    "storage": "Local memory couldn’t be read. Check the database path and disk, then retry.",
    "other": "Something went wrong while answering. Any facts already saved remain in memory.",
}
REVEAL_REQUEST = (
    "Setup just finished. This request comes from Wealth, not the person. Write the first synthesis from the "
    "<situation> brief: one short paragraph on where they stand (net worth, what is left each month, reserve in "
    "months, debts and when they are paid off, whatever is known) and a single next step. Write it in {language}. "
    "Do not ask any setup question again and do not list what is unknown; call a tool only if the paragraph needs "
    "a number the brief does not have."
)
REVEAL_STATEMENTS = (
    " The person attached statements during setup: read them first with wealth_ingest and follow the upload "
    "flow (insights, total and date, ask to save), then give the synthesis."
)
MAX_MESSAGE_CHARS = 12_000
MAX_JSON_BYTES = 40_000
MAX_JSON_DEPTH = 32  # objects and lists nested deeper than this are refused before parsing


def json_depth(raw: bytes) -> int:
    """The deepest nesting of objects/lists in a JSON text (brackets inside strings are ignored)."""
    depth = deepest = 0
    in_string = escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            deepest = max(deepest, depth)
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1
    return deepest


def _store_message(exc: Exception) -> str:
    """A store validation error in words for the page (never a stack trace or raw repr dump)."""
    text = str(exc).split("; see fact_contract.schema")[0].strip()
    if "expected_revision" in text or "merge=true" in text:
        return "This changed since the page was loaded. Reload and try again."
    if "is too large" in text:
        return "That number is too large; check it for a typo."
    if not text:
        return "That can’t be saved."
    text = text[:1].upper() + text[1:]
    if len(text) > 200:
        text = text[:197] + "..."
    return text if text.endswith((".", "?", "!")) else text + "."
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS = 5
# Words that make a message a bare greeting or thanks. Deliberately short: "ok", "sí", "no", "listo" can
# accept advice or answer a question, so they always go to the memory step.
_SMALL_TALK = frozenset("""
hola hi hello hey buenas buenos buen dia dias día días tardes noches morning afternoon evening good
gracias muchas muchísimas thanks thank you thx ty que qué tal how are estas estás cómo como saludos
adios adiós bye nos vemos hasta luego
""".split())


def is_small_talk(message: str) -> bool:
    """A greeting or thanks that states nothing (no digits, only greeting words): the memory step is skipped."""

    text = (message or "").strip().lower()
    if not text or len(text) > 60 or re.search(r"\d", text):
        return False
    words = re.findall(r"[^\W\d_]+", text)
    return bool(words) and len(words) <= 6 and all(word in _SMALL_TALK for word in words)


MAX_TURN_VIEWS = 24
UPLOAD_TYPES = {
    "application/pdf": ".pdf",
    "text/csv": ".csv",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
_HISTORY_PATH = re.compile(r"^/api/profile/fact/([^/]{1,200})/history$")
_CONTRADICTION_PATH = re.compile(r"^/api/profile/contradictions/([A-Za-z0-9_-]{1,80})$")
_FACT_PATH = re.compile(r"^/api/facts/([^/]{1,200})$")
_TURN_PATH = re.compile(r"^/api/turns/([0-9a-f]{16})(/events|/cancel)?$")
SURFACE_GETS = frozenset({"/review", "/api/today", "/api/review", "/api/connections", "/api/export", "/api/tax-pack"})
_VIEW_PATH = re.compile(r"^/api/views/([a-z][a-z0-9_]{0,31}-[0-9a-f]{10})\.(svg|png)$")
# Order tickets: confirm takes a ticket id; cancel takes a ticket id (discard) or an Alpaca order id.
_ORDER_PATH = re.compile(r"^/api/orders/([A-Za-z0-9-]{1,64})/(confirm|cancel)$")
# Profile reads (the profile, contradictions, one fact, its history) need the session token like every
# other /api read. profile.html sends it once its owner's update lands; until then this stays False so the
# page keeps working. Flip to True (or set WEALTH_PROFILE_READS_NEED_TOKEN=1) to enforce it.
PROFILE_READS_NEED_TOKEN = True


def profile_reads_need_token() -> bool:
    return PROFILE_READS_NEED_TOKEN or os.environ.get("WEALTH_PROFILE_READS_NEED_TOKEN") == "1"


def friendly_name(client_id: str, display_name: str | None = None) -> str:
    name = display_name if display_name and display_name != client_id else client_id
    words = re.sub(r"[-_.]+", " ", name or "").strip()
    return (words[:1].upper() + words[1:]) if words else "Personal"


def _valid_timezone(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return value


# --------------------------------------------------------------------------- uploads


class Uploads:
    """Per-client attachment store under the data directory.

    Stored names are server-generated, so a client-supplied filename is only
    ever display text. Types are allowlisted and checked against file content.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    @staticmethod
    def clean_name(raw: str) -> str:
        name = re.split(r"[\\/]", unquote(raw or ""))[-1]
        name = "".join(ch for ch in name if ch.isprintable()).strip().lstrip(".")
        return name[:120] or "file"

    @staticmethod
    def _content_ok(content_type: str, head: bytes) -> bool:
        if content_type == "application/pdf":
            return head.startswith(b"%PDF-")
        if content_type == "image/png":
            return head.startswith(b"\x89PNG\r\n\x1a\n")
        if content_type == "image/jpeg":
            return head.startswith(b"\xff\xd8\xff")
        if content_type == "image/webp":
            return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        if content_type == "text/csv":
            return b"\x00" not in head
        return False

    def save(self, name: str, content_type: str, stream, length: int) -> dict[str, Any]:
        if content_type not in UPLOAD_TYPES:
            raise ValueError("Attach a PDF, CSV, PNG, JPEG or WebP file.")
        if not 0 < length <= MAX_UPLOAD_BYTES:
            raise ValueError("Choose a non-empty file under 25 MB.")
        self._private_dirs()
        upload_id = secrets.token_hex(16)
        target = self.dir / f"{upload_id}{UPLOAD_TYPES[content_type]}"
        partial = target.with_suffix(".part")
        remaining, head = length, b""
        try:
            with open(partial, "xb") as handle:
                partial.chmod(0o600)
                while remaining:
                    chunk = stream.read(min(65_536, remaining))
                    if not chunk:
                        raise ValueError("The upload was incomplete.")
                    if len(head) < 65_536:
                        head += chunk[: 65_536 - len(head)]
                    handle.write(chunk)
                    remaining -= len(chunk)
            if not self._content_ok(content_type, head):
                raise ValueError("The file’s contents don’t match its type.")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        meta = {"id": upload_id, "name": self.clean_name(name), "type": content_type,
                "size": length, "created": int(time.time())}
        sidecar = os.open(self.dir / f"{upload_id}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(sidecar, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(meta))
        return meta

    def _private_dirs(self) -> None:
        """The uploads root and the client's folder: created (or tightened to) 0700, whatever the umask."""
        for folder in (self.dir.parent, self.dir):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not folder.is_dir() or (folder == self.dir and folder.is_symlink()):
                raise OSError("The upload folder is not a directory.")
            info = folder.stat()
            if info.st_mode & 0o077 and info.st_uid == os.getuid():
                folder.chmod(0o700)

    def get(self, upload_id: str) -> dict[str, Any] | None:
        if not isinstance(upload_id, str) or not _UPLOAD_ID.match(upload_id):
            return None
        try:
            meta = json.loads((self.dir / f"{upload_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        path = self.dir / f"{upload_id}{UPLOAD_TYPES.get(meta.get('type'), '.bin')}"
        if not path.is_file():
            return None
        return {**meta, "path": str(path)}


# --------------------------------------------------------------------------- turns


class Turn:
    def __init__(self, message: str, attachments: list[dict[str, Any]], internal: bool = False):
        self.id = secrets.token_hex(8)
        self.message = message
        self.attachments = attachments
        self.internal = internal  # a request from Wealth itself (the reveal): never shown as the person's words
        self.started = time.time()
        self.status = "running"  # running | done | error | cancelled
        self.progress = ""
        self.memory: list[dict[str, str]] = []
        self.views: dict[str, dict[str, Any]] = {}  # offered by results this turn; only placed ones reach the page
        self.answer: str | None = None
        self.partial = ""  # the answer as streamed so far (the current message item), kept if the turn is stopped
        self.partial_item = ""
        self.recorded = False  # a stopped or failed turn already joined the saved conversation
        self.error: dict[str, str] | None = None
        self.exception: BaseException | None = None
        self.control = TurnControl()
        self.events: list[dict[str, Any]] = []
        self._cond = threading.Condition()

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def emit(self, type_: str, **data: Any) -> None:
        with self._cond:
            self.events.append({"seq": len(self.events) + 1, "type": type_, **data})
            self._cond.notify_all()

    def finish(self, status: str) -> None:
        with self._cond:
            self.status = status
            self.events.append({"seq": len(self.events) + 1, "type": "done", "status": status})
            self._cond.notify_all()

    def wait_events(self, after: int, timeout: float) -> list[dict[str, Any]]:
        with self._cond:
            if len(self.events) <= after and not self.finished:
                self._cond.wait(timeout)
            return self.events[after:]

    def wait(self, timeout: float | None = None) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self.finished, timeout)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "message": "" if self.internal else self.message,
                "internal": self.internal,
                "attachments": [_public_attachment(a) for a in self.attachments],
                "progress": self.progress, "memory": list(self.memory), "error": self.error,
                "elapsed": round(time.time() - self.started, 1), "last_event": len(self.events)}


def _log_failure(turn: Turn, kind: str, summary: str, detail: str) -> None:
    """One stderr line for a failed turn: its kind and diagnostic, never the person's words."""
    from .ingest.redact import redact_text

    detail = " ".join(redact_text(str(detail or "")).split())[:300]
    summary = " ".join(str(summary or "").split())[:200]
    print(f"wealth-chat: turn {turn.id} failed after {time.time() - turn.started:.1f}s: kind={kind} "
          f"summary={summary!r} detail={detail!r}", file=sys.stderr)


# A vermilion dot on the canvas (Dot), served for /favicon.ico and /favicon.svg so no page load 404s.
FAVICON_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
               b'<rect width="32" height="32" rx="7" fill="#FBF8F2"/><circle cx="16" cy="16" r="7" fill="#C84335"/></svg>')
# The pages' own CSS and JS under /static/<name>, by exact name only: the request path never reaches the filesystem.
# The type must be right: nosniff makes a browser drop a stylesheet or script served under any other type.
STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "chat.css": "text/css",
    "chat.js": "text/javascript",
    "profile.css": "text/css",
    "profile.js": "text/javascript",
    "review.css": "text/css",
    "review.js": "text/javascript",
}


def _public_attachment(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item[k] for k in ("id", "name", "type", "size") if k in item}


class Chat:
    def __init__(self, db, client_id, model=_agent.DEFAULT_MODEL, web_search=True, ephemeral=False):
        self.db, self.client_id, self.model = db, client_id, model
        self.web_search = web_search
        self.ephemeral = ephemeral
        self.reasoning = "low"
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.messages: list[dict[str, Any]] = []
        self.thread_id: str | None = None
        self.brief_revision: int | None = None  # the brief lists changes since this revision
        self.turn: Turn | None = None
        self.memory_thread: threading.Thread | None = None
        self.uploads = Uploads(upload_dir(client_id, db))
        service = WealthService(db)
        try:
            service.inspect(client_id)
        except ClientNotFoundError:
            try:
                service.create(client_id, client_id)
            except ClientExistsError:
                pass
        snapshot = service.inspect(client_id)
        self.display_name = friendly_name(client_id, (snapshot.get("client") or {}).get("display_name"))
        # A brand-new profile gets the setup cards inline; a returning one gets a single "Continue setup" row.
        # Once the person answers a card in this session, a reload keeps showing the cards.
        self.onboarding_live = not snapshot.get("facts")
        # The conversation survives a restart: the current one's last messages and its Codex thread.
        self.conversation_id: str | None = None
        self._load_conversation()

    # ------------------------------------------------------------------ conversation persistence

    def _load_conversation(self) -> None:
        with WealthStore(self.db) as store:
            current = store.conversation(self.client_id, CONVERSATION_LIMIT)
            self.conversation_id = current["id"] or store.start_conversation(self.client_id)
        self.messages = current["messages"]
        self.thread_id = current["thread_id"]

    def _persist(self, messages: list[dict[str, Any]], thread_id: str | None = None) -> None:
        """Append this turn's messages (redacted by the store) and the thread; a failure never breaks the chat."""
        try:
            plain = json.loads(json.dumps(messages, default=str))
            with WealthStore(self.db) as store:
                store.append_messages(self.client_id, self.conversation_id, plain, thread_id=thread_id)
        except (StoreError, sqlite3.Error, OSError, TypeError, ValueError) as exc:
            print(f"wealth-chat: saving the conversation failed with {type(exc).__name__}", file=sys.stderr)

    def _persist_memory(self, message_id: str, memory: list[dict[str, Any]]) -> None:
        try:
            with WealthStore(self.db) as store:
                store.set_message_memory(self.client_id, message_id, json.loads(json.dumps(memory, default=str)))
        except (StoreError, sqlite3.Error, OSError, TypeError, ValueError) as exc:
            print(f"wealth-chat: saving memory receipts failed with {type(exc).__name__}", file=sys.stderr)

    # ------------------------------------------------------------------ onboarding

    def onboarding(self, language: str | None = None, step: str | None = None) -> dict[str, Any]:
        """The current card (or the named one, prefilled, to edit an answer) and the running picture."""
        sit = WealthService(self.db).situation(self.client_id)
        card = _onboarding.card(sit, step, language) if step else _onboarding.next_step(sit, language)
        return {"card": card, "picture": _onboarding.picture(sit, language)}

    def onboarding_state(self, language: str | None = None) -> dict[str, Any]:
        sit = WealthService(self.db).situation(self.client_id)
        card = _onboarding.next_step(sit, language)
        if card is None:
            return {"active": False, "card": None}
        return {"active": True, "mode": "flow" if self.onboarding_live else "resume", "card": card,
                "picture": _onboarding.picture(sit, language), "remaining": len(_onboarding.remaining(sit))}

    def answer_onboarding(self, body: dict[str, Any]) -> dict[str, Any]:
        """Write one answer (or a skip, or typed text) and start the reveal when setup completes."""
        step, language = body.get("step"), body.get("lang")
        if not isinstance(step, str) or step not in _onboarding.BY_ID:
            raise ValueError("Choose a setup step.")
        if language is not None and language not in ("es", "en"):
            raise ValueError("lang must be es or en.")
        service = WealthService(self.db)
        skip = body.get("skip") is True
        answer = body.get("answer")
        if isinstance(body.get("text"), str):
            text = body["text"].strip()
            if not text or len(text) > MAX_MESSAGE_CHARS:
                raise ValueError("Enter a message of 1–12,000 characters.")
            card = _onboarding.card(service.situation(self.client_id), step, language)
            parsed = _onboarding.parse_free_text(card, text)
            if parsed["status"] != "parsed":
                # The hook for the model: for now the page sends the text as an ordinary turn.
                return {"needs_model": True, "reason": parsed.get("reason")}
            answer = parsed["answer"]
        uploads: list[str] = []
        if step == "statements" and isinstance(answer, dict):
            uploads = answer.get("uploads") or []
            if not isinstance(uploads, list) or len(uploads) > MAX_ATTACHMENTS or \
                    any(self.uploads.get(u) is None for u in uploads):
                raise ValueError("An attachment is no longer available. Attach it again.")
        result = _onboarding.apply(service, self.client_id, step, answer, skip=skip, language=language)
        self.onboarding_live = True
        reveal = None
        if result.pop("completed_now"):
            sit = service.situation(self.client_id)
            lang = _onboarding.reveal_language(sit, [m.get("content") for m in self.messages if m.get("role") == "user"],
                                               language)  # the conversation's language, then the saved one
            request = REVEAL_REQUEST.format(language="Mexican Spanish" if lang == "es" else "English")
            if uploads:
                request += REVEAL_STATEMENTS
            try:
                reveal = self.start(request, attachments=uploads, internal=True).summary()
            except BlockingIOError:
                reveal = None  # a turn is running; the person can still ask for the synthesis
        return {**result, "reveal": reveal}

    def saved_language(self) -> str | None:
        """The language the person set up Wealth in, so the page matches the conversation after a reload."""
        try:
            facts = WealthService(self.db).inspect(self.client_id, keys=["client.profile"]).get("facts") or []
        except (StoreError, sqlite3.Error):
            return None
        language = ((facts[0].get("value") or {}) if facts else {}).get("language")
        return language if language in ("es", "en") else None

    def state(self, language: str | None = None):
        turn = self.turn
        onboarding = self.onboarding_state(language if language in ("es", "en") else None)
        return {"client_id": self.client_id, "display_name": self.display_name,
                "language": self.saved_language(),
                "model": resolve_model(self.model),
                "reasoning": self.reasoning, "reasoning_levels": list(REASONING_LEVELS),
                "csrf_token": self.token, "messages": list(self.messages),
                "onboarding": onboarding,
                "starters": list(STARTERS) if not self.messages and not onboarding["active"] else [],
                "turn": turn.summary() if turn and turn.answer is None and (
                    turn.status == "running" or (turn.status in {"error", "cancelled"} and not turn.recorded)) else None,
                "uploads": {"max_bytes": MAX_UPLOAD_BYTES, "types": list(UPLOAD_TYPES),
                            "max_files": MAX_ATTACHMENTS},
                "capabilities": {"python_analytics": True, "persistent_memory": True,
                                 "live_market_data": True, "web_search": self.web_search,
                                 "streaming": True, "attachments": True}}

    def reset(self):
        if not self.lock.acquire(blocking=False):
            raise BlockingIOError("Wait for the current response to finish.")
        try:
            # A new conversation starts; the old one stays in the database (and in the export).
            with WealthStore(self.db) as store:
                self.conversation_id = store.start_conversation(self.client_id)
            self.messages = []
            self.thread_id = None
            self.brief_revision = None
            self.turn = None
        finally:
            self.lock.release()

    def _events(self, message: str, **kwargs: Any) -> Iterator[TurnEvent]:
        if run_turn is not _agent.run_turn:
            # A substituted blocking turn (tests, local stubs) has no event stream.
            kwargs.pop("control", None)
            kwargs.pop("defer_memory", None)
            kwargs.pop("views", None)
            kwargs.pop("person_message", None)
            yield TurnEvent("answer", run_turn(message, **kwargs))
            return
        yield from stream_turn(message, **kwargs)

    @property
    def defers_memory(self) -> bool:
        """Save after answering only with the real Codex runtime; substituted turns save in-turn."""
        return run_turn is _agent.run_turn and stream_turn is _agent.stream_turn

    def start(self, message, reasoning=None, attachments=(), timezone_name=None, internal=False) -> Turn:
        reasoning = self.reasoning if reasoning is None else reasoning
        if reasoning not in REASONING_LEVELS:
            raise ValueError("reasoning must be low, medium, or high")
        if not isinstance(attachments, (list, tuple)) or len(attachments) > MAX_ATTACHMENTS:
            raise ValueError(f"Attach at most {MAX_ATTACHMENTS} files.")
        files = []
        for upload_id in attachments:
            meta = self.uploads.get(upload_id)
            if meta is None:
                raise ValueError("An attachment is no longer available. Attach it again.")
            files.append(meta)
        # A short grace: the previous turn may be tidying up the instant its answer shows.
        if not self.lock.acquire(timeout=2):
            raise BlockingIOError("A response is already in progress. Please wait.")
        try:
            self.reasoning = reasoning
            turn = Turn(message, files, internal)
            self.turn = turn
            worker = threading.Thread(target=self._work, args=(turn, reasoning, _valid_timezone(timezone_name)),
                                      daemon=True, name=f"wealth-turn-{turn.id}")
            worker.start()
        except BaseException:
            self.lock.release()
            raise
        return turn

    def _work(self, turn: Turn, reasoning: str, timezone_name: str | None) -> None:
        # The next turn does not wait for the last one's memory step: saves run in the background, one at a
        # time and in order (see _queue_memory); the resumed thread already holds the last exchange.
        status = "error"
        defer = self.defers_memory
        handed_off = False
        thread_before, thread_saved = self.thread_id, False
        try:
            state = profile_state(self.db, self.client_id)
            brief, revision, offered = situation_context(self.db, self.client_id, turn.message, self.brief_revision)
            history = [(m["role"], m["content"]) for m in self.messages if m.get("status") != "failed"]
            answer = None
            for event in self._events(
                turn.message, client_id=self.client_id, db_path=self.db, model=self.model,
                history=history, web_search=self.web_search and not turn.attachments, reasoning=reasoning,
                profile_empty=not any(state.values()), profile=state, brief=brief,
                thread_id=self.thread_id, timezone_name=timezone_name,
                attachments=[{k: a[k] for k in ("name", "type", "size", "path")} for a in turn.attachments],
                control=turn.control, ephemeral=self.ephemeral, defer_memory=defer, views=offered,
                # Wealth's own requests (the setup reveal) carry no consent: only the person's words do.
                person_message="" if turn.internal else turn.message,
            ):
                if event.type == "view":
                    for spec in event.data.get("views", ()):
                        if isinstance(spec, dict) and isinstance(spec.get("id"), str) and len(turn.views) < MAX_TURN_VIEWS:
                            turn.views[spec["id"]] = spec
                elif event.type == "thread":
                    self.thread_id = str(event.data.get("thread_id") or "") or self.thread_id
                elif event.type == "delta":
                    # The answer as it is written; the page redraws it and the finished answer replaces it.
                    if event.text and not turn.control.cancelled:
                        item = str(event.data.get("item") or "")
                        if item != turn.partial_item:  # a new message item starts the text over, as on the page
                            turn.partial_item, turn.partial = item, ""
                        turn.partial += event.text
                        turn.emit("delta", text=event.text, item=item)
                elif event.type == "progress" and event.text != turn.progress:
                    turn.progress = event.text
                    turn.emit("progress", text=event.text)
                elif event.type == "memory":
                    known = {item["key"] for item in turn.memory}
                    new = [self._memory_item(str(k)) for k in event.data.get("keys", ()) if str(k) not in known]
                    turn.memory.extend(new)
                    if new:
                        turn.emit("memory", items=list(turn.memory))
                elif event.type == "answer":
                    answer = event.text
                    if event.data.get("thread_id"):
                        self.thread_id = str(event.data["thread_id"])
            if turn.control.cancelled:
                raise AgentError(None, "cancelled")
            if answer is None:
                raise AgentError("Codex completed without an assistant response.")
            self.brief_revision = revision
            user = {"id": secrets.token_hex(6), "role": "user", "content": turn.message}
            if turn.attachments:
                user["attachments"] = [_public_attachment(a) for a in turn.attachments]
            reply = {"id": secrets.token_hex(6), "role": "assistant", "content": answer}
            if turn.memory:
                reply["memory"] = list(turn.memory)
            # Only views the answer placed travel to the page; the rest were working material.
            placed = [turn.views[i] for i in placed_ids(answer, turn.views)]
            if placed:
                reply["views"] = placed
                turn.emit("views", items=placed, message_id=reply["id"])
            # The reveal's request is Wealth's own; only the answer joins the conversation.
            added = [reply] if turn.internal else [user, reply]
            self.messages = (self.messages + added)[-CONVERSATION_LIMIT:]
            self._persist(added, self.thread_id or "")
            thread_saved = True
            turn.answer = answer
            turn.emit("answer", user=None if turn.internal else user, message=reply)
            status = "done"
            if defer and not turn.internal and not is_small_talk(turn.message):
                # Saving runs after the reply is on screen; the turn closes when it finishes.
                self._queue_memory(turn, reply, brief)
                handed_off = True
        except AgentError as exc:
            status = "cancelled" if exc.kind == "cancelled" else "error"
            if status == "cancelled":
                self._fail(turn, exc, exc.kind, exc.detail, stopped=self._record_stopped(turn))
            else:
                _log_failure(turn, exc.kind, exc.summary, exc.detail)
                self._fail(turn, exc, exc.kind, exc.detail, failed=self._record_failed(turn))
        except (StoreError, sqlite3.Error, OSError) as exc:
            _log_failure(turn, "storage", type(exc).__name__, "")
            self._fail(turn, exc, "storage", type(exc).__name__, failed=self._record_failed(turn))
        except Exception as exc:  # noqa: BLE001 - surface any failure to the page
            _log_failure(turn, "other", type(exc).__name__, "")
            self._fail(turn, exc, "other", "", failed=self._record_failed(turn))
        finally:
            if not thread_saved and not turn.recorded and self.thread_id != thread_before:
                self._persist([], self.thread_id or "")  # a failed turn may still have opened the thread
            # Free the chat before announcing the end, so a reply sent the moment the answer shows is never "busy".
            self.lock.release()
            if not handed_off:
                turn.finish(status)

    def _queue_memory(self, turn: Turn, reply: dict[str, Any], brief: str | None) -> None:
        """Start this exchange's memory step after every earlier one (saves stay serial and in order).

        ``memory_thread`` is the newest save; joining it waits for all of them.
        """
        earlier = [str(m.get("content") or "") for m in self.messages if m.get("role") == "user"]
        recent = earlier[:-1] if earlier and earlier[-1] == turn.message else earlier  # captured now, not later
        previous = self.memory_thread

        def run() -> None:
            if previous is not None:
                previous.join()
            self._remember(turn, reply, brief, recent)

        self.memory_thread = threading.Thread(target=run, daemon=True, name=f"wealth-memory-{turn.id}")
        self.memory_thread.start()

    def _remember(self, turn: Turn, reply: dict[str, Any], brief: str | None, recent: list[str]) -> None:
        try:
            keys = remember_exchange(turn.message, turn.answer or "", client_id=self.client_id, db_path=self.db,
                                     model=self.model, brief=brief, control=turn.control, recent_person=recent)
            items = [self._memory_item(k) for k in keys]
            if items:
                turn.memory.extend(items)
                reply["memory"] = list(turn.memory)
                self._persist_memory(reply["id"], reply["memory"])
                turn.emit("memory", items=list(turn.memory), message_id=reply["id"])
        except Exception as exc:  # noqa: BLE001 - a failed save must not break the conversation
            print(f"wealth-chat: memory step failed with {type(exc).__name__}", file=sys.stderr)
            turn.emit("memory_error", message_id=reply["id"])
        finally:
            turn.finish("done")

    def _user_record(self, turn: Turn, status: str | None = None) -> dict[str, Any]:
        user: dict[str, Any] = {"id": secrets.token_hex(6), "role": "user", "content": turn.message}
        if turn.attachments:
            user["attachments"] = [_public_attachment(a) for a in turn.attachments]
        if status:
            user["status"] = status
        return user

    def _record(self, turn: Turn, added: list[dict[str, Any]]) -> None:
        self.messages = (self.messages + added)[-CONVERSATION_LIMIT:]
        self._persist(added, self.thread_id or "")
        turn.recorded = True

    def _record_stopped(self, turn: Turn) -> dict[str, Any] | None:
        """A stopped turn keeps what was already on screen: the person's words and the answer streamed so far.

        The assistant message carries ``status: stopped`` (the page draws a quiet "Stopped" line under it). With
        nothing streamed yet the person's message alone is kept, marked stopped. Returns the stopped reply.
        """
        partial = turn.partial.strip()
        added: list[dict[str, Any]] = [] if turn.internal else [self._user_record(turn, None if partial else "stopped")]
        reply = None
        if partial:
            reply = {"id": secrets.token_hex(6), "role": "assistant", "content": partial, "status": "stopped"}
            if turn.memory:
                reply["memory"] = list(turn.memory)
            added.append(reply)
        if added:
            self._record(turn, added)
        return reply

    def _record_failed(self, turn: Turn) -> dict[str, Any] | None:
        """An unanswered message stays in the transcript, marked failed, so a reload shows it with Retry."""
        if turn.internal:
            return None
        user = self._user_record(turn, "failed")
        self._record(turn, [user])
        return user

    @staticmethod
    def _fail(turn: Turn, exc: BaseException, kind: str, detail: str, stopped: dict[str, Any] | None = None,
              failed: dict[str, Any] | None = None) -> None:
        turn.exception = exc
        turn.error = {"kind": kind, "message": ERROR_TEXT.get(kind, ERROR_TEXT["other"]), "detail": detail or ""}
        extra: dict[str, Any] = {}
        if stopped is not None:
            extra["stopped"] = stopped  # the partial answer, now a saved message
        if failed is not None:
            extra["user_id"] = failed["id"]
        turn.emit("error", **turn.error, **extra)

    def _memory_item(self, key: str) -> dict[str, str]:
        """A display item for a saved key; the page words it in the reader's language."""
        item = {"key": key}
        if key.startswith("account."):
            try:
                facts = WealthService(self.db).inspect(self.client_id, keys=[key]).get("facts") or []
                value = facts[0]["value"] if facts else {}
                institution = ((value or {}).get("account") or {}).get("institution")
                if institution:
                    item["institution"] = str(institution)
            except (StoreError, sqlite3.Error, AttributeError, TypeError, IndexError):
                pass
        return item

    def view(self, view_id: str) -> dict[str, Any] | None:
        """A view an answer in this conversation placed, newest first; None if there is none."""
        for message in reversed(self.messages):
            for spec in message.get("views") or ():
                if spec.get("id") == view_id:
                    return spec
        return None

    def cancel(self, turn_id: str) -> bool:
        turn = self.turn
        if turn is None or turn.id != turn_id:
            return False
        if not turn.finished:
            turn.control.cancel()
        return True

    def ask(self, message, reasoning=None, attachments=(), timezone_name=None):
        """Blocking turn used by the JSON endpoint and tests."""

        turn = self.start(message, reasoning, attachments, timezone_name)
        turn.wait()
        if turn.exception is not None:
            raise turn.exception
        return turn.answer


# --------------------------------------------------------------------------- HTTP


def _security_headers(script_src: str) -> tuple[tuple[str, str], ...]:
    return (
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy", f"default-src 'self'; script-src {script_src}; "
         "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
         "img-src 'self' data:; connect-src 'self'; "
         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    )


# The pages run only their /static scripts: no inline script anywhere they are served.
SECURITY_HEADERS = _security_headers("'self'")
# The tax pack's printable page is one self-contained file, opened from disk, so its print button and language
# toggle are inline; its download alone keeps inline script allowed.
PRINTABLE_SECURITY_HEADERS = _security_headers("'self' 'unsafe-inline'")


def _error_payload(kind: str, message: str | None = None, detail: str = "") -> dict[str, Any]:
    return {"error": message or ERROR_TEXT.get(kind, ERROR_TEXT["other"]), "kind": kind, "detail": detail}


def create_server(chat, port=8765, host="127.0.0.1"):
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("Wealth chat binds only to 127.0.0.1 or ::1")

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        daemon_threads = True

    class Handler(BaseHTTPRequestHandler):
        timeout = 30
        server_version = "Wealth"
        sys_version = ""

        def log_message(self, *args):
            pass  # Do not log financial prompts or URL payloads.

        def local_request(self):
            port_ = self.server.server_port
            names = ["localhost"] + (["[::1]"] if host == "::1" else ["127.0.0.1"])
            hosts = {f"{name}:{port_}" for name in names}
            return self.headers.get("Host") in hosts and (
                not self.headers.get("Origin") or self.headers["Origin"] in {"http://" + h for h in hosts})

        def authorized(self):
            return self.local_request() and secrets.compare_digest(
                self.headers.get("X-Wealth-Token", ""), chat.token)

        def respond(self, status, value, content_type="application/json"):
            data = json.dumps(value).encode() if content_type == "application/json" else value
            self.send_response(status)
            binary = content_type.startswith("image/") and not content_type.endswith("+xml")
            self.send_header("Content-Type", content_type if binary else content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for name, header in SECURITY_HEADERS:
                self.send_header(name, header)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def read_json(self):
            size = int(self.headers.get("Content-Length", "0") or 0)
            if not 0 < size <= MAX_JSON_BYTES or self.headers.get_content_type() != "application/json":
                raise ValueError("Send a JSON message under 40 KB.")
            raw = self.rfile.read(size)
            if json_depth(raw) > MAX_JSON_DEPTH:
                raise ValueError(f"Send JSON nested at most {MAX_JSON_DEPTH} levels deep.")
            try:
                body = json.loads(raw)
            except RecursionError:
                raise ValueError(f"Send JSON nested at most {MAX_JSON_DEPTH} levels deep.") from None
            if not isinstance(body, dict):
                raise ValueError("Send a JSON object.")
            return body

        def turn_request(self, body):
            message = body.get("message")
            attachments = body.get("attachments") or []
            if not isinstance(message, str) or len(message) > MAX_MESSAGE_CHARS:
                raise ValueError("Enter a message of 1–12,000 characters.")
            message = message.strip()
            if not message and attachments:
                message = "I’ve attached a file."
            if not message:
                raise ValueError("Enter a message of 1–12,000 characters.")
            return dict(message=message, reasoning=body.get("reasoning"), attachments=attachments,
                        timezone_name=body.get("timezone"))

        def handle_failure(self, exc):
            # Validation, lookup and conflict errors are the request's fault (4xx, in words);
            # only a real storage failure is a 503.
            if isinstance(exc, BlockingIOError):
                return self.respond(409, _error_payload("busy", str(exc)))
            if isinstance(exc, (StaleRevisionError, RequestConflictError, IneligibleEvidenceError)):
                return self.respond(409, _error_payload("conflict", "This changed elsewhere. Reload and try again."))
            if isinstance(exc, (ClientNotFoundError, DecisionNotFoundError, ContradictionNotFoundError)):
                return self.respond(404, _error_payload("missing", "That item is no longer there. Reload the page."))
            if type(exc) is LookupError:  # raised by profile lookups; KeyError stays a 500
                return self.respond(422, _error_payload("missing", "That item is no longer there."))
            if isinstance(exc, AgentError):
                return self.respond(502, _error_payload(exc.kind, None, exc.detail))
            if isinstance(exc, ValidationError):
                return self.respond(400, _error_payload("invalid", _store_message(exc)))
            if isinstance(exc, (StoreError, sqlite3.Error)):
                return self.respond(503, _error_payload("storage"))
            if isinstance(exc, RecursionError):
                return self.respond(400, _error_payload("invalid", "That request is nested too deeply."))
            if isinstance(exc, OverflowError):
                return self.respond(400, _error_payload("invalid", "That number is too large."))
            if isinstance(exc, (ValueError, UnicodeError)):
                message = str(exc) if str(exc)[:1].isupper() and len(str(exc)) < 120 else "Invalid request."
                return self.respond(400, _error_payload("invalid", message))
            if isinstance(exc, OSError):
                return self.respond(503, _error_payload("storage"))
            print(f"wealth-chat: request failed with {type(exc).__name__}", file=sys.stderr)
            return self.respond(500, _error_payload("other"))

        def do_GET(self):
            if not self.local_request():
                return self.respond(403, {"error": "Local origin required.", "kind": "forbidden"})
            url = urlsplit(self.path)
            try:
                if url.path.startswith("/static/"):
                    name = url.path[len("/static/"):]
                    if name not in STATIC_FILES:
                        return self.respond(404, {"error": "Not found."})
                    return self.respond(200, (STATIC_DIR / name).read_bytes(), STATIC_FILES[name])
                if url.path == "/":
                    return self.respond(200, Path(__file__).with_name("chat.html").read_bytes(), "text/html")
                if url.path in ("/favicon.ico", "/favicon.svg"):
                    return self.respond(200, FAVICON_SVG, "image/svg+xml")
                if url.path == "/api/state":
                    lang = (parse_qs(url.query).get("lang") or [None])[0]
                    return self.respond(200, chat.state(lang))
                if url.path == "/api/onboarding":
                    if not self.authorized():
                        return self.respond(403, {"error": "Reload to reconnect.", "kind": "forbidden"})
                    query = parse_qs(url.query)
                    lang = (query.get("lang") or [None])[0]
                    step = (query.get("step") or [None])[0]
                    return self.respond(200, chat.onboarding(lang if lang in ("es", "en") else None, step))
                if url.path == "/profile":
                    return self.respond(200, Path(__file__).with_name("profile.html").read_bytes(), "text/html")
                if self.profile_read(url.path) and profile_reads_need_token() and not self.authorized():
                    return self.respond(403, {"error": "Reload to reconnect.", "kind": "forbidden"})
                if url.path == "/api/profile":
                    lang = (parse_qs(url.query).get("lang") or [None])[0]
                    return self.respond(200, profile_view(WealthService(chat.db), chat.client_id,
                                                          language=lang if lang in {"en", "es"} else None))
                if url.path in SURFACE_GETS:
                    return self.surface_get(url)
                if url.path == "/api/profile/contradictions":
                    return self.respond(200, WealthService(chat.db).contradictions(chat.client_id))
                history = _HISTORY_PATH.match(url.path)
                if history:
                    return self.respond(200, WealthService(chat.db).history(chat.client_id, unquote(history.group(1))))
                fact = _FACT_PATH.match(url.path)
                if fact:
                    return self.respond(200, fact_detail(WealthService(chat.db), chat.client_id, unquote(fact.group(1))))
                view = _VIEW_PATH.match(url.path)
                if view:
                    if not self.authorized():
                        return self.respond(403, {"error": "Reload to reconnect.", "kind": "forbidden"})
                    return self.render_view(view.group(1), view.group(2), url.query)
                if url.path == "/api/orders":
                    if not self.authorized():
                        return self.respond(403, {"error": "Reload to reconnect.", "kind": "forbidden"})
                    return self.orders_list()
                match = _TURN_PATH.match(url.path)
                if match and match.group(2) == "/events":
                    if not self.authorized():
                        return self.respond(403, {"error": "Reload to reconnect.", "kind": "forbidden"})
                    return self.stream(match.group(1), url.query)
            except Exception as exc:  # noqa: BLE001
                return self.handle_failure(exc)
            self.respond(404, {"error": "Not found."})

        @staticmethod
        def profile_read(path):
            return (path in {"/api/profile", "/api/profile/contradictions"}
                    or bool(_HISTORY_PATH.match(path)) or bool(_FACT_PATH.match(path)))

        def render_view(self, view_id, extension, query):
            """A placed view as an image for text channels: SVG always, PNG when Pillow is installed."""
            spec = chat.view(view_id)
            if spec is None:
                return self.respond(404, {"error": "That view is no longer available.", "kind": "gone"})
            lang = (parse_qs(query).get("lang") or ["es"])[0]
            lang = lang if lang in ("es", "en") else "es"
            if extension == "svg":
                return self.respond(200, render_svg(spec, lang).encode(), "image/svg+xml")
            png = render_png(spec, lang) if png_available() else None
            if png is None:
                return self.respond(501, _error_payload("unsupported", "PNG needs Pillow here; use the .svg address."))
            return self.respond(200, png, "image/png")

        def stream(self, turn_id, query):
            turn = chat.turn
            if turn is None or turn.id != turn_id:
                return self.respond(404, {"error": "That response is no longer available.", "kind": "gone"})
            try:
                after = int((parse_qs(query).get("after") or [self.headers.get("Last-Event-ID") or 0])[0])
            except ValueError:
                after = 0
            after = max(0, after)
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("X-Accel-Buffering", "no")
            for name, header in SECURITY_HEADERS:
                self.send_header(name, header)
            self.end_headers()
            try:
                while True:
                    events = turn.wait_events(after, 15)
                    if not events:
                        if turn.finished:
                            break
                        self.wfile.write(b": keepalive\n\n")
                    for event in events:
                        after = event["seq"]
                        self.wfile.write(
                            f"id: {after}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                    if events and events[-1]["type"] == "done":
                        break
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass

        def do_POST(self):
            if not self.authorized():
                return self.respond(403, {"error": "Reload the local chat page to reconnect.", "kind": "forbidden"})
            path = urlsplit(self.path).path
            try:
                if path == "/api/chat":
                    answer = chat.ask(**self.turn_request(self.read_json()))
                    return self.respond(200, {"answer": answer})
                if path == "/api/turns":
                    turn = chat.start(**self.turn_request(self.read_json()))
                    return self.respond(202, {"turn": turn.summary()})
                if path == "/api/onboarding":
                    return self.respond(200, chat.answer_onboarding(self.read_json()))
                if path == "/api/reset":
                    chat.reset()
                    return self.respond(200, chat.state())
                if path == "/api/upload":
                    return self.upload()
                resolve = _CONTRADICTION_PATH.match(path)
                if resolve:
                    body = self.read_json()
                    if body.get("choice") not in {"keep", "use_new", "changed"}:
                        raise ValueError("Choose keep, use_new or changed.")
                    service = WealthService(chat.db)
                    service.resolve_contradiction(chat.client_id, resolve.group(1), body["choice"],
                                                  valid_from=body.get("valid_from"))
                    return self.respond(200, {"profile": profile_view(service, chat.client_id)})
                if path == "/api/today":
                    return self.respond(200, self.today(self.read_json()))
                if path == "/api/profile/form":
                    return self.profile_write(self.read_json(), form=True)
                fact = _FACT_PATH.match(path)
                if fact:
                    return self.profile_write(self.read_json(), key=unquote(fact.group(1)))
                order = _ORDER_PATH.match(path)
                if order:
                    return self.order_action(order.group(1), order.group(2))
                match = _TURN_PATH.match(path)
                if match and match.group(2) == "/cancel":
                    if not chat.cancel(match.group(1)):
                        return self.respond(404, {"error": "That response is no longer running.", "kind": "gone"})
                    return self.respond(202, {"status": "cancelling"})
            except Exception as exc:  # noqa: BLE001 - every failure returns JSON
                return self.handle_failure(exc)
            self.respond(404, {"error": "Not found."})

        def orders_list(self):
            """Recent order tickets for the cards; open orders are refreshed from the broker first."""
            with WealthStore(chat.db) as store:
                _tickets.refresh(store, chat.client_id)
                state = _tickets.execution_status(store, chat.client_id)
                items = _tickets.list_tickets(store, chat.client_id, include_nonce=True)
            return self.respond(200, {"mode": state["mode"], "tickets": items})

        def order_action(self, target, action):
            """The only place an order is placed: the person's tap on the card (token + local origin + nonce)."""
            body = self.read_json() if action == "confirm" else {}
            try:
                with WealthStore(chat.db) as store:
                    if action == "confirm":
                        ticket = _tickets.confirm(store, chat.client_id, target, nonce=body.get("nonce"),
                                                  override=body.get("override", False), typed=body.get("typed"),
                                                  snapshot=store.snapshot(chat.client_id))
                    else:
                        ticket = _tickets.cancel(store, chat.client_id, target)
            except _tickets.ConfirmError as exc:
                return self.respond(exc.status, {"error": str(exc), "kind": exc.kind, "ticket": exc.ticket})
            return self.respond(200, {"ticket": ticket})

        # ---- today lines, quarterly review, connections and export (read views; acks are the only write)

        def today(self, body=None):
            query = parse_qs(urlsplit(self.path).query)
            zone = _valid_timezone((query.get("tz") or [None])[0] or (body or {}).get("timezone"))
            action = item = None
            if body is not None:
                chosen = [name for name in ("dismiss", "snooze", "restore") if name in body]
                if len(chosen) != 1 or not isinstance(body[chosen[0]], str):
                    raise ValueError("Send one of dismiss, snooze or restore with an item id.")
                action, item = chosen[0], body[chosen[0]]
            return today_view(WealthService(chat.db), chat.client_id, action=action, item_id=item, timezone_name=zone)

        def surface_get(self, url):
            if url.path == "/review":
                return self.respond(200, Path(__file__).with_name("review.html").read_bytes(), "text/html")
            if not self.authorized():
                return self.respond(403, {"error": "Reload to reconnect.", "kind": "forbidden"})
            service = WealthService(chat.db)
            if url.path == "/api/today":
                return self.respond(200, self.today())
            if url.path == "/api/review":
                period = (parse_qs(url.query).get("period") or [None])[0]
                return self.respond(200, review_view(service, chat.client_id, period,
                                                     inputs=getattr(chat, "review_inputs", None)))
            if url.path == "/api/connections":
                return self.respond(200, connections_view(service, chat.client_id))
            if url.path == "/api/tax-pack":
                return self.tax_pack(service, parse_qs(url.query))
            data = json.dumps(export_payload(service, chat.client_id), default=str, indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="wealth-export.json"')
            self.send_header("Content-Length", str(len(data)))
            for name, header in SECURITY_HEADERS:
                self.send_header(name, header)
            self.end_headers()
            self.wfile.write(data)

        def tax_pack(self, service, query):
            """The annual tax pack: ?year=YYYY&format=json|html|csv&section=<id>&lang=es|en (read only)."""
            from . import taxpack
            year = (query.get("year") or [None])[0]
            fmt = (query.get("format") or ["json"])[0]
            lang = (query.get("lang") or [None])[0]
            inputs = {}
            if year is not None:
                if not re.fullmatch(r"20\d\d", year):
                    raise ValueError("Send year as YYYY.")
                inputs["tax_year"] = int(year)
            if lang is not None:
                if lang not in ("es", "en"):
                    raise ValueError("Send lang as es or en.")
                inputs["language"] = lang
            if fmt not in ("json", "html", "csv"):
                raise ValueError("Send format as json, html or csv.")
            report = service.run("tax_pack", inputs=inputs, client_id=chat.client_id)
            report.pop("views", None)
            if fmt == "json":
                return self.respond(200, report)
            label = (report.get("result") or {}).get("tax_year")
            if fmt == "html":
                data, name, kind = taxpack.render_html(report, lang).encode(), f"tax-pack-{label}.html", "text/html"
            else:
                files = taxpack.csv_files(report, lang)
                section = (query.get("section") or ["pendientes"])[0]
                name = f"tax-pack-{label}-{section}.csv"
                if name not in files:
                    raise ValueError("No such section; use one of the section ids.")
                data, kind = files[name].encode(), "text/csv"
            self.send_response(200)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            for header, value in PRINTABLE_SECURITY_HEADERS if fmt == "html" else SECURITY_HEADERS:
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(data)

        def profile_write(self, body, *, key=None, form=False):
            service = WealthService(chat.db)
            snapshot = service.inspect(chat.client_id)
            if form:
                facts = form_facts(snapshot, body.get("form"))
                if not facts:
                    raise ValueError("Nothing to save.")
            else:
                action = body.get("action")
                if action not in {"edit", "confirm", "delete"}:
                    raise ValueError("Choose edit, confirm or delete.")
                facts = fact_action(snapshot, key, action, field=body.get("field"), value=body.get("value"))
            revision = body.get("expected_revision")
            service.remember(chat.client_id, facts, revision if isinstance(revision, int) else None)
            return self.respond(200, {"profile": profile_view(service, chat.client_id)})

        def upload(self):
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if length > MAX_UPLOAD_BYTES:
                self.close_connection = True
                return self.respond(413, _error_payload("invalid", "Files must be under 25 MB."))
            self.close_connection = True  # a rejected body may be left unread
            meta = chat.uploads.save(self.headers.get("X-Filename", ""), self.headers.get_content_type(),
                                     self.rfile, length)
            return self.respond(201, {"upload": meta})

    return Server((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Wealth browser chat")
    parser.add_argument("--client", default="personal")
    parser.add_argument("--model", default=_agent.DEFAULT_MODEL,
                        help="Codex model: default (your Codex config), sol, luna or a full model ID")
    parser.add_argument("--db")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1",
                        help="loopback address to bind (default: 127.0.0.1)")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--web-search", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ephemeral", action="store_true",
                        help="do not keep a Codex session between turns (no resume)")
    parser.add_argument("--service-tier", choices=_agent.SERVICE_TIERS, default=None,
                        help="fast: Codex priority processing, quicker answers at a higher cost "
                             "(default: your account's tier; also WEALTH_SERVICE_TIER=fast)")
    args = parser.parse_args(argv)
    if args.service_tier:
        _agent.set_service_tier(args.service_tier)
    db = database_path(args.db)
    if args.demo:
        if args.db is None:
            db = db.parent / "agent-demo.sqlite3"
        args.client = "fictional-demo"
        seed_demo(db, args.client)
    chat = Chat(db, args.client, args.model, args.web_search, ephemeral=args.ephemeral)
    server = create_server(chat, args.port, args.host)
    shown = "[::1]" if args.host == "::1" else "127.0.0.1"
    print(f"Wealth chat: http://{shown}:{server.server_port} · {chat.display_name}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        turn = chat.turn
        if turn is not None and not turn.finished:
            turn.control.cancel()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
