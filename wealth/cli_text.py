"""Text-channel helpers for chat hosts such as OpenClaw (WhatsApp, Telegram, iMessage, Signal).

``wealth onboarding next|answer``, ``wealth today`` and ``wealth view`` print short,
person-facing text (or write an image) so a host agent can run the deterministic
onboarding, daily nudges and engine-drawn views over a text channel. The logic is
the same as the web chat's: cards come from :mod:`wealth.onboarding`, drawings
from :mod:`wealth.views`.  Nothing here prints the database path, the data
directory or anything the person did not type.

Exit codes: 0 ok, 2 invalid input or error, 3 the reply needs the host model to
interpret it (stdout is a JSON hint), 4 nothing to send.
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

from . import onboarding as _ob
from . import views as _views
from .service import WealthService

COMMANDS = ("onboarding", "today", "view")
NEEDS_MODEL = 3
NOTHING = 4
MAX_NUDGES = 5
MAX_LINE = 280

_SKIP = re.compile(r"^\s*(omitir|omite|om[ií]tela|saltar|s[aá]ltala|salta|paso|skip|pass|next)\s*[.!]*\s*$", re.I)
_NUMBERS = re.compile(r"^\s*\d+(\s*(,|y|and|&|/|\s)\s*\d+)*\s*[.!]*\s*$", re.I)
_PAIR = re.compile(r"(?:^|[,;]|\by\b|\band\b)\s*(\d{1,2})\s*[:=]\s*", re.I)

# The chip field whose options are numbered, per step.  Other steps are typed answers.
_NUMBERED = {"identity": "country", "money": "items", "debts": "items", "goals": "goals", "risk": "drop_reaction"}

_T = {
    "progress": {"es": "({i}/{n})", "en": "({i}/{n})"},
    "pick_one": {"es": "Responde con el número.", "en": "Reply with the number."},
    "pick_two": {"es": "Elige una o dos: responde con los números (por ejemplo, 1 y 3).",
                 "en": "Pick one or two: reply with the numbers (for example, 1 and 3)."},
    "pick_many": {"es": "Responde con los números y, si quieres, cuánto hay en cada uno (por ejemplo, 1: 60 mil).",
                  "en": "Reply with the numbers and, if you like, roughly how much is in each (for example, 1: 60k)."},
    "debts": {"es": "Responde con los números y el saldo de cada una (por ejemplo, 2: 20 mil al 45%).",
              "en": "Reply with the numbers and the balance of each (for example, 2: 20k at 24%)."},
    "identity": {"es": "Escribe tu nombre y el número de dónde vives (por ejemplo, Ana, 1).",
                 "en": "Write your name and the number for where you live (for example, Sam, 2)."},
    "about": {"es": "Escribe el año y cuántas personas dependen de ti (por ejemplo, 1990, 2).",
              "en": "Write the year and how many people depend on you (for example, 1990, 2)."},
    "amount": {"es": "Escribe la cantidad en {currency} (por ejemplo, 45 mil).",
               "en": "Write the amount in {currency} (for example, 4,500)."},
    "statements": {"es": "Envíame aquí tus estados de cuenta (PDF o foto) para un panorama exacto.",
                   "en": "Send your statements here (PDF or photo) for an exact picture."},
    "unsure": {"es": "Si no sabes, escribe \"no sé\".", "en": "If you're not sure, write \"not sure\"."},
    "skip": {"es": "Para saltarla, escribe \"omitir\".", "en": "To skip it, write \"skip\"."},
    "confirm": {"es": "Tengo esto: {summary}. ¿Es correcto? Responde \"sí\" o escribe el dato nuevo.",
                "en": "I have this: {summary}. Is that right? Reply \"yes\" or write the new figure."},
    "done": {"es": "Listo, ya tengo tu panorama.", "en": "Done, I have your picture."},
}
_YES = re.compile(r"^\s*(s[ií]|si,? (es )?correcto|correcto|yes|yep|correct|right|ok|okay|va|as[ií] es)\s*[.!]*\s*$", re.I)


def _t(key: str, lang: str, **values: Any) -> str:
    return _T[key][lang].format(**values)


def _lang(service: WealthService, client_id: str, requested: str | None) -> str:
    if requested in ("es", "en"):
        return requested
    try:
        language = (service.situation(client_id).get("profile") or {}).get("language")
    except (ValueError, KeyError, sqlite3.Error):
        language = None
    return "en" if str(language or "").lower().startswith("en") else "es"


# ------------------------------------------------------------------ onboarding cards as text


def _field(card: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    return next((f for f in card.get("fields") or [] if f.get("name") == name), None)


def _confirm_summary(sit: Mapping[str, Any], card: Mapping[str, Any] | None, lang: str) -> str | None:
    """What memory already holds for a prefilled card, in the card's own words."""
    if not card or not card.get("confirm") or not card.get("prefill"):
        return None
    try:
        return _ob.BY_ID[card["step"]].summary(card["prefill"], lang, {**_ob._context(sit), "language": lang})
    except (ValueError, KeyError, TypeError, AttributeError):
        return card.get("title")


