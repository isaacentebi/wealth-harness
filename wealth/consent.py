"""Consent and provenance evidence taken from the person's own words, not the model's.

The launcher (``agent.py``) passes the person's current message, and a bounded
slice of their recent messages, into the Wealth MCP server's environment for
one turn. The model cannot change that environment, so these checks bind the
operations that save or settle something (confirming a proposal, answering a
contradiction, accepting a decision) and the "the person said it" label on a
fact to what the person actually typed.

Environment (set per turn by the launcher; see ``turn_env``):

- ``WEALTH_TURN_SESSION``: ``chat`` for a conversation turn, ``memory`` for the
  after-reply memory step. Unset for direct CLI/service use and third-party hosts.
- ``WEALTH_TURN_MESSAGE_B64``: base64 (UTF-8) of the person's current message;
  empty when the turn's request came from Wealth itself (the setup reveal).
- ``WEALTH_TURN_RECENT_B64``: base64 of the person's recent earlier messages.
- ``WEALTH_REQUIRE_TURN_CONSENT=1``: opt-in for third-party MCP hosts that set no
  turn environment: the consent operations then fail closed. Without it, a
  host with no turn environment is responsible for consent itself.
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SESSION_ENV = "WEALTH_TURN_SESSION"
MESSAGE_ENV = "WEALTH_TURN_MESSAGE_B64"
RECENT_ENV = "WEALTH_TURN_RECENT_B64"
REQUIRE_ENV = "WEALTH_REQUIRE_TURN_CONSENT"
SESSIONS = ("chat", "memory")
MAX_MESSAGE_CHARS = 12_000
MAX_RECENT_CHARS = 6_000
MAX_RECENT_MESSAGES = 6
CONSENT_TOOLS = frozenset({"wealth_ingest", "wealth_resolve_contradiction", "wealth_decision"})
"""MCP tools with operations that need the person's own yes; the memory step is never given them."""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def turn_env(session: str, message: str, recent: Iterable[str] = ()) -> dict[str, str]:
    """The per-turn environment for the Wealth MCP server (bounded)."""
    if session not in SESSIONS:
        raise ValueError(f"session must be one of {SESSIONS}")
    kept: list[str] = []
    size = 0
    for text in reversed([str(t) for t in recent if str(t).strip()][-MAX_RECENT_MESSAGES:]):
        if size + len(text) > MAX_RECENT_CHARS:
            text = text[: max(0, MAX_RECENT_CHARS - size)]
        if not text:
            break
        kept.append(text)
        size += len(text)
    return {
        SESSION_ENV: session,
        MESSAGE_ENV: _b64(str(message or "")[:MAX_MESSAGE_CHARS]),
        RECENT_ENV: _b64("\n".join(reversed(kept))),
    }


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")[:MAX_MESSAGE_CHARS]
    except (binascii.Error, UnicodeError, ValueError):
        return ""


@dataclass(frozen=True)
class Turn:
    """What the launcher said about this turn; ``session`` is None outside a Wealth turn."""

    session: str | None
    message: str
    recent: str
    require: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Turn":
        session = environ.get(SESSION_ENV) or None
        if session is not None and session not in SESSIONS:
            session = "chat"  # an unknown label is treated as the strictest person-facing turn
        return cls(session=session, message=_decode(environ.get(MESSAGE_ENV)),
                   recent=_decode(environ.get(RECENT_ENV))[:MAX_RECENT_CHARS],
                   require=environ.get(REQUIRE_ENV) == "1")

    @property
    def bound(self) -> bool:
        """Whether consent must come from the person's message (a Wealth turn, or the host opted in)."""
        return self.session is not None or self.require


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
# Affirmatives accepted anywhere in the message.
_AFFIRMATIVES = ("dale", "ok", "okay", "orale", "claro", "guardalo", "guardala", "guardalos", "guardalas",
                 "guarda", "confirmo", "confirmado", "de acuerdo", "adelante", "correcto", "perfecto",
                 "yes", "yeah", "yep", "sure", "save it", "save them", "go ahead", "confirm", "confirmed",
                 "do it", "hazlo", "sounds good", "looks good", "that's right", "thats right", "va que va",
                 "si por favor", "si porfa", "si guardalo", "si confirmo")
# Short words that are only a yes when they open the reply ("si gano mas..." is "if", not "yes").
_OPENERS = ("si", "va", "simon", "yes", "ok")


def is_affirmative(message: str) -> bool:
    """An explicit yes in Spanish or English, with no negation anywhere in the message."""
    text = _fold(message)
    if not text or _has(text, _NEGATIONS):
        return False
    if re.search(r"(?<!\w)sí(?!\w)", unicodedata.normalize("NFC", str(message).lower())):
        return True  # an accented sí is always a yes
    if _has(text, _AFFIRMATIVES):
        return True
    # "si", "va": a yes only as the whole reply or set off by punctuation ("si, guárdalo"), never "si gano...".
    return re.match(r"^[¡!\s]*(?:" + "|".join(_OPENERS) + r")\s*(?:[,.!;]|$)", text) is not None


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
    """Whether the person's words support this contradiction answer."""
    phrases = _CHOICES.get(choice)
    return bool(phrases) and _has(_fold(message), phrases)


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


# What people say in chat is memory, and the model restates it: "1.2 millones al año" is saved as a
# monthly 100,000, "tengo 34" as a birth year, "80k en GBM y 20k en Nu" as a 100,000 total. Those are
# still the person's figures. A number they never said, or cannot be read from what they said, stays
# an inference.
_PERIODS = (2, 4, 12, 24, 26, 52, 365)
_MAX_SUMMED = 12


def _restatements(said: set[float]) -> set[float]:
    import datetime as _dt
    year = _dt.date.today().year
    base = {abs(s) for s in said}
    out = set(base)
    for s in base:
        out.update({s * 100, s / 100})  # a share written either way: 30 (%) and 0.3
        for k in _PERIODS:
            out.update({s * k, s / k})
        if 0 < s < 120 and float(s).is_integer():  # an age gives a birth year
            out.update({year - s, year - s - 1})
    figures = sorted((s for s in base if s >= 100), reverse=True)[:_MAX_SUMMED]
    for i, a in enumerate(figures):
        for b in figures[i + 1:]:
            out.update({a + b, a - b})
    return out
