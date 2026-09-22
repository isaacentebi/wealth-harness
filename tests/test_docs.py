"""The documentation's claims about tasks, tools and links match the code."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from wealth import connectors
from wealth.cli_text import COMMANDS as TEXT_COMMANDS
from wealth.service import OPERATIONS, TASKS

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SKILL = ROOT / "SKILL.md"
CLI = ROOT / "docs" / "cli.md"
OWNED = [README, SKILL, ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", *(ROOT / "docs" / name for name in (
    "architecture.md", "cli.md", "agent.md", "verification.md", "open-source.md", "trading.md", "openclaw.md"))]
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
CODE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
SKIP_DIRS = {".git", ".venv", ".claude", "node_modules", "reports", "__pycache__"}


@pytest.fixture(scope="module")
def tools(tmp_path_factory) -> set[str]:
    pytest.importorskip("mcp")
    from wealth.server import build_server

    server = build_server(str(tmp_path_factory.mktemp("docs") / "w.sqlite3"))
    return {tool.name for tool in asyncio.run(server.list_tools())}


def section(path: Path, heading: str) -> str:
    """The text under a ``## heading`` up to the next ``##`` heading."""
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert match, f"{path.name} has no '## {heading}' section"
    return match.group(1)


def named_tasks(text: str, tools: set[str]) -> set[str]:
    """Backticked identifiers in the table rows of ``text`` that are not tool or connector names."""
    rows = "\n".join(line for line in text.splitlines() if line.startswith("|"))
    names = {token for token in CODE.findall(rows) if re.fullmatch(r"[a-z][a-z0-9_]*", token)}
    return names - tools - set(connectors.names())


@pytest.mark.parametrize("path, heading", [(CLI, "Tasks"), (SKILL, "Tasks by question")])
def test_task_index_names_every_task_and_only_real_ones(path, heading, tools):
    named = named_tasks(section(path, heading), tools)
    assert not named - set(TASKS), f"{path.name} names tasks that do not exist: {sorted(named - set(TASKS))}"
    assert not set(TASKS) - named, f"{path.name} omits tasks: {sorted(set(TASKS) - named)}"


def test_every_named_tool_exists_and_counts_are_right(tools):
    for path in OWNED:
        text = path.read_text(encoding="utf-8")
        for name in set(re.findall(r"\bwealth_[a-z_]+\b", text)):
            assert name in tools, f"{path.name} names a tool that does not exist: {name}"
        for word in re.findall(r"\b(\w+)[ -](?:MCP )?tools?\b", text, re.I):
            count = NUMBERS.get(word.lower()) or (int(word) if word.isdigit() else None)
            if count is not None:
                assert count == len(tools), f"{path.name} says {word} tools; the server has {len(tools)}"


def test_skill_lists_every_tool(tools):
    listed = set(re.findall(r"\bwealth_[a-z_]+\b", section(SKILL, "Tools")))
    assert listed == tools


def test_cli_reference_covers_every_command_and_tool(tools):
    cli = ROOT / "docs" / "cli.md"
    text = cli.read_text(encoding="utf-8")
    commands = section(cli, "CLI commands")
    for operation in (*OPERATIONS, "watch", *TEXT_COMMANDS):
        assert f"`{operation}`" in commands, f"docs/cli.md does not list the {operation} command"
        assert re.search(rf"uv run wealth {operation}\b", commands), f"docs/cli.md has no example for {operation}"
    for tool in tools:
        assert f"`{tool}`" in section(cli, "MCP tools"), f"docs/cli.md omits {tool}"
    for name in connectors.names():
        assert f"`{name}`" in text


def test_no_stale_claims():
    stale = ("six tools", "six-tool", "eight tools", "eight mcp tools", "no brokerage connection",
             "never trades", "no order placement", "trading connection", "not implemented yet")
    for path in OWNED:
        text = path.read_text(encoding="utf-8").lower()
        for phrase in stale:
            assert phrase not in text, f"{path.name} still says {phrase!r}"


def _slug(heading: str) -> str:
    heading = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading))


def _markdown_files() -> list[Path]:
    """Tracked Markdown only: private, ignored notes are not part of the published docs."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    return [ROOT / name for name in sorted(tracked) if not SKIP_DIRS & set(Path(name).parts)]


def test_internal_markdown_links_resolve():
    broken = []
    for path in _markdown_files():
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        for target in LINK.findall(text):
            if re.match(r"[a-z]+:", target):
                continue  # http(s), mailto
            file_part, _, anchor = target.partition("#")
            resolved = (path.parent / file_part) if file_part else path
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
                continue
            if anchor and resolved.suffix == ".md":
                headings = re.findall(r"^#+ (.+)$", resolved.read_text(encoding="utf-8"), re.M)
                if anchor not in {_slug(h) for h in headings}:
                    broken.append(f"{path.relative_to(ROOT)} -> {target} (no such heading)")
    assert not broken, "broken links:\n" + "\n".join(broken)


def test_one_name_and_no_private_paths():
    """The product is Wealth and the repository is wealth-harness; no stale checkout names or local paths."""
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        assert "wealth-mgmgt" not in text, f"{path.relative_to(ROOT)} still says wealth-mgmgt"
        assert not re.search(r"/Users/(?!you/)", text), f"{path.relative_to(ROOT)} contains a local path"


def test_readme_images_exist_and_stay_small():
    text = README.read_text(encoding="utf-8")
    images = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text) + re.findall(r'<img [^>]*src="([^"]+)"', text)
    assert images, "the README has no screenshots"
    local = [image for image in images if not re.match(r"[a-z]+:", image)]  # the CI badge is remote
    assert local, "the README has no screenshots"
    for image in local:
        path = ROOT / image
        assert path.exists(), image
        assert path.stat().st_size < 400_000, f"{image} is {path.stat().st_size} bytes"
