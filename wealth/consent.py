"""Consent and provenance evidence taken from the person's own words, not the model's.

The launcher (``agent.py``) hands the Wealth MCP server the person's current
message, and a bounded slice of their recent messages, for one turn. The model
cannot change that evidence, so these checks bind the operations that save or
settle something (confirming a proposal, answering a contradiction, accepting a
decision) and the "the person said it" label on a fact to what the person
actually typed.

Environment of the MCP server (set per turn by the launcher; see ``TurnFile``):

- ``WEALTH_TURN_SESSION``: ``chat`` for a conversation turn, ``memory`` for the
  after-reply memory step. Unset for direct CLI/service use and third-party hosts.
- ``WEALTH_TURN_FILE``: path of a 0600 JSON file ``{"message", "recent"}`` with the
  person's words for this turn (``message`` is empty when the request came from
  Wealth itself, the setup reveal). The words never travel in argv or the
  environment, which other local processes can read; the launcher deletes the
  file when the turn ends.

A host that sets no turn session gets a two-step confirmation (``Confirmations``):
the first call returns ``needs_person`` with a short summary and a one-time code
the host must show the person, and only a second call with ``confirm=true`` and
that code completes it. Pending codes are kept in the database, so a host that
starts a new server process for each turn can still finish them. A host that
confirms natively sets
``WEALTH_HOST_HANDLES_CONSENT=1`` to skip the second step;
``WEALTH_REQUIRE_TURN_CONSENT=1`` makes these operations fail closed instead.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

SESSION_ENV = "WEALTH_TURN_SESSION"
TURN_FILE_ENV = "WEALTH_TURN_FILE"
REQUIRE_ENV = "WEALTH_REQUIRE_TURN_CONSENT"
HOST_ENV = "WEALTH_HOST_HANDLES_CONSENT"
SESSIONS = ("chat", "memory")
MAX_MESSAGE_CHARS = 12_000
MAX_RECENT_CHARS = 6_000
MAX_RECENT_MESSAGES = 6
MAX_TURN_FILE_BYTES = 256 * 1024
CONSENT_TOOLS = frozenset({"wealth_ingest", "wealth_resolve_contradiction", "wealth_decision"})
"""MCP tools with operations that need the person's own yes; the memory step is never given them."""


def _bounded_recent(recent: Iterable[str]) -> str:
    kept: list[str] = []
    size = 0
    for text in reversed([str(t) for t in recent if str(t).strip()][-MAX_RECENT_MESSAGES:]):
        if size + len(text) > MAX_RECENT_CHARS:
            text = text[: max(0, MAX_RECENT_CHARS - size)]
        if not text:
            break
        kept.append(text)
        size += len(text)
    return "\n".join(reversed(kept))


class TurnFile:
    """The person's words for one turn, in a private file the MCP server reads when it starts.

    ``env`` goes to the MCP server. ``close()`` (or leaving a ``with`` block)
    deletes the file; the launcher does so when the turn ends.
    """

    def __init__(self, session: str, message: str, recent: Iterable[str] = ()):
        if session not in SESSIONS:
            raise ValueError(f"session must be one of {SESSIONS}")
        self._dir = Path(tempfile.mkdtemp(prefix="wealth-turn-"))  # 0700 in the per-user temp dir
        self.path = self._dir / "turn.json"
        payload = json.dumps({"message": str(message or "")[:MAX_MESSAGE_CHARS], "recent": _bounded_recent(recent)},
                             ensure_ascii=False).encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        self.env = {SESSION_ENV: session, TURN_FILE_ENV: str(self.path)}

    def close(self) -> None:
        for remove in (self.path.unlink, self._dir.rmdir):
            try:
                remove()
            except OSError:
                pass

    def __enter__(self) -> "TurnFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def turn_env(session: str, message: str, recent: Iterable[str] = ()) -> TurnFile:
    """Write this turn's evidence file: pass ``.env`` to the MCP server and ``.close()`` it after the turn."""
    return TurnFile(session, message, recent)


