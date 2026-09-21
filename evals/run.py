"""Run, replay and compare Wealth conversation-quality evaluations.

Live run (spends Codex quota; each scenario gets an isolated temporary DB):
    uv run python -m evals.run --scenarios all --model sol --judge codex

Rescore saved transcripts without re-running the agent:
    uv run python -m evals.run --replay evals/reports/<run> --judge none

Pairwise A/B of two runs (for example, before and after a prompt change):
    uv run python -m evals.run --compare evals/reports/<a> evals/reports/<b> --judge codex

Try a candidate prompt without editing the shipped one:
    uv run python -m evals.run --instructions /path/to/candidate.md --label candidate ...

Scenario selection: all | id,id | category:<c> | lang:<en|es> | residence:<US|MX> | seed:<name>.
Judges: none | codex (your default Codex model) | codex:<model> | cmd:<command reading stdin>.
Reports go to evals/reports/<timestamp>[-label]/ (git-ignored).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Sequence

from wealth import agent
from wealth.behavior import ONBOARDING_WELCOME

from . import scenarios as scenario_lib
from .checks import Finding, detect_language, memory_findings, run_checks, shape, verdict
from .judge import JudgeError, make_judge
from .rubric import DIMENSIONS, pairwise_prompt, parse_pairwise, parse_scores, score_prompt

REPORTS_DIR = Path(__file__).with_name("reports")
PREVIEW_CHARS = 320


# --- live agent runs --------------------------------------------------------

_captured = threading.local()


def _install_capture() -> None:
    """Keep each thread's raw Codex stdout so the transcript can list tools used."""

    if getattr(agent._stream_process, "_eval_capture", False):
        return
    original = agent._stream_process

    def capturing(command, prompt, timeout, control=None, cwd=None):
        lines = _captured.__dict__.setdefault("lines", [])
        for item in original(command, prompt, timeout, control, cwd):
            if item[0] == "line":
                lines.append(item[1])
            yield item

    capturing._eval_capture = True  # type: ignore[attr-defined]
    agent._stream_process = capturing


def run_scenario(data: dict, scenario: dict, options: argparse.Namespace) -> dict[str, Any]:
    _captured.lines = []
    with tempfile.TemporaryDirectory(prefix="wealth-eval-") as tmp:
        db = Path(tmp) / "wealth.sqlite3"
        service = scenario_lib.prepare_profile(data, scenario, db)
        before = service.inspect(scenario_lib.CLIENT_ID)["facts"]
        before_ids = {fact["id"] for fact in before}
        turns = scenario_lib.history(scenario, ONBOARDING_WELCOME)
        started = time.monotonic()
        response, error, memory_s = None, None, None
        brief = None
        try:
            brief, _ = agent.situation_brief(db, scenario_lib.CLIENT_ID, scenario["message"])
            response = agent.run_turn(
                scenario["message"], client_id=scenario_lib.CLIENT_ID, db_path=db,
                model=options.model, history=turns, timeout=options.timeout,
                profile_empty=not before,
                profile=agent.profile_state(db, scenario_lib.CLIENT_ID), brief=brief,
                web_search=scenario.get("web_search", options.web_search),
                reasoning=options.reasoning, ephemeral=True, defer_memory=True,
            )
        except agent.AgentError as exc:
            error = str(exc)
        elapsed = round(time.monotonic() - started, 1)
        if response:
            saved_at = time.monotonic()
            try:
                agent.remember_exchange(scenario["message"], response, client_id=scenario_lib.CLIENT_ID,
                                        db_path=db, model=options.model, brief=brief)
            except agent.AgentError as exc:
                error = f"memory step: {exc}"
            memory_s = round(time.monotonic() - saved_at, 1)
        after = service.inspect(scenario_lib.CLIENT_ID)["facts"]
    events = agent.parse_events("\n".join(getattr(_captured, "lines", [])))
    return {
        "scenario_id": scenario["id"],
        "scenario": scenario,
        "seeded_facts": [{"key": f["key"], "value": f["value"], "stale": f.get("stale", False)} for f in before],
        "history": [list(turn) for turn in turns],
        "user": scenario["message"],
        "response": response,
        "error": error,
        "tools": list(events.tools),
        "saved_keys": sorted({f["key"] for f in after if f["id"] not in before_ids}),
        "elapsed_s": elapsed,
        "memory_s": memory_s,
        "model": agent.resolve_model(options.model),
        "reasoning": options.reasoning,
    }


# --- scoring ----------------------------------------------------------------