def card_text(card: Mapping[str, Any] | None, lang: str, picture: Mapping[str, Any] | None = None,
              known: str | None = None) -> str:
    """One onboarding card as a short numbered text message (no markdown).

    ``known`` is the summary of a prefilled answer; the card then asks to confirm it.
    """
    if card is None:
        line = (picture or {}).get("line") or ""
        return "\n".join(filter(None, [_t("done", lang), line]))
    step = card["step"]
    lines = [f"{_t('progress', lang, i=card.get('index') or '?', n=card.get('total') or '?')} {card['prompt']}"]
    if known:
        lines.append(_t("confirm", lang, summary=known))
        return "\n".join(lines)
    numbered = _field(card, _NUMBERED.get(step, ""))
    if numbered:
        options = [o for o in numbered.get("options") or []]
        lines += [f"{n}. {o['label']}" for n, o in enumerate(options, 1)]
    if step == "identity":
        lines.append(_t("identity", lang))
    elif step == "about":
        lines.append(_t("about", lang))
    elif step in ("income", "spending"):
        lines.append(_t("amount", lang, currency=card.get("currency") or "MXN"))
    elif step == "goals":
        lines.append(_t("pick_two", lang))
    elif step == "money":
        lines.append(_t("pick_many", lang))
    elif step == "debts":
        lines.append(_t("debts", lang))
    elif step == "statements":
        lines.append(_t("statements", lang))
    elif numbered:
        lines.append(_t("pick_one", lang))
    tail = []
    if card.get("unsure"):
        tail.append(_t("unsure", lang))
    tail.append(_t("skip", lang))
    lines.append(" ".join(tail))
    return "\n".join(lines)


