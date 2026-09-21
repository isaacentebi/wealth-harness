"""Scenario loading, validation, selection and memory seeding."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from wealth.service import WealthService
from wealth.store import review_days

from .checks import DEFAULTS

SCENARIOS_PATH = Path(__file__).with_name("scenarios.json")
CLIENT_ID = "profile-7f3a"  # neutral on purpose: the agent must not infer a name from it
LANGUAGES = {"en", "es"}
RESIDENCES = {"US", "MX", "unknown"}
STRUCTURES = {"auto", "none", "light", "allowed"}
SOURCES = {"auto", "required", "none"}
EXPECT_KEYS = set(DEFAULTS) | {"saves", "no_saves"}
SCENARIO_KEYS = {
    "id", "category", "language", "residence", "seed", "stale", "history", "welcome",
    "message", "expect", "ideal", "web_search",
}
FRESH_DAYS_AGO = 7
OBSERVED = "@observed"


class ScenarioError(ValueError):
    pass


def _today() -> date:
    return datetime.now(timezone.utc).date()


def load(path: str | Path = SCENARIOS_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(data)
    return data


def validate(data: dict[str, Any]) -> None:
    seeds = data.get("seeds", {})
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ScenarioError("scenarios must be a nonempty list")
    for name, facts in seeds.items():
        if not isinstance(facts, list) or not facts:
            raise ScenarioError(f"seed {name!r} must be a nonempty list of facts")
        for fact in facts:
            if not {"key", "value"} <= set(fact):
                raise ScenarioError(f"seed {name!r} fact needs key and value")
    seen: set[str] = set()
    for scenario in scenarios:
        sid = scenario.get("id")
        if not isinstance(sid, str) or not sid:
            raise ScenarioError("every scenario needs an id")
        if sid in seen:
            raise ScenarioError(f"duplicate scenario id {sid!r}")
        seen.add(sid)
        unknown = set(scenario) - SCENARIO_KEYS
        if unknown:
            raise ScenarioError(f"{sid}: unknown fields {sorted(unknown)}")
        for field in ("category", "message", "ideal"):
            if not isinstance(scenario.get(field), str) or not scenario[field].strip():
                raise ScenarioError(f"{sid}: {field} is required")
        if scenario.get("language") not in LANGUAGES:
            raise ScenarioError(f"{sid}: language must be one of {sorted(LANGUAGES)}")
        if scenario.get("residence") not in RESIDENCES:
            raise ScenarioError(f"{sid}: residence must be one of {sorted(RESIDENCES)}")
        seed = scenario.get("seed")
        if seed is not None and seed not in seeds:
            raise ScenarioError(f"{sid}: unknown seed {seed!r}")
        seeded_keys = {fact["key"] for fact in seeds.get(seed, [])}
        for key in scenario.get("stale", []):
            if key not in seeded_keys:
                raise ScenarioError(f"{sid}: stale key {key!r} is not in seed {seed!r}")
        for turn in scenario.get("history", []):
            if (not isinstance(turn, list) or len(turn) != 2
                    or turn[0] not in {"user", "assistant"} or not isinstance(turn[1], str)):
                raise ScenarioError(f"{sid}: history turns are [role, text] pairs")
        expect = scenario.get("expect", {})
        unknown = set(expect) - EXPECT_KEYS
        if unknown:
            raise ScenarioError(f"{sid}: unknown expect fields {sorted(unknown)}")
        band = expect.get("words")
        if band is not None and not (isinstance(band, list) and len(band) == 2 and 0 <= band[0] < band[1]):
            raise ScenarioError(f"{sid}: expect.words must be [min, max]")
        if expect.get("structure", "auto") not in STRUCTURES:
            raise ScenarioError(f"{sid}: expect.structure must be one of {sorted(STRUCTURES)}")
        if expect.get("sources", "auto") not in SOURCES:
            raise ScenarioError(f"{sid}: expect.sources must be one of {sorted(SOURCES)}")


def select(data: dict[str, Any], spec: str = "all") -> list[dict[str, Any]]:
    """Select scenarios: all, comma-separated ids, or field:value filters.

    Filters (category:, lang:, residence:, seed:) combine with AND; ids with OR.
    """

    scenarios = data["scenarios"]
    if spec in {"", "all"}:
        return list(scenarios)
    ids, filters = set(), []
    for part in (piece.strip() for piece in spec.split(",")):
        if not part:
            continue
        if ":" in part:
            field, value = part.split(":", 1)
            field = {"lang": "language"}.get(field, field)
            filters.append((field, value))
        else:
            ids.add(part)
    known = {scenario["id"] for scenario in scenarios}
    missing = ids - known
    if missing:
        raise ScenarioError(f"unknown scenario ids: {sorted(missing)}")
    chosen = [
        scenario for scenario in scenarios
        if (not ids or scenario["id"] in ids)
        and all(str(scenario.get(field)) == value for field, value in filters)
    ]
    if not chosen:
        raise ScenarioError(f"no scenarios match {spec!r}")
    return chosen


def _dated(value: Any, observed: str) -> Any:
    """Replace the "@observed" placeholder with the fact's observation date."""

    if value == OBSERVED:
        return observed
    if isinstance(value, dict):
        return {k: _dated(v, observed) for k, v in value.items()}
    if isinstance(value, list):
        return [_dated(v, observed) for v in value]
    return value


def seed_facts(data: dict[str, Any], scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Facts ready for WealthService.remember, with stale keys backdated."""

    name = scenario.get("seed")
    if not name:
        return []
    stale = set(scenario.get("stale", []))
    today = _today()
    facts = []
    for fact in data["seeds"][name]:
        key = fact["key"]
        days_ago = review_days(key) + 45 if key in stale else fact.get("days_ago", FRESH_DAYS_AGO)
        observed = (today - timedelta(days=days_ago)).isoformat()
        facts.append({
            "key": key,
            "value": _dated(fact["value"], observed),
            "source": {"kind": fact.get("kind", "user"), "ref": "evaluation seed", "observed_on": observed},
            "confidence": fact.get("confidence", "reported"),
        })
    return facts


def prepare_profile(data: dict[str, Any], scenario: dict[str, Any], db_path: str | Path) -> WealthService:
    service = WealthService(db_path)
    service.create(CLIENT_ID, "Evaluation profile")
    facts = seed_facts(data, scenario)
    if facts:
        service.remember(CLIENT_ID, facts)
    return service


def uses_welcome(scenario: dict[str, Any]) -> bool:
    return scenario.get("welcome", not scenario.get("seed") and not scenario.get("history"))


def history(scenario: dict[str, Any], welcome: str) -> list[tuple[str, str]]:
    turns = [(role, text) for role, text in scenario.get("history", [])]
    return ([("assistant", welcome)] if uses_welcome(scenario) else []) + turns


def categories(scenarios: Iterable[dict[str, Any]]) -> set[str]:
    return {scenario["category"] for scenario in scenarios}
