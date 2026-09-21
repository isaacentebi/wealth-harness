"""Text-channel helpers for OpenClaw: onboarding as numbered text, nudges, and views as PNG/SVG."""
from __future__ import annotations

import copy
import json
import stat
from pathlib import Path

import pytest

from wealth import cli, cli_text, views
from wealth.service import WealthService

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "views_envelopes.json").read_text(encoding="utf-8"))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "private" / "w.sqlite3"
    path.parent.mkdir()
    WealthService(path).create("me", "me")
    monkeypatch.setenv("WEALTH_DB", str(path))
    return path


def _cli(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _answer(capsys, step, text, lang="es"):
    code, out, err = _cli(capsys, "onboarding", "answer", "--client", "me", "--step", step, "--text", text,
                          "--lang", lang, "--json")
    assert code == 0, (out, err)
    return json.loads(out)


def test_next_prints_the_first_card_as_numbered_text(db, capsys):
    code, out, _ = _cli(capsys, "onboarding", "next", "--client", "me", "--lang", "es")
    assert code == 0
    assert out.splitlines() == [
        "(1/9) ¿Cómo te llamo y dónde vives?",
        "1. México",
        "2. Estados Unidos",
        "3. Otro país",
        "Escribe tu nombre y el número de dónde vives (por ejemplo, Ana, 1).",
        'Para saltarla, escribe "omitir".',
    ]
    code, out, _ = _cli(capsys, "onboarding", "next", "--client", "me", "--lang", "en", "--json")
    payload = json.loads(out)
    assert payload["step"] == "identity" and payload["complete"] is False
    assert payload["text"].startswith("(1/9) What should I call you")


def test_a_whole_onboarding_over_text_in_spanish(db, capsys):
    first = _answer(capsys, "identity", "Ana, 1")
    assert first["answered"]["summary"] == "Ana, en México" and first["step"] == "about"
    assert _answer(capsys, "about", "1990, 2")["step"] == "income"
    income = _answer(capsys, "income", "85 mil")
    assert income["text"].count("Recibes $85,000 al mes") == 1  # the summary is not repeated as the picture line
    assert "Escribe la cantidad en MXN" in income["text"]
    assert _answer(capsys, "spending", "como 45 mil")["picture_line"] == "Te quedan $40,000 al mes"
    money = _answer(capsys, "money", "1: 60 mil, 3: 200 mil")
    assert money["answered"]["summary"] == "Nu / banco $60,000 · GBM / casa de bolsa $200,000"
    assert "1. No\n2. Tarjeta de crédito" in money["text"]
    assert _answer(capsys, "debts", "1")["answered"]["summary"] == "Sin deudas"
    goals = _answer(capsys, "goals", "1 y 3")
    assert goals["answered"]["summary"] == "Fondo de emergencia, enganche de casa"
    assert "Responde con el número." in goals["text"]
    assert _answer(capsys, "risk", "2")["step"] == "statements"
    done = _answer(capsys, "statements", "omitir")
    assert done["complete"] is True and done["completed_now"] is True and done["step"] is None
    assert done["text"].splitlines()[1:] == [
        "Listo, ya tengo tu panorama.",
        "Patrimonio neto $260,000 · te quedan $40,000 al mes · reserva de 1.3 meses",
    ]
    code, out, _ = _cli(capsys, "onboarding", "next", "--client", "me")
    assert code == 0 and out.startswith("Listo, ya tengo tu panorama.")


def test_replies_the_parser_cannot_read_go_back_to_the_model(db, capsys):
    code, out, _ = _cli(capsys, "onboarding", "answer", "--client", "me", "--step", "identity",
                        "--text", "¿para qué quieres saber dónde vivo?")
    assert code == cli_text.NEEDS_MODEL
    hint = json.loads(out)
    assert hint["needs_model"] is True and hint["reason"] == "question" and hint["step"] == "identity"
    assert any(field["name"] == "country" for field in hint["fields"])
    # The model then sends the structured answer.
    code, out, _ = _cli(capsys, "onboarding", "answer", "--client", "me", "--step", "identity",
                        "--answer", '{"name": "Sam", "country": "US"}', "--lang", "en")
    assert code == 0 and out.startswith("Sam, in the United States")
    # Numbers outside the list are not guessed.
    code, out, _ = _cli(capsys, "onboarding", "answer", "--client", "me", "--step", "goals", "--text", "9")
    assert code == cli_text.NEEDS_MODEL


def test_numbered_answers_for_each_chip_step():
    card = {"step": "risk", "fields": [{"name": "drop_reaction", "type": "chips",
                                        "options": [{"id": "sell", "label": "Sell"}, {"id": "hold", "label": "Hold"}]}]}
    assert cli_text.numbered_answer(card, "2") == {"drop_reaction": "hold"}
    assert cli_text.numbered_answer(card, "1 2") is None
    assert cli_text.numbered_answer(card, "hold on") is None
    goals = {"step": "goals", "fields": [{"name": "goals", "type": "chips",
                                          "options": [{"id": g, "label": g} for g in "abcdef"]}]}
    assert cli_text.numbered_answer(goals, "1 y 3") == {"goals": ["a", "c"]}
    assert cli_text.numbered_answer(goals, "1, 2, 3") is None


def test_today_prints_nothing_when_no_nudge_fires(db, capsys):
    code, out, _ = _cli(capsys, "today", "--client", "me")
    assert code == cli_text.NOTHING and out == ""


def test_today_prints_short_lines_in_the_persons_language(db, capsys, monkeypatch):
    items = [{"title": {"es": "Llegó tu aguinaldo:\n$42,000", "en": "Your aguinaldo arrived"},
              "next_step": {"es": "¿Qué hago con él?", "en": "What should I do with it?"}},
             {"title": {"es": "x" * 400}}, {"other": 1}]
    monkeypatch.setattr(WealthService, "run", lambda self, task, inputs, client_id=None: {"result": {"today": items}})
    code, out, _ = _cli(capsys, "today", "--client", "me", "--lang", "es")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "Llegó tu aguinaldo: $42,000 — ¿Qué hago con él?"
    assert len(lines) == 2 and len(lines[1]) == cli_text.MAX_LINE


@pytest.mark.parametrize("case", ["spending_monthly", "debt_payoff", "income"])
def test_views_are_written_as_png_from_fixtures(case, tmp_path):
    fixture = FIXTURES[case]
    written, note = cli_text.write_views(fixture["task"], copy.deepcopy(fixture["envelope"]), tmp_path / "v.png", "es")
    expected = views.views_for(fixture["task"], copy.deepcopy(fixture["envelope"]))
    assert note is None and len(written) == len(expected) >= 1
    for item, spec in zip(written, expected):
        path = Path(item["path"])
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert item["title"] == spec["title"]["es"]
    if len(written) == 2:
        assert Path(written[1]["path"]).name == "v-2.png"


def test_views_fall_back_to_svg_without_pillow(tmp_path, monkeypatch):
    monkeypatch.setattr(views, "png_available", lambda: False)
    fixture = FIXTURES["debt_payoff"]
    written, note = cli_text.write_views(fixture["task"], copy.deepcopy(fixture["envelope"]), tmp_path / "v.png", "en")
    assert "Pillow" in note
    assert written[0]["format"] == "svg" and written[0]["path"].endswith("v.svg")
    assert Path(written[0]["path"]).read_text().startswith("<svg")


def test_view_command_draws_the_saved_picture(db, capsys, tmp_path):
    for step, text in (("identity", "Ana, 1"), ("income", "85 mil"), ("spending", "45 mil"),
                       ("money", "1: 60 mil")):
        _answer(capsys, step, text)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    code, out, err = _cli(capsys, "view", "--client", "me", "--task", "situation", "--png", str(out_dir / "p.png"))
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == "Dónde estás hoy"
    assert lines[1] == f"MEDIA:{(out_dir / 'p.png').resolve()}"
    # A task with nothing to draw says so and sends nothing.
    code, out, err = _cli(capsys, "view", "--task", "situation", "--png", str(out_dir / "q.png"))
    assert code == 2 and "--client" in err


def test_outputs_leak_no_private_paths_or_account_numbers(db, capsys, tmp_path):
    outputs = []
    outputs.append(_cli(capsys, "onboarding", "next", "--client", "me"))
    outputs.append(_cli(capsys, "onboarding", "answer", "--client", "me", "--step", "identity",
                        "--text", "mi cuenta es 012180015555555555, ¿sirve?"))
    outputs.append(_cli(capsys, "onboarding", "answer", "--client", "nobody", "--step", "identity", "--text", "Ana, 1"))
    outputs.append(_cli(capsys, "view", "--client", "me", "--task", "situation", "--png",
                        str(db.parent / "missing-dir" / "x.png")))
    outputs.append(_cli(capsys, "today", "--client", "me"))
    private = [str(db), str(db.parent), str(Path.home())]
    for _, out, err in outputs:
        for secret in private:
            assert secret not in out and secret not in err, (secret, out, err)
        assert "012180015555555555" not in out + err