def _numbers(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", text)]


def numbered_answer(card: Mapping[str, Any], text: str) -> dict | None:
    """A reply made of option numbers (or name + number for identity), as the step's answer."""
    step = card.get("step")
    field = _field(card, _NUMBERED.get(step or "", ""))
    if not field:
        return None
    options = field.get("options") or []

    def pick(numbers: list[int]) -> list[str] | None:
        if not numbers or any(n < 1 or n > len(options) for n in numbers):
            return None
        ids: list[str] = []
        for n in numbers:
            if options[n - 1]["id"] not in ids:
                ids.append(options[n - 1]["id"])
        return ids

    if step == "identity":
        match = re.fullmatch(r"\s*([^\d,;]{1,80}?)\s*[,;]?\s*(\d)\s*[.!]*\s*", text)
        if not match:
            return None
        ids = pick([int(match.group(2))])
        name = match.group(1).strip(" .")
        return {"name": name, "country": ids[0]} if ids and name else None
    if step == "money" and _PAIR.search(text):
        # "1: 60 mil, 2: 200k": each number starts an item; the text up to the next number is its amount.
        marks = list(_PAIR.finditer(text))
        if text[:marks[0].start()].strip(" ,;"):
            return None
        items: dict[str, dict] = {}
        for n, mark in enumerate(marks):
            end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
            ids = pick([int(mark.group(1))])
            amount = _ob.parse_amount(text[mark.end():end].strip(" ,;.") or "")
            if not ids or amount is None or ids[0] in items:
                return None
            items[ids[0]] = {"amount": {"amount": amount["amount"], "currency": amount["currency"]}
                             if amount["currency"] else amount["amount"]}
        return {"items": items}
    if not _NUMBERS.match(text):
        return None
    ids = pick(_numbers(text))
    if not ids:
        return None
    if step == "goals":
        return {"goals": ids} if len(ids) <= 2 else None
    if step == "risk":
        return {"drop_reaction": ids[0]} if len(ids) == 1 else None
    if step in ("money", "debts"):
        return {"items": {i: {} for i in ids}}
    return None


def _needs_model(card: Mapping[str, Any], reason: str) -> dict:
    return {"needs_model": True, "reason": reason, "step": card.get("step"), "fields": card.get("fields"),
            "hint": "Interpret the reply yourself and resend it with --answer '<json>' using these field names, "
                    "or answer the person's question first and ask the card again."}


def onboarding_next(service: WealthService, client_id: str, lang: str | None = None) -> tuple[str, dict]:
    language = _lang(service, client_id, lang)
    sit = service.situation(client_id)
    card = _ob.next_step(sit, language)
    picture = _ob.picture(sit, language)
    text = card_text(card, language, picture, _confirm_summary(sit, card, language))
    return text, {"text": text, "step": card["step"] if card else None, "complete": card is None,
                  "picture_line": picture["line"]}


def onboarding_answer(service: WealthService, client_id: str, step: str, *, text: str | None = None,
                      answer: Mapping[str, Any] | None = None, skip: bool = False,
                      lang: str | None = None) -> tuple[int, str, dict]:
    """Write one answer and return (exit code, text for the person, JSON payload)."""
    if step not in _ob.BY_ID:
        raise ValueError(f"unknown onboarding step; steps are {', '.join(_ob.BY_ID)}")
    language = _lang(service, client_id, lang)
    card = _ob.card(service.situation(client_id), step, language)
    if text is not None:
        text = text.strip()
        if not text or len(text) > 2000:
            raise ValueError("--text must be 1-2000 characters")
        if _SKIP.match(text):
            skip = True
        elif card.get("confirm") and _YES.match(text):
            answer = {"confirm": True}
        else:
            answer = numbered_answer(card, text)
            if answer is None:
                parsed = _ob.parse_free_text(card, text)
                if parsed["status"] != "parsed":
                    payload = _needs_model(card, parsed.get("reason") or "free text")
                    return NEEDS_MODEL, json.dumps(payload, ensure_ascii=False), payload
                answer = parsed["answer"]
    try:
        result = _ob.apply(service, client_id, step, answer, skip=skip, language=language)
    except ValueError as exc:
        if text is None:
            raise
        payload = _needs_model(card, str(exc))
        return NEEDS_MODEL, json.dumps(payload, ensure_ascii=False), payload
    picture = result["picture"]
    lines = [result["answered"]["summary"]]
    if result["card"] is None:
        lines.append(card_text(None, language, picture))
    else:
        if picture.get("line") and picture["line"].casefold() != lines[0].casefold():
            lines.append(picture["line"])
        after = service.situation(client_id)
        lines.append(card_text(result["card"], language, known=_confirm_summary(after, result["card"], language)))
    message = "\n".join(lines)
    return 0, message, {"text": message, "step": result["card"]["step"] if result["card"] else None,
                        "answered": result["answered"], "complete": result["complete"],
                        "completed_now": result["completed_now"], "picture_line": picture.get("line") or ""}


# ------------------------------------------------------------------ daily nudges


def _nudge_line(item: Any) -> str | None:
    if isinstance(item, Mapping):
        item = item.get("text") or item.get("line") or item.get("title") or item.get("message")
    if not isinstance(item, str):
        return None
    line = " ".join(item.split())
    return line[:MAX_LINE - 1] + "…" if len(line) > MAX_LINE else line or None


def today_lines(service: WealthService, client_id: str, lang: str | None = None) -> list[str] | None:
    """Today's nudges as short lines, or ``None`` when this Wealth has no proactive module."""
    try:
        proactive = importlib.import_module("wealth.proactive")
    except ImportError:
        return None
    today = getattr(proactive, "today", None)
    if not callable(today):
        return None
    result = today(service, client_id, language=_lang(service, client_id, lang))
    if isinstance(result, Mapping):
        result = result.get("nudges") or result.get("items") or []
    lines = [line for line in (_nudge_line(item) for item in result or []) if line]
    return lines[:MAX_NUDGES]


# ------------------------------------------------------------------ views as media


def _envelope(service: WealthService, client_id: str | None, task: str, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    if task == "situation":
        if not client_id:
            raise ValueError("the situation view needs --client")
        return service.situation(client_id)
    return service.run(task, dict(inputs), client_id=client_id)


def write_views(task: str, envelope: Mapping[str, Any], out: Path, lang: str, *, png: bool = True) -> tuple[list[dict], str | None]:
    """Draw the result's views to ``out`` (``out-2`` for a second view).

    Returns ``([{"path", "title", "format"}], note)``; the note says when an SVG was
    written because Pillow is missing.
    """
    specs = _views.views_for(task, envelope)
    written: list[dict] = []
    note = None
    use_png = png and _views.png_available()
    if png and not use_png:
        note = "Pillow is not installed, so the view was written as SVG; most chat apps do not preview SVG."
    for n, spec in enumerate(specs, 1):
        target = out if n == 1 else out.with_name(f"{out.stem}-{n}{out.suffix}")
        data = _views.render_png(spec, lang) if use_png else None
        if data is None:
            target = target.with_suffix(".svg")
            target.write_text(_views.render_svg(spec, lang), encoding="utf-8")
            fmt = "svg"
        else:
            target.write_bytes(data)
            fmt = "png"
        target.chmod(0o600)  # the drawing shows the person's figures
        written.append({"path": str(target.resolve()), "title": spec["title"].get(lang) or spec["title"]["en"],
                        "format": fmt})
    return written, note


def _json_input(raw: str | None) -> dict:
    if raw is None or not raw.strip():
        return {}
    text = sys.stdin.read() if raw == "-" else raw if raw.lstrip().startswith("{") else Path(raw).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("--input must be a JSON object")
    return value


# ------------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wealth", description="Wealth text-channel helpers")
    parser.add_argument("--db", help="SQLite path (default WEALTH_DB)")
    sub = parser.add_subparsers(dest="command", required=True)
    ob = sub.add_parser("onboarding", help="setup questions as numbered text")
    ob.add_argument("action", choices=("next", "answer"))
    ob.add_argument("--client", required=True)
    ob.add_argument("--lang", choices=("es", "en"))
    ob.add_argument("--step", help="the step being answered (from the last card)")
    group = ob.add_mutually_exclusive_group()
    group.add_argument("--text", help="the person's reply, as typed")
    group.add_argument("--answer", help="a structured answer as JSON, when the reply needed interpretation")
    group.add_argument("--skip", action="store_true", help="skip this step; it is not asked again")
    ob.add_argument("--json", action="store_true", help="print JSON instead of text")
    today = sub.add_parser("today", help="today's nudges as short lines")
    today.add_argument("--client", required=True)
    today.add_argument("--lang", choices=("es", "en"))
    view = sub.add_parser("view", help="draw a result's views as images to send")
    view.add_argument("--client")
    view.add_argument("--task", required=True, help="a wealth run task, or 'situation' for the saved picture")
    view.add_argument("--input", help="task inputs: inline JSON object, a file path, or '-' for stdin")
    view.add_argument("--lang", choices=("es", "en"))
    out = view.add_mutually_exclusive_group(required=True)
    out.add_argument("--png", help="output PNG path (SVG next to it when Pillow is missing)")
    out.add_argument("--svg", help="output SVG path")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        service = WealthService(args.db)
        if args.command == "onboarding":
            if args.action == "next":
                text, payload = onboarding_next(service, args.client, args.lang)
                print(json.dumps(payload, ensure_ascii=False) if args.json else text)
                return 0
            if not args.step:
                raise ValueError("onboarding answer requires --step (the step of the card you asked)")
            if args.text is None and args.answer is None and not args.skip:
                raise ValueError("onboarding answer requires --text, --answer or --skip")
            answer = json.loads(args.answer) if args.answer is not None else None
            code, text, payload = onboarding_answer(service, args.client, args.step, text=args.text, answer=answer,
                                                    skip=args.skip, lang=args.lang)
            print(json.dumps(payload, ensure_ascii=False) if args.json or code == NEEDS_MODEL else text)
            return code
        if args.command == "today":
            lines = today_lines(service, args.client, args.lang)
            if lines is None:
                print("Daily nudges are not available in this version of Wealth (wealth.proactive is missing); "
                      "nothing to send.", file=sys.stderr)
                return NOTHING
            if not lines:
                return NOTHING
            print("\n".join(lines))
            return 0
        lang = _lang(service, args.client, args.lang) if args.client else (args.lang or "es")
        envelope = _envelope(service, args.client, args.task, _json_input(args.input))
        target = Path(args.png or args.svg).expanduser()
        if not target.parent.is_dir():
            raise ValueError("the output directory does not exist")
        written, note = write_views(args.task, envelope, target, lang, png=bool(args.png))
        if note:
            print(note, file=sys.stderr)
        if not written:
            status = envelope.get("status") if isinstance(envelope, Mapping) else None
            print(f"No view to draw for this result (status: {status or 'n/a'}).", file=sys.stderr)
            return NOTHING
        for item in written:
            print(item["title"])
            print(f"MEDIA:{item['path']}")
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(json.dumps({"error": _scrub(str(exc), getattr(args, "db", None)), "error_type": type(exc).__name__}),
              file=sys.stderr)
        return 2


def _scrub(message: str, db: str | None) -> str:
    """Error text without local paths: the database location and home directory stay private."""
    from .service import database_path

    for secret in filter(None, {str(database_path(db)), str(database_path(db).parent), str(Path.home())}):
        message = message.replace(secret, "<private>")
    return message
