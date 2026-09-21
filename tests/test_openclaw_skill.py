"""The OpenClaw skill: frontmatter OpenClaw can parse, declared requirements, and the chat policy."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from wealth.server import build_server

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "integrations" / "openclaw" / "skills" / "wealth" / "SKILL.md"


def _frontmatter(text: str) -> tuple[dict, str]:
    """OpenClaw's single-line frontmatter reading: `key: value` lines, metadata as one-line JSON."""
    assert text.startswith("---\n")
    head, body = text[4:].split("\n---\n", 1)
    fields = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        assert key and value, f"frontmatter line is not key: value: {line!r}"
        fields[key.strip()] = value.strip()
    return fields, body


def test_frontmatter_has_required_fields_and_requirements():
    fields, body = _frontmatter(SKILL.read_text(encoding="utf-8"))
    assert fields["name"] == "wealth" == SKILL.parent.name
    assert 40 < len(fields["description"]) < 400
    meta = json.loads(fields["metadata"])  # single-line JSON, as OpenClaw documents
    spec = meta["openclaw"]
    assert spec["requires"]["bins"] == ["uv"]
    assert {"WEALTH_HOME", "WEALTH_DB"} <= set(spec["requires"]["env"])
    assert set(spec["os"]) <= {"darwin", "linux", "win32"}
    assert all({"id", "kind", "bins"} <= set(item) for item in spec.get("install", []))
    assert body.strip().startswith("# Wealth")


def test_the_tools_the_skill_names_exist(tmp_path):
    body = SKILL.read_text(encoding="utf-8")
    named = set(re.findall(r"`(wealth_[a-z_]+)", body))
    served = {tool.name for tool in asyncio.run(build_server(str(tmp_path / "w.sqlite3")).list_tools())}
    assert named and named <= served


def test_policy_for_chat_channels_is_in_the_skill():
    body = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "No markdown tables",                      # WhatsApp and iMessage
        "ask at most one question",
        "numbered options",                        # onboarding
        "Views as images",
        "never say or imply that you bought",      # no trades claimed
        "Save only after an explicit yes",         # statements
        "Never echo account",                      # privacy
        "mcp.servers",
        "onboarding answer",
    ):
        assert phrase in body, phrase
    # The shell commands keep the person's words out of double quotes.
    assert "--text '<their reply>'" in body
    assert not re.search(r"--text \"", body)