def score(transcript: dict[str, Any], judge: Callable[[str], str] | None) -> dict[str, Any]:
    scenario = transcript.get("scenario") or {}
    expect = scenario.get("expect") or {}
    response = transcript.get("response")
    if transcript.get("error") or not response:
        findings = [Finding("agent_error", "fail", (transcript.get("error") or "no response")[:300])]
    else:
        findings = run_checks(response, user=transcript.get("user", ""), expect=expect,
                              language=scenario.get("language"), history=transcript.get("history") or ())
        if "saved_keys" in transcript:
            findings += memory_findings(transcript["saved_keys"], expect)
    judged = None
    if judge is not None and response:
        try:
            judged = parse_scores(judge(score_prompt(transcript)))
        except (JudgeError, ValueError) as exc:
            judged = {"error": str(exc)}
    return {
        "scenario_id": transcript.get("scenario_id"),
        "language": scenario.get("language") or (detect_language(response) if response else None),
        "category": scenario.get("category"),
        "words": shape(response)["words"] if response else 0,
        "verdict": verdict(findings),
        "findings": [finding.to_dict() for finding in findings],
        "judge": judged,
    }


def _judge_mean(result: dict[str, Any]) -> float | None:
    judged = result.get("judge") or {}
    return judged.get("mean")


# --- transcripts on disk ----------------------------------------------------

def load_transcripts(paths: Sequence[str], data: dict | None) -> list[dict[str, Any]]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            folder = path / "transcripts" if (path / "transcripts").is_dir() else path
            files.extend(sorted(folder.glob("*.json")))
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"not found: {raw}")
    known = {s["id"]: s for s in (data or {}).get("scenarios", [])}
    transcripts = []
    for file in files:
        record = json.loads(file.read_text(encoding="utf-8"))
        record = record.get("transcript", record)
        if not isinstance(record, dict) or "response" not in record:
            continue
        record.setdefault("scenario_id", file.stem)
        if record["scenario_id"] in known:  # rescore against the current expectations
            record["scenario"] = known[record["scenario_id"]]
        transcripts.append(record)
    if not transcripts:
        raise SystemExit("no transcripts found")
    return transcripts


