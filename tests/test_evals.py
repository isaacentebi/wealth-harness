"""Offline tests for the conversation-quality evals (no model calls)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import checks, rubric, scenarios as S
from evals import run as runner
from wealth.household import validate_household


def names(findings, severity=None):
    return {f.check for f in findings if severity is None or f.severity == severity}


# --- scenarios --------------------------------------------------------------

def test_scenario_set_loads_and_covers_the_matrix():
    data = S.load()
    items = data["scenarios"]
    assert len(items) >= 40
    assert {s["language"] for s in items} == {"en", "es"}
    assert {"US", "MX"} <= {s["residence"] for s in items}
    assert any(s.get("seed") for s in items) and any(not s.get("seed") for s in items)
    required = {
        "greeting", "onboarding", "invest_in_x", "understand_y", "dca", "expenses", "statement",
        "stale_fact", "tool_failure", "conflicting_sources", "emotional", "distress",
        "leverage_crypto", "tax_out_of_scope", "expert_depth", "acknowledgement", "refusal",
        "prompt_injection",
    }
    assert required <= S.categories(items)
    for category in required:
        assert {s["language"] for s in items if s["category"] == category} == {"en", "es"}, category


def test_every_scenario_seeds_into_an_isolated_store(tmp_path):
    data = S.load()
    for index, scenario in enumerate(data["scenarios"]):
        service = S.prepare_profile(data, scenario, tmp_path / f"{index}.sqlite3")
        facts = service.inspect(S.CLIENT_ID)["facts"]
        assert bool(facts) == bool(scenario.get("seed")), scenario["id"]
        for fact in facts:
            if fact["key"] == "household":
                validate_household(fact["value"])
        stale = {f["key"] for f in facts if f["stale"]}
        assert stale == set(scenario.get("stale", [])), scenario["id"]


def test_first_run_scenarios_receive_the_welcome_like_the_web_chat():
    data = S.load()
    first = next(s for s in data["scenarios"] if s["id"] == "greet-new-en")
    returning = next(s for s in data["scenarios"] if s["id"] == "greet-returning-en")
    assert S.history(first, "WELCOME") == [("assistant", "WELCOME")]
    assert S.history(returning, "WELCOME") == []


def test_selection_filters_and_rejects_unknown_ids():
    data = S.load()
    spanish = S.select(data, "lang:es")
    assert spanish and all(s["language"] == "es" for s in spanish)
    assert [s["id"] for s in S.select(data, "greet-new-en,greet-new-es")] == ["greet-new-en", "greet-new-es"]
    assert all(s["category"] == "emotional" and s["residence"] == "MX"
               for s in S.select(data, "category:emotional,residence:MX"))
    with pytest.raises(S.ScenarioError):
        S.select(data, "no-such-scenario")


def test_validation_rejects_malformed_scenarios():
    base = {"id": "x", "category": "c", "language": "en", "residence": "US", "message": "hi", "ideal": "ok"}
    S.validate({"scenarios": [base]})
    for broken in (
        {**base, "language": "fr"},
        {**base, "expect": {"words": [50, 10]}},
        {**base, "expect": {"typo": 1}},
        {**base, "seed": "missing"},
        {**base, "surprise": True},
    ):
        with pytest.raises(S.ScenarioError):
            S.validate({"scenarios": [broken]})
    with pytest.raises(S.ScenarioError):
        S.validate({"scenarios": [base, base]})


# --- deterministic checks ---------------------------------------------------

GOOD_EN = ("I wouldn't sell. Today cost you about $1,600 on NVDA, and the kitchen money and your "
           "reserve are in cash, so nothing you need soon is exposed. Is it this one position that "
           "worries you, or the total in stocks?")
GOOD_ES = ("Con lo que me contaste, te sobran unos 24 mil al mes. Yo apartaría 10 mil para el "
           "enganche en CETES y pondría entre 8 y 12 mil en tu NAFTRAC para el largo plazo.")


def test_good_replies_pass_clean():
    assert checks.run_checks(GOOD_EN, user="Market's down, sell everything?", language="en") == []
    assert checks.run_checks(GOOD_ES, user="¿Cuánto puedo invertir al mes?", language="es") == []


@pytest.mark.parametrize("text", [
    "I ran wealth_context for your profile and found the answer.",
    "Your client_id is on file.",
    "The MCP server returned your facts.",
    "The write failed with a stale expected_revision.",
    "I called the planning tool and it says you can invest $12,000.",
    "Your plan.resources entry shows $42,000.",
    'Here is the output: {"status": "ready", "missing": []}',
    "Traceback (most recent call last):\n  File \"x.py\", line 3\nValueError: bad",
    "Usé la herramienta de planeación para calcularlo.",
])
def test_leaked_internals_fail(text):
    assert "leaked_internals" in names(checks.run_checks(text), "fail")


def test_ordinary_words_do_not_trip_leak_checks():
    text = "Acme's guidance revision matters less than the tool you use to compare funds, like a broker."
    assert "leaked_internals" not in names(checks.run_checks(text))


def test_memory_mechanics_and_save_receipts():
    assert "memory_mechanics" in names(checks.run_checks("I've saved that to your profile for next time."))
    assert "memory_mechanics" in names(checks.run_checks("Guardé esto en tu perfil para después."))
    assert "save_receipt" in names(checks.run_checks("Got it, I've saved your income."), "warn")


@pytest.mark.parametrize("text,check", [
    ("This is not financial advice, but index funds are cheap.", "boilerplate_disclaimer"),
    ("As an AI, I cannot predict markets.", "boilerplate_disclaimer"),
    ("Esto no constituye asesoría financiera.", "boilerplate_disclaimer"),
    ("You should consult a financial professional before buying.", "unwarranted_referral"),
    ("Absolutely! Here is how that works.", "filler_opener"),
    ("Great question. It depends on your horizon.", "filler_opener"),
    ("¡Excelente pregunta! Depende de tu horizonte.", "filler_opener"),
    ("I can help you with budgeting, investing and retirement.", "service_menu"),
    ("Hey! What would you like to start with today?", "service_menu"),
    ("Puedo ayudarte con tu presupuesto o tus inversiones.", "service_menu"),
    ("Let's start with your most important financial goal, such as a home or retirement.", "service_menu"),
    ("That covers it. Let me know if you have any other questions.", "filler_closer"),
    ("Avísame si quieres que lo revise.", "filler_closer"),
    ("The weight is \\(w_i h_{ij}\\) summed over funds.", "latex"),
    ("$$x = \\frac{a}{b}$$", "latex"),
])
def test_tics_fail(text, check):
    assert check in names(checks.run_checks(text), "fail")


def test_referral_allowed_when_warranted():
    text = "Para la declaración anual, consulta a un contador público."
    assert "unwarranted_referral" in names(checks.run_checks(text))
    assert "unwarranted_referral" not in names(checks.run_checks(text, expect={"allow_referral": True}))


def test_markdown_rules_depend_on_the_turn():
    heavy = ("## Overview\n\n**Cash** is fine.\n\n- one\n- two\n\nMore text.\n\n1. first\n2. second\n\n"
             "**Stocks** are **concentrated**.")
    casual = names(checks.run_checks(heavy, user="how am I doing?"), "fail")
    assert {"markdown_overuse", "bold_sprinkle"} <= casual
    deep = names(checks.run_checks(heavy, user="Give me a detailed technical breakdown in a table"), "fail")
    assert "markdown_overuse" not in deep


def test_list_shape_counts_blocks_not_items():
    stats = checks.shape("Intro\n\n- a\n- b\n\n- c\n\nMiddle\n\n1. x\n2. y\n")
    assert stats["list_blocks"] == 2 and stats["list_items"] == 5


def test_question_count_ignores_quoted_rhetorical_questions():
    text = 'Look-through answers “what do I own?” while correlation answers “what moves together?” Which matters more to you?'
    assert len(checks.questions(text)) == 1
    two = "What is your income? And do you have debts?"
    assert "too_many_questions" in names(checks.run_checks(two), "fail")
    assert "too_many_questions" in names(checks.run_checks("Anything else?", expect={"max_questions": 0}))


def test_language_detection_and_mismatch():
    assert checks.detect_language(GOOD_ES) == "es"
    assert checks.detect_language(GOOD_EN) == "en"
    assert checks.detect_language("ok") is None
    found = checks.run_checks(GOOD_EN, user="¿Vendo todo? El peso se fue a 21 y la bolsa cae.")
    assert "language_mismatch" in names(found, "fail")
    assert "language_mismatch" in names(checks.run_checks(GOOD_EN, language="es"))


def test_spanish_calques_warn():
    text = "No hace sentido tomar ventaja de la caída con tu dinero del enganche."
    assert "spanish_calque" in names(checks.run_checks(text, language="es"), "warn")


def test_length_band():
    assert "too_long" in names(checks.run_checks(GOOD_EN, expect={"words": [1, 10]}), "fail")
    assert "too_short" in names(checks.run_checks("Sure.", expect={"words": [20, 80]}), "warn")


def test_declined_question_is_not_repeated():
    expect = {"declined": [{"pattern": "income|earn", "why": "income"}]}
    reply = "That mix can fall by half in a bad year. What's your annual income?"
    assert "repeated_declined_question" in names(checks.run_checks(reply, expect=expect))
    fine = "That mix can fall by half in a bad year. When will you need the money?"
    assert "repeated_declined_question" not in names(checks.run_checks(fine, expect=expect))


def test_generic_repeat_after_non_answer_is_caught():
    history = [["assistant", "Is the 40,000 in the savings account your emergency cushion, or is "
                             "some of it meant for the apartment deposit?"]]
    reply = ("Whenever you can, tell me whether the 40,000 savings account is your emergency cushion "
             "or partly for the apartment deposit.")
    assert "repeated_declined_question" in names(checks.run_checks(reply, user="mm", history=history))
    assert "repeated_declined_question" not in names(checks.run_checks("No problem.", user="mm", history=history))
    assert not checks.repeats_unanswered_question(reply, "it is the cushion", history)


def test_sources_for_current_market_claims():
    claim = "Acme trades at 22x earnings and closed at $87 yesterday."
    assert "unsourced_market_claim" in names(checks.run_checks(claim, user="is acme expensive?"))
    cited = claim + " [Acme results](https://example.com/acme)"
    assert "unsourced_market_claim" not in names(checks.run_checks(cited, user="is acme expensive?"))
    assert "missing_sources" in names(checks.run_checks(GOOD_EN, expect={"sources": "required"}))


def test_must_and_must_not_patterns():
    expect = {"must": [{"pattern": "CONDUSEF", "why": "Mexican resource"}],
              "must_not": [{"pattern": "credit counsel", "why": "US-only referral"}]}
    found = names(checks.run_checks("Talk to a nonprofit credit counselor.", expect=expect))
    assert {"must", "must_not"} <= found


def test_memory_findings():
    assert names(checks.memory_findings([], {"saves": ["plan.resources"]})) == {"memory_not_saved"}
    assert names(checks.memory_findings(["goals"], {"no_saves": True})) == {"memory_unexpected_save"}
    assert checks.memory_findings(["plan.resources"], {"saves": ["plan."]}) == []


def test_empty_response_fails():
    assert checks.verdict(checks.run_checks("   ")) == "fail"


# --- judge parsing and replay ----------------------------------------------

def test_judge_prompt_and_parsing_without_a_model():
    transcript = {"scenario": {"ideal": "Be brief."}, "history": [["assistant", "Welcome"]],
                  "user": "hey", "response": "Hi there.", "seeded_facts": [
                      {"key": "plan.resources", "value": {"cash_available": 1}, "stale": True}]}
    prompt = rubric.score_prompt(transcript)
    for name in rubric.DIMENSIONS:
        assert name in prompt
    assert "Be brief." in prompt and "past its review date" in prompt and "<reply>\nHi there.\n</reply>" in prompt
    reply = "```json\n" + json.dumps({"scores": {n: 4 for n in rubric.DIMENSIONS}, "fix": "shorter"}) + "\n```"
    parsed = rubric.parse_scores(reply)
    assert parsed["mean"] == 4.0 and parsed["fix"] == "shorter"
    with pytest.raises(ValueError):
        rubric.parse_scores('{"scores": {"intent": 9}}')
    swapped = rubric.parse_pairwise('{"winner": "A", "dimensions": {"voice": "B"}}', swapped=True)
    assert swapped["winner"] == "B" and swapped["dimensions"]["voice"] == "A"


def test_judge_specs():
    from evals.judge import make_judge
    assert make_judge("none") is None
    assert make_judge("cmd:cat").label == "cmd:cat"
    with pytest.raises(ValueError):
        make_judge("gpt")


def test_replay_scores_saved_transcripts_with_a_stub_judge(tmp_path, capsys):
    folder = tmp_path / "run" / "transcripts"
    folder.mkdir(parents=True)
    (folder / "greet-new-en.json").write_text(json.dumps({"transcript": {
        "scenario_id": "greet-new-en", "user": "hey", "history": [],
        "response": "Absolutely! I can help you with budgeting and investing. What are your goals? Any debts?"}}))
    (folder / "custom.json").write_text(json.dumps({"scenario_id": "custom", "user": "thanks",
                                                    "history": [], "response": "Anytime."}))
    judge = tmp_path / "judge.py"
    judge.write_text("import json,sys\nsys.stdin.read()\nprint(json.dumps({'scores': {k: 3 for k in "
                     f"{list(rubric.DIMENSIONS)!r}" "}}))\n")
    out = tmp_path / "report"
    assert runner.main(["--replay", str(tmp_path / "run"), "--judge", f"cmd:python {judge}",
                        "--out", str(out), "--jobs", "1"]) == 0
    results = json.loads((out / "results.json").read_text())["results"]
    by_id = {r["scenario_id"]: r for r in results}
    assert by_id["greet-new-en"]["verdict"] == "fail"
    assert {"filler_opener", "service_menu", "too_many_questions"} <= {
        f["check"] for f in by_id["greet-new-en"]["findings"]}
    assert by_id["custom"]["verdict"] == "pass"
    assert by_id["custom"]["judge"]["mean"] == 3.0
    assert "Checks (scenarios affected)" in (out / "summary.md").read_text()
    assert "greet-new-en" in capsys.readouterr().out


def test_live_run_path_with_a_fake_codex_process(tmp_path, monkeypatch):
    from wealth import agent

    prompts = []

    def fake_process(command, prompt, timeout, control=None, cwd=None):
        prompts.append((command, prompt))
        events = [
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "wealth",
                                                "tool": "wealth_context", "status": "completed"}},
            {"type": "item.completed", "item": {"type": "agent_message",
                                                "text": "Hi. What comes in and goes out each month, roughly?"}},
            {"type": "turn.completed"},
        ]
        for event in events:
            yield ("line", json.dumps(event))
        yield ("exit", 0, "")

    candidate = tmp_path / "candidate.md"
    candidate.write_text("Candidate prompt.")
    monkeypatch.setattr(agent, "_stream_process", fake_process)
    monkeypatch.setattr(agent, "INSTRUCTIONS_PATH", agent.INSTRUCTIONS_PATH)
    out = tmp_path / "report"
    assert runner.main(["--scenarios", "greet-new-en,stale-resources-en", "--out", str(out),
                        "--jobs", "1", "--instructions", str(candidate), "--no-web-search"]) == 0
    assert len(prompts) == 4  # each scenario: the answer turn, then the memory step
    turns = [(c, p) for c, p in prompts if "<adviser>" not in p]
    assert len(turns) == 2 and all("candidate" not in p for p in turns[0][1:])
    assert all("conversation-" in " ".join(command) for command, _ in turns)
    first = json.loads((out / "transcripts" / "greet-new-en.json").read_text())["transcript"]
    assert first["tools"] == ["wealth.wealth_context"]
    assert first["history"][0][0] == "assistant"  # the onboarding welcome, as in the web chat
    assert "nothing saved yet" in turns[0][1] or "Saved profile: empty" in turns[0][1]
    stale = json.loads((out / "transcripts" / "stale-resources-en.json").read_text())["transcript"]
    assert {f["key"] for f in stale["seeded_facts"] if f["stale"]} == {"plan.resources", "household"}
    assert "<situation>" in turns[1][1]


def test_reports_directory_is_git_ignored():
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    assert "/evals/reports/" in ignore
