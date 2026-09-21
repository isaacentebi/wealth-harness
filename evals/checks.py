"""Deterministic conversation-quality checks (no model calls).

Each check looks for one observable failure of the intended voice: leaked
internals, boilerplate, filler, structure the turn did not earn, LaTeX, too
many questions, the wrong language, the wrong length, repeated questions,
unsourced market claims and scenario-specific must/must-not patterns.

``run_checks`` returns findings with severity ``fail`` (a defect a reader would
notice) or ``warn`` (a likely defect worth a look). Expectations come from the
scenario; every one has a conservative default so saved transcripts without a
scenario can still be checked.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # "fail" | "warn"
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _rx(*patterns: str, flags: int = re.IGNORECASE) -> list[re.Pattern[str]]:
    return [re.compile(pattern, flags) for pattern in patterns]


# --- text helpers -----------------------------------------------------------

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL = re.compile(r"https?://[^\s)>\]]+")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)


def has_url(text: str) -> bool:
    return bool(_URL.search(text))


def prose(text: str) -> str:
    """Text a reader reads: link labels kept, URLs and code spans removed."""

    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    return _CODE_SPAN.sub(" ", text)


def words(text: str) -> list[str]:
    return _WORD.findall(prose(text))


def word_count(text: str) -> int:
    return len(words(text))


def _snippet(text: str, match: re.Match[str], width: int = 30) -> str:
    start = max(0, match.start() - width)
    return " ".join(text[start:match.end() + width].split())


def _first(patterns: Iterable[re.Pattern[str]], text: str) -> re.Match[str] | None:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found
    return None


# --- language ---------------------------------------------------------------

_EN = frozenset(
    "the and to of you your is are that it for with on this be have what how "
    "would should can will not or if but about from my i we they there "
    "which when".split()
)
_ES = frozenset(
    "el la los las de del que y en tu tus es son para con por una un lo más "
    "pero como si mi qué cómo cuánto cuál esto eso está están hay sus "
    "también ya muy te al".split()
)


def detect_language(text: str) -> str | None:
    """Return "en", "es" or None when the text is too short or mixed to call."""

    tokens = [token.lower() for token in words(text)]
    if len(tokens) < 3:
        return None
    en = sum(token in _EN for token in tokens)
    es = sum(token in _ES for token in tokens)
    if max(en, es) < 2 or en == es:
        return None
    return "en" if en > es else "es"


# --- markdown shape ---------------------------------------------------------

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.MULTILINE)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+\S")
_BOLD = re.compile(r"\*\*[^*\n]+?\*\*|__[^_\n]+?__")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*\|", re.MULTILINE)


def shape(text: str) -> dict[str, int]:
    lines = text.splitlines()
    blocks = items = 0
    in_list = False
    for index, line in enumerate(lines):
        if _LIST_ITEM.match(line):
            items += 1
            if not in_list:
                blocks += 1
            in_list = True
        elif not line.strip():
            following = next((l for l in lines[index + 1:] if l.strip()), "")
            in_list = in_list and bool(_LIST_ITEM.match(following))
        elif not line.startswith((" ", "\t")):
            in_list = False
    return {
        "words": word_count(text),
        "headings": len(_HEADING.findall(text)),
        "list_blocks": blocks,
        "list_items": items,
        "bold": len(_BOLD.findall(text)),
        "tables": len(_TABLE_RULE.findall(text)),
    }


# --- questions --------------------------------------------------------------

_SENTENCE = re.compile(r"[^.!?\n]*\?", re.UNICODE)
_QUOTED = re.compile(r"“[^”\n]*”|\"[^\"\n]*\"|«[^»\n]*»")


def questions(text: str) -> list[str]:
    """Questions put to the reader; quoted rhetorical questions are not counted."""

    body = _QUOTED.sub(" ", _CODE_SPAN.sub(" ", _URL.sub(" ", _MD_LINK.sub(r"\1", text))))
    return [q.strip(" ¿*_-\t") for q in _SENTENCE.findall(body) if q.strip(" ?¿")]


_NON_ANSWER = re.compile(
    r"^\W*(?:mm+|hm+|ok(?:ay)?|k|sure|yeah|thanks|thank you|cool|got it|gracias|va|vale|ajá|aja|ok gracias)\W*$|"
    r"\b(?:rather not|prefer not|don'?t want to (?:say|share)|not (?:going to|gonna) (?:say|share)|skip (?:that|it)|"
    r"prefiero no|no (?:quiero|te voy a) decir)\b",
    re.IGNORECASE,
)
_STOP = frozenset(
    "that this with what your have from would about there their which when them they will "
    "could should into also just like much many some more than then were been being does "
    "para como pero este esta esto tiene tienes quieres sobre cuánto cuanto donde dónde "
    "algún alguna algo".split()
)


def _content(text: str) -> set[str]:
    return {w.lower() for w in words(text) if len(w) >= 4 and w.lower() not in _STOP}


def repeats_unanswered_question(response: str, user: str, history: Sequence[Sequence[str]]) -> bool:
    """True when the person gave a non-answer and the reply asks the same thing again."""

    if not history or not _NON_ANSWER.search(user or ""):
        return False
    previous = next((text for role, text in reversed(history) if role == "assistant"), "")
    prior = set().union(*(_content(q) for q in questions(previous))) if questions(previous) else set()
    if len(prior) < 3:
        return False
    overlap = prior & _content(response)
    return len(overlap) >= 3 and len(overlap) / len(prior) >= 0.5


# --- pattern tables ---------------------------------------------------------

LEAKS = _rx(
    r"\bwealth_[a-z_]+\b",
    r"\bclient[_ ]id\b|\bprofile id\b|\bstorage id\b",
    r"\bMCP\b",
    r"\b(?:expected|client|resulting)_revision\b|\brevision (?:number|conflict|mismatch|\d+)\b|\bat revision\b",
    r"\b(?:fact_contract|save_as|stale_fact_keys|fresh_fact_keys|evidence_ids?|"
    r"request_id|expires_on|observed_on|review_days|live_fetch|source\.kind)\b",
    r"\b(?:plan\.resources|income\.schedule|client\.profile|tax\.profile|"
    r"(?:constraint|preference|thesis|research|analysis)\.[a-z_]+)\b",
    r"\bI(?:'ve| have)? (?:called|ran|invoked|queried|used) (?:the |a |an |my |your )?"
    r"(?:[\w-]+ ){0,2}(?:tool|function|task|endpoint|server)s?\b",
    r"\b(?:I'll|I will|let me|I'm going to) (?:use|call|invoke|run|query) (?:the |a |an |my )?"
    r"(?:[\w-]+ ){0,2}(?:tool|function|task|endpoint)s?\b",
    r"\btool (?:call|output|result|payload|error)s?\b",
    r"\b(?:usé|llamé|ejecuté|consulté|voy a usar|voy a llamar|voy a ejecutar) (?:la|una|las) herramientas?\b",
    r"Traceback \(most recent call last\)",
    r"^\s*File \".+\", line \d+",
    r"\b[A-Z][A-Za-z]+(?:Error|Exception):",
    r"\{\s*\"[A-Za-z_]+\"\s*:",
    r"```json",
    flags=re.IGNORECASE | re.MULTILINE,
)
# "MCP" must stay case-sensitive to avoid matching ordinary words.
LEAKS[2] = re.compile(r"\bMCP\b")

MEMORY_MECHANICS = _rx(
    r"\b(?:saved|stored|recorded|logged|written) (?:this|that|it|these|those|your [\w ]{1,20}?) "
    r"(?:to|in|into) (?:your |my |the )?(?:memory|profile|records?|database|file)\b",
    r"\bmy memory\b|\bmemory (?:store|system|database)\b|\bmi memoria\b",
    r"\b(?:guardé|registré|almacené|anoté) (?:esto|eso|esa|ese|tu|tus|la|el|los|las)\b[^.]{0,40}\b(?:memoria|perfil|expediente)\b",
    r"\b(?:stale|expired) (?:fact|record|entry|data point)s?\b",
)
SAVE_RECEIPTS = _rx(
    r"\bI(?:'ve| have)? (?:saved|stored|recorded|noted down)\b",
    r"\b(?:lo he guardado|lo guardé|quedó guardad[oa]|he guardado|ya guardé|quedó registrad[oa])\b",
)
BOILERPLATE = _rx(
    r"\bnot (?:intended as |meant as |to be (?:taken|construed) as )?(?:personal(?:ized)? )?"
    r"(?:financial|investment|tax|legal) advice\b",
    r"\bno (?:es|constituye) (?:una )?(?:asesoría|recomendación|consejo)s? (?:financier|de inversión|fiscal)",
    r"\bas an AI\b|\bI(?:'m| am) (?:just |only )?an AI\b|\bas a language model\b",
    r"\bcomo (?:una )?(?:IA|inteligencia artificial|modelo de lenguaje)\b",
    r"\bI(?:'m| am) not a (?:licensed |registered |certified )?(?:financial )?(?:advisor|adviser|planner)\b",
    r"\bno soy (?:un |una )?(?:asesor|asesora|planificador)\b",
    r"\bpast performance (?:is not|does not|doesn't)",
    r"\b(?:los )?rendimientos pasados no garantizan",
    r"\bdo your own research\b|\beveryone'?s (?:situation|circumstances) (?:is|are) different\b",
    r"\bcada (?:persona|situación) es (?:diferente|distinta)\b",
)
REFERRALS = _rx(
    r"\bconsult(?:ing)? (?:with )?(?:a|an|your) (?:qualified |licensed |certified |financial |tax |"
    r"professional |fiduciary )*(?:professional|advisor|adviser|planner|expert)\b",
    r"\b(?:speak|talk) (?:with|to) (?:a|an) (?:qualified |licensed |financial |certified )+"
    r"(?:professional|advisor|adviser|planner)\b",
    r"\bconsulta(?:r|lo)? (?:a|con) (?:un|una|tu) (?:asesor|asesora|profesional|experto|especialista|contador|contadora)\b",
)
FILLER_OPENERS = _rx(
    r"^\W{0,3}(?:Absolutely|Certainly|Of course|Great question|Good question|Excellent question|"
    r"Happy to help|I'?d be (?:happy|glad|delighted) to|What a great|Thanks for sharing|"
    r"Thank you for sharing|¡Claro!|¡Claro que sí|¡?Por supuesto|¡?Excelente pregunta|"
    r"¡?Buena pregunta|¡?Con (?:mucho )?gusto|¡?Qué buena pregunta|Gracias por compartir)\b",
)
SERVICE_MENUS = _rx(
    r"\bI can (?:also )?help(?: you)?(?: with| connect| organize| understand| by| you|,|\.)",
    r"\bhere(?:'s| is) what I can do\b|\bI can (?:assist|support) (?:you )?with\b",
    r"\b(?:también )?puedo ayudarte(?: a| con)?\b",
    r"\bwe can (?:start with|look at) (?:either|any of)\b|\bpodemos empezar por cualquiera\b",
    r"\bwhat would you like to (?:start with|work on|focus on|do|explore)\b|\bwhere would you like to start\b",
    r"\b(?:qué|en qué|por dónde) (?:te gustaría|quieres|prefieres) (?:empezar|trabajar|enfocarte|comenzar)\b",
    r"\b(?:most important|main|primary|biggest) (?:financial )?(?:goal|priority)\b[^.?]{0,40}\b(?:such as|like|e\.g\.)",
    r"\b(?:meta|objetivo|prioridad) (?:financier[oa] )?(?:más importante|principal)\b[^.?]{0,40}\b(?:como|por ejemplo)\b",
)
FILLER_CLOSERS = _rx(
    r"\blet me know if\b|\bfeel free to\b|\bdon'?t hesitate\b|\bhope (?:this|that) helps\b",
    r"\bI hope this\b|\bhappy to (?:help|dig|go|walk|elaborate)\b",
    r"\bavísame si\b|\bno dudes en\b|\bespero que (?:esto|te) (?:ayude|sirva)\b|\bcon gusto (?:te )?(?:ayudo|profundizo)\b",
)
LATEX = _rx(
    r"\\\(|\\\)|\\\[|\\\]|\$\$",
    r"\\(?:frac|sum|sqrt|text|cdot|times|left|right|mathrm|mathbb|beta|sigma|alpha|mu|rho|Delta)\b",
    flags=0,
)
CALQUES_ES = _rx(
    r"\bhace sentido\b",
    r"\baplicar (?:para|a) (?:un|una|el|la) (?:crédito|préstamo|tarjeta|hipoteca)\b",
    r"\ben orden de (?:que|poder|lograr)\b",
    r"\btomar ventaja de\b",
    r"\bvosotros\b|\bhabéis\b|\bos (?:recomiendo|sugiero|conviene)\b",
    r"\bmercado de stocks?\b|\bstocks de\b",
)
HEDGES = _rx(
    r"\b(?:may|might|could|possibly|potentially|perhaps|generally|typically|it depends|"
    r"podría|podrían|quizá|quizás|posiblemente|potencialmente|generalmente|depende)\b",
)
MARKET_CLAIMS = _rx(
    r"\btrad(?:es|ing|ed) (?:at|near|around)\b|\bcurrently (?:trades|yields|pays|priced)\b",
    r"\bclosed (?:at|yesterday)\b|\bas of (?:today|this (?:week|month)|[A-Z][a-z]+ \d{1,2})\b",
    r"\b(?:forward |trailing )?P/?E (?:of|is|near|around|at)?\s*~?\d",
    r"\b\d+(?:\.\d+)?\s?[x×] (?:earnings|EPS|sales|revenue|ventas|utilidades)\b",
    r"\byield(?:s|ing)? (?:of |about |around |roughly )?~?\d",
    r"\bcotiza(?:ba|ndo)? (?:en|a|alrededor|cerca)\b|\bal cierre (?:de|del)\b",
    r"\b(?:CETES|la tasa objetivo|la tasa de Banxico|el tipo de cambio)\b[^.]{0,40}\b\d+(?:[.,]\d+)?\s?%",
    r"\b(?:the Fed(?:eral Reserve)?|Treasury|T-bill)s?\b[^.]{0,40}\b\d+(?:\.\d+)?\s?%",
)


# --- expectations -----------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "words": None,           # [min, max] or None
    "structure": "auto",     # none | light | allowed | auto
    "max_questions": 1,
    "sources": "auto",       # required | auto | none
    "allow_referral": False,
    "declined": [],          # regexes a question must not repeat
    "must": [],              # [{"pattern", "why"}] or regex strings
    "must_not": [],
}
DEPTH_REQUEST = re.compile(
    r"\b(?:in depth|in detail|detailed|deep dive|technical|walk me through|step by step|explain .* "
    r"(?:math|method)|compare|comparison|table|breakdown|a fondo|a detalle|detallad[oa]|paso a paso|"
    r"compara|comparación|tabla|desglose)\b",
    re.IGNORECASE,
)


def expectations(expect: Mapping[str, Any] | None, user: str = "") -> dict[str, Any]:
    merged = {**DEFAULTS, **(expect or {})}
    if merged["structure"] == "auto":
        if DEPTH_REQUEST.search(user or ""):
            merged["structure"] = "allowed"
        elif word_count(user or "") <= 15:
            merged["structure"] = "none"
        else:
            merged["structure"] = "light"
    return merged


def _patterns(entries: Iterable[Any]) -> list[tuple[re.Pattern[str], str]]:
    out = []
    for entry in entries:
        if isinstance(entry, str):
            out.append((re.compile(entry, re.IGNORECASE), entry))
        else:
            out.append((re.compile(entry["pattern"], re.IGNORECASE), entry.get("why", entry["pattern"])))
    return out


# --- the checks -------------------------------------------------------------

def run_checks(
    response: str,
    *,
    user: str = "",
    expect: Mapping[str, Any] | None = None,
    language: str | None = None,
    history: Sequence[Sequence[str]] = (),
) -> list[Finding]:
    findings: list[Finding] = []

    def add(check: str, severity: str, detail: str) -> None:
        findings.append(Finding(check, severity, detail))

    text = response or ""
    if not text.strip():
        return [Finding("empty_response", "fail", "no assistant text")]
    exp = expectations(expect, user)
    body = prose(text)
    stats = shape(text)
    count = stats["words"]

    for pattern in LEAKS:
        found = pattern.search(text)
        if found:
            add("leaked_internals", "fail", _snippet(text, found))
            break
    found = _first(MEMORY_MECHANICS, body)
    if found:
        add("memory_mechanics", "fail", _snippet(body, found))
    found = _first(SAVE_RECEIPTS, body)
    if found:
        add("save_receipt", "warn", _snippet(body, found))

    found = _first(BOILERPLATE, body)
    if found:
        add("boilerplate_disclaimer", "fail", _snippet(body, found))
    if not exp["allow_referral"]:
        found = _first(REFERRALS, body)
        if found:
            add("unwarranted_referral", "fail", _snippet(body, found))

    found = _first(FILLER_OPENERS, body.lstrip())
    if found:
        add("filler_opener", "fail", _snippet(body.lstrip(), found))
    found = _first(SERVICE_MENUS, body)
    if found:
        add("service_menu", "fail", _snippet(body, found))
    found = _first(FILLER_CLOSERS, body)
    if found:
        add("filler_closer", "fail", _snippet(body, found))

    found = _first(LATEX, text)
    if found:
        add("latex", "fail", _snippet(text, found))

    structure = exp["structure"]
    if structure == "none":
        if stats["headings"]:
            add("markdown_overuse", "fail", f"{stats['headings']} heading(s) in a conversational turn")
        if stats["tables"]:
            add("markdown_overuse", "fail", "table in a conversational turn")
        if stats["list_blocks"] > 1:
            add("markdown_overuse", "fail", f"{stats['list_blocks']} lists in a conversational turn")
        elif stats["list_blocks"] == 1:
            add("markdown_overuse", "warn", "list in a conversational turn")
        if stats["bold"] > 1:
            add("bold_sprinkle", "fail", f"{stats['bold']} bold spans in a conversational turn")
    elif structure == "light":
        if stats["headings"]:
            add("markdown_overuse", "fail", f"{stats['headings']} heading(s) without a depth request")
        if stats["list_blocks"] > 1:
            add("markdown_overuse", "fail", f"{stats['list_blocks']} lists without a depth request")
        if stats["tables"]:
            add("markdown_overuse", "warn", "table without a depth request")
        if stats["bold"] > max(2, count // 80):
            add("bold_sprinkle", "fail", f"{stats['bold']} bold spans in {count} words")
    else:
        if stats["bold"] > max(4, count // 50):
            add("bold_sprinkle", "warn", f"{stats['bold']} bold spans in {count} words")

    asked = questions(text)
    if len(asked) > exp["max_questions"]:
        add("too_many_questions", "fail", f"{len(asked)} questions (max {exp['max_questions']})")

    expected = language or detect_language(user)
    actual = detect_language(text)
    if expected and actual and expected != actual:
        add("language_mismatch", "fail", f"expected {expected}, replied in {actual}")
    if (expected or actual) == "es":
        found = _first(CALQUES_ES, body)
        if found:
            add("spanish_calque", "warn", _snippet(body, found))

    band = exp["words"]
    if band:
        low, high = band
        if count > high:
            add("too_long", "fail", f"{count} words (band {low}-{high})")
        elif count < low:
            add("too_short", "warn", f"{count} words (band {low}-{high})")

    hedges = sum(len(pattern.findall(body)) for pattern in HEDGES)
    if count >= 40 and hedges * 100 / count > 3.5:
        add("over_hedging", "warn", f"{hedges} hedges in {count} words")

    for pattern, why in _patterns(exp["declined"]):
        repeated = next((q for q in asked if pattern.search(q)), None)
        if repeated:
            add("repeated_declined_question", "fail", f"asked again about {why}")
    if not exp["declined"] and repeats_unanswered_question(text, user, history):
        add("repeated_declined_question", "fail", "re-asked the previous question after a non-answer")

    if exp["sources"] == "required" and not has_url(text):
        add("missing_sources", "fail", "scenario requires cited sources")
    elif exp["sources"] == "auto" and not has_url(text):
        found = _first(MARKET_CLAIMS, body)
        if found:
            add("unsourced_market_claim", "fail", _snippet(body, found))

    for pattern, why in _patterns(exp["must_not"]):
        found = pattern.search(body)
        if found:
            add("must_not", "fail", f"{why}: {_snippet(body, found)}")
    for pattern, why in _patterns(exp["must"]):
        if not pattern.search(body):
            add("must", "fail", f"missing: {why}")
    return findings


def memory_findings(saved_keys: Iterable[str], expect: Mapping[str, Any] | None) -> list[Finding]:
    """Checks on what the turn wrote to memory (live runs only)."""

    saved = list(saved_keys)
    expect = expect or {}
    findings = []
    for prefix in expect.get("saves", []):
        if not any(key == prefix or key.startswith(prefix) for key in saved):
            findings.append(Finding("memory_not_saved", "warn", f"expected a write to {prefix}"))
    if expect.get("no_saves") and saved:
        findings.append(Finding("memory_unexpected_save", "fail", f"wrote {', '.join(sorted(saved))}"))
    return findings


def verdict(findings: Iterable[Finding]) -> str:
    severities = {finding.severity for finding in findings}
    return "fail" if "fail" in severities else "warn" if "warn" in severities else "pass"