def _read_turn_file(path: str | None) -> tuple[str, str]:
    """(message, recent) from a turn file this user owns with no group/other access; ("", "") otherwise."""
    if not path:
        return "", ""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return "", ""
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_TURN_FILE_BYTES):
            return "", ""
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = json.loads(handle.read(MAX_TURN_FILE_BYTES).decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return "", ""
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return "", ""
    message, recent = data.get("message"), data.get("recent")
    return (message[:MAX_MESSAGE_CHARS] if isinstance(message, str) else "",
            recent[:MAX_RECENT_CHARS] if isinstance(recent, str) else "")


@dataclass(frozen=True)
class Turn:
    """What the launcher said about this turn; ``session`` is None outside a Wealth turn."""

    session: str | None
    message: str
    recent: str
    require: bool
    host_handles: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Turn":
        session = environ.get(SESSION_ENV) or None
        if session is not None and session not in SESSIONS:
            session = "chat"  # an unknown label is treated as the strictest person-facing turn
        message, recent = _read_turn_file(environ.get(TURN_FILE_ENV)) if session else ("", "")
        return cls(session=session, message=message, recent=recent,
                   require=environ.get(REQUIRE_ENV) == "1", host_handles=environ.get(HOST_ENV) == "1")

    @property
    def bound(self) -> bool:
        """Whether consent must come from the person's message (a Wealth turn, or the host opted in)."""
        return self.session is not None or self.require


# --------------------------------------------------------------------------- two-step confirmation

CODE_TTL_SECONDS = 600
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L


def digest_of(value: Any) -> str:
    """A stable hash of what a confirmation covers: tool, target, arguments and the stored record."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _code_hash(code: str, digest: str = "") -> str:
    """Salted with what the code covers, so a stored hash is not a lookup table of codes."""
    return hashlib.sha256((digest + ":" + re.sub(r"[^A-Z0-9]", "", str(code).upper())).encode()).hexdigest()


class Confirmations:
    """One-time codes for hosts that set no turn session.

    ``issue(digest, client)`` returns a fresh code bound to that digest, replacing
    any earlier one. ``redeem(digest, code, client)`` succeeds once, within the
    TTL, and only while what the code covered is unchanged (same digest). Any
    redeem attempt uses the pending code up, and a wrong code voids it, so it
    cannot be guessed. Only a salted hash of each code is kept.

    ``persist(client, update)`` (optional) applies ``update`` to that client's
    pending codes atomically and durably (the ``consent`` auxiliary namespace of
    the database). With it a code issued by one server process can be redeemed by
    the next, so hosts that start the MCP server for each turn can finish the two
    steps. Without it the codes live only in this process.
    """

    def __init__(self, ttl: float = CODE_TTL_SECONDS, clock: Callable[[], float] = time.time,
                 persist: Callable[[str, Callable[[dict], dict]], Any] | None = None):
        self._ttl, self._clock, self._persist = ttl, clock, persist
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _apply(self, client: str | None, update: Callable[[dict], dict]) -> None:
        if self._persist is not None and client:
            self._persist(client, update)
            return
        with self._lock:
            key = str(client or "")
            self._pending[key] = update(dict(self._pending.get(key) or {}))

    def issue(self, digest: str, client: str | None = None) -> str:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        now = self._clock()

        def update(pending: dict) -> dict:
            kept = {d: e for d, e in pending.items() if isinstance(e, dict) and float(e.get("expires") or 0) > now}
            kept[digest] = {"code_hash": _code_hash(code, digest), "expires": now + self._ttl, "client": client}
            return kept

        self._apply(client, update)
        return f"{code[:3]}-{code[3:]}"

    def redeem(self, digest: str, code: Any, client: str | None = None) -> bool:
        if not isinstance(code, str) or not code.strip():
            return False
        now = self._clock()
        outcome = [False]

        def update(pending: dict) -> dict:
            pending = dict(pending)
            entry = pending.pop(digest, None)
            if not isinstance(entry, dict):
                # The code was issued for something that has changed since (or never existed): void it.
                for other, item in list(pending.items()):
                    if isinstance(item, dict) and hmac.compare_digest(str(item.get("code_hash")),
                                                                      _code_hash(code, other)):
                        del pending[other]
                return pending
            outcome[0] = (float(entry.get("expires") or 0) > now and entry.get("client") == client
                          and hmac.compare_digest(str(entry.get("code_hash")), _code_hash(code, digest)))
            return pending  # single use: right or wrong, the pending code is gone

        self._apply(client, update)
        return outcome[0]


# --------------------------------------------------------------------------- words


def _fold(text: str) -> str:
    """Lowercase, accents removed, curly quotes straightened, whitespace collapsed."""
    text = unicodedata.normalize("NFKD", str(text or "").replace("’", "'").replace("‘", "'"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return " ".join(text.split())


def _has(text: str, phrases: Iterable[str]) -> bool:
    return any(re.search(r"(?<![\w'])" + re.escape(p) + r"(?![\w'])", text) for p in phrases)


# Negations win over any affirmative in the same message.
_NEGATIONS = ("no", "nop", "nel", "todavia no", "aun no", "aun no", "mejor no", "espera", "esperate",
              "no lo guardes", "cancela", "nunca", "don't", "dont", "do not", "not yet", "nope", "wait",
              "never", "cancel", "hold on", "stop")
# A condition or a "first..." means the yes is not the point of the message.
_HEDGES = ("pero", "but", "primero", "first", "antes", "before", "later", "mas tarde", "unless", "a menos",
           "except", "excepto", "sin embargo", "however", "maybe", "quizas", "tal vez", "i think", "creo que",
           "not sure", "depends", "depende")
# Explicit instructions to save: a yes in a message of any length (with no question, negation or hedge).
_SAVE_VERBS = ("guardalo", "guardala", "guardalos", "guardalas", "guardarlo", "guardarla", "save it", "save them",
               "save that", "save this", "please save", "go ahead and save", "confirmalo", "confirmala",
               "confirm it", "confirm that", "confirm this", "confirmo")
# A short reply is a yes only when every word is consent vocabulary ("sí, guárdalo", "yes please",
# "claro que sí"): never "confirm what you see in the file" or "si gano más".
_YES_WORDS = frozenset("""
    si yes yeah yep yup sure ok okay dale va orale claro de acuerdo adelante perfecto correcto simon go
    ahead sounds looks good great fine that's thats right do it that this them hazlo confirmo confirmado
    confirmed confirm confirma confirmalo confirmala please por favor porfa porfavor gracias thanks thank you
    guardalo guardala guardalos guardalas guarda guardarlo save lets let's esta bien listo exacto eso all
    alright absolutely of course supuesto y and vale hecho done agreed sale perfect correct exactly que todo
    asi ya muchas it's its is es
""".split())
_STRONG_YES = frozenset("""
    si yes yeah yep yup sure ok okay dale va orale claro acuerdo adelante perfecto correcto simon ahead
    hazlo confirmo confirmado confirmed confirm confirma confirmalo confirmala guardalo guardala guardalos
    guardalas guarda guardarlo save listo exacto alright absolutely supuesto course vale hecho done agreed
    sale perfect correct exactly good right
""".split())
_MAX_SHORT_WORDS = 8


def is_affirmative(message: str) -> bool:
    """An explicit yes in Spanish or English whose main point is the yes.

    Never with a question, a negation or a condition ("sure, but first..."). Either a
    short reply (at most eight words) made only of consent words ("sí, guárdalo",
    "yes", "dale"), or an explicit instruction to save ("guárdalo", "save it").
    """
    raw = str(message or "")
    text = _fold(raw)
    if not text or "?" in raw or "¿" in raw or _has(text, _NEGATIONS) or _has(text, _HEDGES):
        return False
    words = re.findall(r"[a-z']+", text)
    if not words:
        return False
    if (len(words) <= _MAX_SHORT_WORDS and all(w in _YES_WORDS for w in words)
            and any(w in _STRONG_YES for w in words)):
        return True
    return _has(text, _SAVE_VERBS)


_CHOICES = {
    "keep": ("keep", "keep mine", "mine", "my number", "my figure", "what i said", "what i told you",
             "i was right", "leave it", "mantenlo", "manten", "mantener", "conserva", "conservalo",
             "el mio", "la mia", "lo mio", "lo que dije", "lo que te dije", "dejalo", "deja el mio",
             "sigue igual", "sigue siendo", "esta bien el mio"),
    "use_new": ("use the new", "the new one", "new figure", "new number", "statement is right",
                "document is right", "i was wrong", "update it", "usa el nuevo", "usa la nueva", "el nuevo",
                "la nueva", "el del estado", "lo del estado", "actualiza", "actualizalo", "me equivoque",
                "estaba mal", "tiene razon", "usa ese", "usa esa"),
    "changed": ("changed", "it changed", "has changed", "went up", "went down", "got a raise", "raise",
                "now it's", "now it is", "cambio", "ya cambio", "subio", "bajo", "me subieron", "me bajaron",
                "aumento", "ahora es", "ahora gano", "ahora tengo"),
}


def matches_choice(message: str, choice: str) -> bool:
    """Whether the person's words support this contradiction answer (a question never does)."""
    phrases = _CHOICES.get(choice)
    raw = str(message or "")
    return bool(phrases) and "?" not in raw and "¿" not in raw and _has(_fold(raw), phrases)


# The person saying they placed a place-it-yourself order at their broker ("Ya la puse").
_PLACED = (
    "ya la puse", "ya lo puse", "ya las puse", "ya los puse", "la puse", "lo puse", "ya quedo puesta",
    "ya quedo puesto", "ya esta puesta", "ya esta puesto", "ya la coloque", "ya lo coloque", "ya las coloque",
    "ya los coloque", "la coloque", "ya la meti", "ya lo meti", "ya la mande", "ya lo mande", "ya la hice",
    "ya compre", "ya lo compre", "ya la compre", "ya los compre", "ya las compre", "ya vendi", "ya lo vendi",
    "ya la vendi", "ya los vendi", "ya las vendi", "ya quedo la orden", "ya puse la orden", "puse la orden",
    "coloque la orden", "ya coloque la orden", "ya hice la compra", "ya hice la venta",
    "i placed it", "i placed the order", "i placed them", "i placed the orders", "placed it", "i've placed it",
    "ive placed it", "i have placed it", "i put it in", "i put the order in", "i submitted it", "order placed",
    "it's placed", "its placed", "i bought it", "i bought them", "i sold it", "i sold them", "done, placed",
    "just placed it", "i just placed it",
)


def says_placed(message: str) -> bool:
    """Whether the person's own message says they placed the order themselves ("ya la puse", "I placed it").

    Never with a question, a negation or a condition ("todavía no la pongo", "ya la puse, pero a otro precio").
    """
    raw = str(message or "")
    text = _fold(raw)
    if not text or "?" in raw or "¿" in raw or _has(text, _NEGATIONS) or _has(text, _HEDGES) \
            or _has(text, _NOT_PLACED):
        return False
    return _has(text, _PLACED)


_NOT_PLACED = ("haven't", "havent", "have not", "hasn't", "hasnt", "didn't", "didnt", "did not", "won't", "wont",
               "will", "going to", "gonna", "voy a", "la pongo", "lo pongo", "la voy", "lo voy", "manana", "tomorrow",
               "yet", "aun", "todavia", "falta", "pendiente")


# --------------------------------------------------------------------------- numbers

_NUMBER_WORDS = {
    "cero": 0, "zero": 0, "un": 1, "uno": 1, "una": 1, "one": 1, "dos": 2, "two": 2, "tres": 3, "three": 3,
    "cuatro": 4, "four": 4, "cinco": 5, "five": 5, "seis": 6, "six": 6, "siete": 7, "seven": 7,
    "ocho": 8, "eight": 8, "nueve": 9, "nine": 9, "diez": 10, "ten": 10, "once": 11, "eleven": 11,
    "doce": 12, "twelve": 12, "quince": 15, "fifteen": 15, "veinte": 20, "twenty": 20, "treinta": 30,
    "thirty": 30, "cuarenta": 40, "forty": 40, "cincuenta": 50, "fifty": 50, "cien": 100, "hundred": 100,
    "medio": 0.5, "half": 0.5,
}
_ZERO_WORDS = ("no", "nada", "ninguna", "ninguno", "none", "nothing", "sin", "without", "zero", "cero")
_SCALES = {"k": 1e3, "mil": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "millon": 1e6, "millones": 1e6,
           "million": 1e6, "millions": 1e6, "mdp": 1e6, "b": 1e9, "billion": 1e9}
_NUMERAL = re.compile(r"(?<![\w.])(\d{1,3}(?:[,.\s]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*"
                      r"(k|mil|thousand|mm|m|millon(?:es)?|millions?|mdp|b|billion)?(?![\w])")
_WORD_SCALE = re.compile(r"(?<![\w])(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\s+"
                         r"(mil|thousand|millon(?:es)?|millions?)(?![\w])")
_WORD = re.compile(r"(?<![\w])(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")(?![\w])")


def _readings(token: str) -> set[float]:
    """Every plausible value of a numeral: 85,000 / 85.000 / 1.5 / 1,5 / 85 000."""
    out: set[float] = set()
    compact = token.replace(" ", "")
    for thousands, decimal in ((",", "."), (".", ",")):
        candidate = compact.replace(thousands, "").replace(decimal, ".")
        try:
            out.add(float(candidate))
        except ValueError:
            pass
    return out


def numbers_in(text: str) -> set[float]:
    """Numbers the person wrote, normalised: "85 mil", "$85,000" and "85k" all give 85000."""
    folded = _fold(text)
    found: set[float] = set()
    for match in _NUMERAL.finditer(folded):
        scale = _SCALES.get(match.group(2) or "", 1.0)
        for value in _readings(match.group(1)):
            found.add(value)
            found.add(value * scale)
    for match in _WORD_SCALE.finditer(folded):
        found.add(_NUMBER_WORDS[match.group(1)] * _SCALES[match.group(2)])
    for match in _WORD.finditer(folded):
        found.add(float(_NUMBER_WORDS[match.group(1)]))
    if re.search(r"(?<![\w])(mil|thousand)(?![\w])", folded):
        found.add(1e3)
    if re.search(r"(?<![\w])(millon|million)(?![\w])", folded):
        found.add(1e6)
    if _has(folded, _ZERO_WORDS):
        found.add(0.0)
    return found


_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def value_numbers(value: Any) -> list[float]:
    """Numeric leaves of a fact value (and strings that are just a number); dates are not figures."""
    out: list[float] = []
    if isinstance(value, bool) or value is None:
        return out
    if isinstance(value, (int, float)):
        out.append(float(value))
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if text and not _DATE.match(text) and re.fullmatch(r"[-+]?\$?\d+(\.\d+)?", text):
            out.append(float(text.replace("$", "")))
    elif isinstance(value, Mapping):
        for item in value.values():
            out.extend(value_numbers(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(value_numbers(item))
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-9, 1e-6 * max(abs(a), abs(b)))


def supported(value: Any, texts: Iterable[str]) -> list[float]:
    """Numbers in ``value`` the person's texts do not contain (empty: fully supported).

    A share may be written either way: 30 (%) supports 0.3 and 0.3 supports 30.
    """
    said: set[float] = set()
    for text in texts:
        said |= numbers_in(text)
    restated = _restatements(said)
    missing = []
    for number in value_numbers(value):
        n = abs(number)
        if any(_close(n, s) for s in restated):
            continue
        missing.append(number)
    return missing


_NOT_FIGURES = frozenset({"provenance", "extraction_request", "confirmation", "field_confidence", "proposal_id",
                          "verification", "review_reasons"})


def document_figures(result: Any) -> set[float]:
    """The figures a stored proposal actually holds (balances, positions, lines), without file metadata."""
    found: set[float] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key not in _NOT_FIGURES:
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            text = value.strip().replace(",", "")
            if re.fullmatch(r"[-+]?\d+(\.\d+)?", text) and not _DATE.match(text):
                found.add(abs(float(text)))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            found.add(abs(float(value)))

    walk(result)
    return found


def _near(a: float, b: float) -> bool:
    # A statement figure may be restated rounded to the unit (38,601.05 as 38,601, or 3,216.75 a month), no more.
    largest = max(abs(a), abs(b))
    return abs(a - b) <= (0.51 if largest >= 100 else max(1e-9, 0.005 * largest))


def _document_restatements(figures: set[float]) -> set[float]:
    """A statement's figures, per period (x or / 2..365), as shares (only small ones) and pairwise sums."""
    base = {abs(f) for f in figures}
    out = set(base)
    for f in base:
        for k in _PERIODS:
            out.update({f * k, f / k})
        if f <= 100:
            out.update({f * 100, f / 100})  # a rate written 4.5 or 0.045
    top = sorted((f for f in base if f >= 100), reverse=True)[:_MAX_SUMMED]
    for i, a in enumerate(top):
        for b in top[i + 1:]:
            out.update({a + b, a - b})
    return out


def ungrounded(value: Any, figures: Iterable[float]) -> list[float]:
    """Numbers in ``value`` that the document's figures (or plain restatements of them) do not contain."""
    restated = _document_restatements(set(figures))
    missing = []
    for number in value_numbers(value):
        n = abs(number)
        if n in restated:  # a figure copied as printed (a 1099-B can list thousands of lots)
            continue
        if not any(_near(n, f) for f in restated):
            missing.append(number)
    return missing


# What people say in chat is memory, and the model restates it: "1.2 millones al año" is saved as a
# monthly 100,000, "tengo 34" as a birth year, "80k en GBM y 20k en Nu" as a 100,000 total. Those are
# still the person's figures. A number they never said, or cannot be read from what they said, stays
# an inference.
_PERIODS = (2, 4, 6, 12, 24, 26, 52, 365)  # times a year: semestral ... quincenal (24), catorcenal (26), semanal
_MAX_SUMMED = 12


def _restatements(said: set[float]) -> set[float]:
    import datetime as _dt
    year = _dt.date.today().year
    base = {abs(s) for s in said}
    out = set(base)
    for s in base:
        out.update({s * 100, s / 100})  # a share written either way: 30 (%) and 0.3
        for k in _PERIODS:
            out.update({s * k, s / k, s * k / 12, s * 12 / k})  # per year, and per month ("1,000 a la semana")
        if 0 < s < 120 and float(s).is_integer():  # an age gives a birth year
            out.update({year - s, year - s - 1})
    figures = sorted((s for s in base if s >= 100), reverse=True)[:_MAX_SUMMED]
    for i, a in enumerate(figures):
        for b in figures[i + 1:]:
            out.update({a + b, a - b})
    return out