def _out_dir(options: argparse.Namespace, suffix: str = "") -> Path:
    if options.out:
        out = Path(options.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        label = "-".join(part for part in (options.label, suffix) if part)
        out = REPORTS_DIR / (stamp + (f"-{label}" if label else ""))
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_report(out: Path, transcripts: list[dict], results: list[dict], meta: dict) -> None:
    folder = out / "transcripts"
    folder.mkdir(exist_ok=True)
    for transcript, result in zip(transcripts, results):
        sample = transcript.get("sample")
        name = transcript["scenario_id"] + (f"~{sample}" if sample else "")
        (folder / f"{name}.json").write_text(
            json.dumps({"transcript": transcript, "result": result}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    (out / "results.json").write_text(
        json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(summary(transcripts, results, meta), encoding="utf-8")


# --- summaries --------------------------------------------------------------

def check_counts(results: Iterable[dict]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for result in results:
        seen = set()
        for finding in result["findings"]:
            key = (finding["check"], finding["severity"])
            if key in seen:
                continue
            seen.add(key)
            counts.setdefault(finding["check"], {"fail": 0, "warn": 0})[finding["severity"]] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1]["fail"], -item[1]["warn"], item[0])))


def summary(transcripts: list[dict], results: list[dict], meta: dict, worst: int = 5) -> str:
    lines = [f"# Wealth conversation eval ({meta.get('mode')})", ""]
    for key in ("model", "reasoning", "judge", "instructions", "instructions_sha256", "scenarios"):
        if meta.get(key) is not None:
            lines.append(f"- {key}: {meta[key]}")
    verdicts = [r["verdict"] for r in results]
    lines += ["", f"Deterministic: {verdicts.count('pass')} pass, {verdicts.count('warn')} warn, "
              f"{verdicts.count('fail')} fail of {len(results)}", ""]
    lines.append(f"{'scenario':34} {'lang':4} {'words':>5} {'verdict':7} {'judge':>5}  checks")
    for result in results:
        fails = sorted({f["check"] for f in result["findings"] if f["severity"] == "fail"})
        warns = sorted({f["check"] for f in result["findings"] if f["severity"] == "warn"})
        mean = _judge_mean(result)
        flagged = ", ".join(fails + [f"({w})" for w in warns])
        lines.append(f"{str(result['scenario_id'])[:34]:34} {str(result.get('language') or '-'):4} "
                     f"{result['words']:>5} {result['verdict']:7} "
                     f"{(f'{mean:.2f}' if mean is not None else '-'):>5}  {flagged}")
    counts = check_counts(results)
    if counts:
        lines += ["", "Checks (scenarios affected): fail / warn"]
        lines += [f"  {name:28} {c['fail']:>3} / {c['warn']:<3}" for name, c in counts.items()]
    judged = [r["judge"]["scores"] for r in results if (r.get("judge") or {}).get("scores")]
    if judged:
        lines += ["", "Judge means by dimension"]
        for name in DIMENSIONS:
            lines.append(f"  {name:16} {sum(s[name] for s in judged) / len(judged):.2f}")
        overall = [sum(s.values()) / len(s) for s in judged]
        mean = sum(overall) / len(overall)
        spread = (sum((x - mean) ** 2 for x in overall) / (len(overall) - 1)) ** 0.5 if len(overall) > 1 else 0.0
        lines.append(f"  {'overall':16} {mean:.2f}  (n={len(overall)}, standard error {spread / len(overall) ** 0.5:.2f})")
        by_scenario: dict[str, list[float]] = {}
        for result in results:
            value = _judge_mean(result)
            if value is not None:
                by_scenario.setdefault(str(result["scenario_id"]), []).append(value)
        if any(len(v) > 1 for v in by_scenario.values()):
            lines += ["", "By scenario (mean of samples)"]
            for sid, values in sorted(by_scenario.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
                lines.append(f"  {sid[:34]:34} {sum(values) / len(values):.2f}  {' '.join(f'{v:.1f}' for v in values)}")
    errors = [r for r in results if (r.get("judge") or {}).get("error")]
    if errors:
        lines.append(f"\nJudge errors: {len(errors)}")
    ranked = sorted(
        zip(transcripts, results),
        key=lambda pair: (_judge_mean(pair[1]) if _judge_mean(pair[1]) is not None else 6,
                          -sum(f["severity"] == "fail" for f in pair[1]["findings"])),
    )
    ranked = [pair for pair in ranked if pair[1]["verdict"] != "pass" or _judge_mean(pair[1]) is not None]
    if ranked and worst:
        lines += ["", f"Worst {min(worst, len(ranked))}"]
        for transcript, result in ranked[:worst]:
            text = " ".join((transcript.get("response") or transcript.get("error") or "").split())
            lines += ["", f"## {result['scenario_id']}  (judge {_judge_mean(result) or '-'})",
                      f"user: {' '.join(str(transcript.get('user', '')).split())[:160]}",
                      f"reply: {text[:PREVIEW_CHARS]}{'…' if len(text) > PREVIEW_CHARS else ''}"]
            for finding in result["findings"]:
                lines.append(f"  - [{finding['severity']}] {finding['check']}: {finding['detail']}")
            fix = (result.get("judge") or {}).get("fix")
            if fix:
                lines.append(f"  - judge fix: {fix}")
    return "\n".join(lines) + "\n"


# --- modes ------------------------------------------------------------------

def _parallel(function, items, jobs):
    if jobs <= 1:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(function, items))


def _instructions(options: argparse.Namespace) -> Path:
    path = Path(options.instructions).expanduser().resolve() if options.instructions else agent.INSTRUCTIONS_PATH
    if not path.is_file():
        raise SystemExit(f"instructions file not found: {path}")
    return path


def mode_run(options, data) -> Path:
    chosen = scenario_lib.select(data, options.scenarios)
    instructions = _instructions(options)
    agent.INSTRUCTIONS_PATH = instructions  # build_command reads it per call
    _install_capture()
    judge = make_judge(options.judge, reasoning=options.judge_reasoning)
    print(f"Running {len(chosen)} scenario(s) with {agent.resolve_model(options.model)} "
          f"({options.reasoning}); judge {getattr(judge, 'label', 'none')}", file=sys.stderr)

    def one(item):
        scenario, sample = item
        transcript = run_scenario(data, scenario, options)
        if options.samples > 1:
            transcript["sample"] = sample
        result = score(transcript, judge)
        print(f"  {scenario['id']}{f' #{sample}' if options.samples > 1 else ''}: {result['verdict']}", file=sys.stderr)
        return transcript, result

    work = [(scenario, sample) for scenario in chosen for sample in range(1, options.samples + 1)]
    pairs = _parallel(one, work, options.jobs)
    transcripts, results = [p[0] for p in pairs], [p[1] for p in pairs]
    meta = {"mode": "run", "model": agent.resolve_model(options.model), "reasoning": options.reasoning,
            "judge": getattr(judge, "label", "none"), "instructions": str(instructions),
            "instructions_sha256": hashlib.sha256(instructions.read_bytes()).hexdigest()[:12],
            "scenarios": options.scenarios, "web_search": options.web_search, "samples": options.samples}
    out = _out_dir(options)
    write_report(out, transcripts, results, meta)
    return out


def mode_replay(options, data) -> Path:
    transcripts = load_transcripts(options.replay, data)
    judge = make_judge(options.judge, reasoning=options.judge_reasoning)
    results = _parallel(lambda t: score(t, judge), transcripts, options.jobs)
    meta = {"mode": "replay", "judge": getattr(judge, "label", "none"),
            "source": [str(p) for p in options.replay]}
    out = _out_dir(options, "replay")
    write_report(out, transcripts, results, meta)
    return out


def mode_compare(options, data) -> Path:
    judge = make_judge(options.judge, reasoning=options.judge_reasoning)
    if judge is None:
        raise SystemExit("--compare needs a judge (for example --judge codex)")
    first = {t["scenario_id"]: t for t in load_transcripts([options.compare[0]], data)}
    second = {t["scenario_id"]: t for t in load_transcripts([options.compare[1]], data)}
    shared = [sid for sid in first if sid in second and first[sid].get("response") and second[sid].get("response")]
    if not shared:
        raise SystemExit("the two runs share no answered scenarios")

    def one(sid):
        base, other = first[sid], second[sid]
        orders = [False, True] if options.both_orders else [random.Random(sid).random() < 0.5]
        verdicts = []
        for swapped in orders:
            a, b = (other["response"], base["response"]) if swapped else (base["response"], other["response"])
            try:
                verdicts.append(parse_pairwise(judge(pairwise_prompt(base, a, b)), swapped))
            except (JudgeError, ValueError) as exc:
                return {"scenario_id": sid, "winner": "error", "reason": str(exc)}
        winners = {v["winner"] for v in verdicts}
        winner = verdicts[0]["winner"] if len(winners) == 1 else "tie"
        return {"scenario_id": sid, "winner": winner, "margin": verdicts[0].get("margin"),
                "reason": verdicts[0].get("reason"), "dimensions": verdicts[0].get("dimensions"),
                "orders": verdicts}

    outcomes = _parallel(one, shared, options.jobs)
    tally = {key: sum(o["winner"] == key for o in outcomes) for key in ("A", "B", "tie", "error")}
    lines = [f"# Pairwise: A={options.compare[0]}  B={options.compare[1]}",
             f"A wins {tally['A']}, B wins {tally['B']}, ties {tally['tie']}, errors {tally['error']}", ""]
    lines += [f"{o['scenario_id']:30} {o['winner']:5} {o.get('margin') or '':9} {o.get('reason') or ''}"
              for o in outcomes]
    out = _out_dir(options, "compare")
    (out / "compare.json").write_text(json.dumps({"a": options.compare[0], "b": options.compare[1],
                                                  "tally": tally, "outcomes": outcomes},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def mode_list(data) -> None:
    for s in data["scenarios"]:
        print(f"{s['id']:30} {s['language']:3} {s['residence']:8} {s['category']:22} "
              f"{s.get('seed') or '-':10} {' '.join(s['message'].split())[:60]}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--replay", nargs="+", metavar="PATH", help="rescore saved transcripts or report dirs")
    mode.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"), help="pairwise A/B of two runs")
    mode.add_argument("--list", action="store_true", help="list scenarios")
    p.add_argument("--scenarios", default="all")
    p.add_argument("--scenario-file", default=str(scenario_lib.SCENARIOS_PATH))
    p.add_argument("--model", default="default", help="agent model: default (your Codex config), sol, luna or a full Codex model ID")
    p.add_argument("--reasoning", default="low", choices=agent.REASONING_LEVELS)
    p.add_argument("--judge", default="none", help="none | codex | codex:<model> | cmd:<command>")
    p.add_argument("--judge-reasoning", default="medium", choices=agent.REASONING_LEVELS)
    p.add_argument("--jobs", type=int, default=2, help="parallel agent turns / judge calls")
    p.add_argument("--timeout", type=float, default=agent.DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--web-search", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--instructions", help="alternative instructions file for prompt iteration")
    p.add_argument("--both-orders", action="store_true", help="pairwise: judge both orders; disagreement is a tie")
    p.add_argument("--label", default="", help="suffix for the report directory")
    p.add_argument("--out", help="explicit report directory")
    p.add_argument("--worst", type=int, default=5)
    p.add_argument("--samples", type=int, default=1, help="runs per scenario; the summary averages them")
    return p


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    data = scenario_lib.load(options.scenario_file)
    if options.list:
        mode_list(data)
        return 0
    if options.compare:
        out = mode_compare(options, data)
        print((out / "summary.md").read_text(encoding="utf-8"))
    else:
        out = mode_replay(options, data) if options.replay else mode_run(options, data)
        results = json.loads((out / "results.json").read_text(encoding="utf-8"))
        transcripts = [json.loads(p.read_text(encoding="utf-8"))["transcript"]
                       for p in sorted((out / "transcripts").glob("*.json"))]
        by_id = {t["scenario_id"]: t for t in transcripts}
        ordered = [by_id[r["scenario_id"]] for r in results["results"]]
        print(summary(ordered, results["results"], results["meta"], options.worst))
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
