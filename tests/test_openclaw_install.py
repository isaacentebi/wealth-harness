"""integrations/openclaw/install.sh in a throwaway HOME, with a stub uv and no openclaw on PATH."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "integrations" / "openclaw" / "install.sh"
sys.path.insert(0, str(INSTALL.parent))
import openclaw_config  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")


@pytest.fixture()
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv.log"
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\ncat >/dev/null 2>&1 || true\nexit 0\n')
    uv.chmod(0o755)
    return home, bin_dir, log


def _run(home: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "LANG": "C"}
    shell = shutil.which("dash") or "sh"  # dash when present: the strictest common POSIX shell
    return subprocess.run([shell, str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def test_dry_run_touches_nothing(home):
    home, bin_dir, log = home
    config = home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_text('{"mcp": {"servers": {"other": {"command": "x"}}}}')
    before = _tree(home), config.read_text()
    result = _run(home, bin_dir, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert (_tree(home), config.read_text()) == before
    assert not log.exists()  # not even uv sync ran
    assert "[dry-run]" in result.stdout and "uv --directory" in result.stdout
    snippet = result.stdout[result.stdout.index("{"):]
    planned = json.loads(snippet[:snippet.rindex("}") + 1])
    assert planned["mcp"]["servers"]["wealth"]["args"][-1] == "wealth-mcp"
    assert set(planned["skills"]["entries"]["wealth"]["env"]) == {"WEALTH_HOME", "WEALTH_DB", "WEALTH_UPLOAD_DIR",
                                                                   "WEALTH_VIEW_DIR"}
    uninstall = _run(home, bin_dir, "--uninstall", "--dry-run")
    assert uninstall.returncode == 0, uninstall.stderr
    assert (_tree(home), config.read_text()) == before


def test_install_is_idempotent_keeps_other_entries_and_uninstalls(home):
    home, bin_dir, log = home
    config = home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    original = {"mcp": {"servers": {"other": {"command": "x"}}},
                "skills": {"entries": {"peekaboo": {"enabled": True}, "wealth": {"env": {"EXTRA": "1"}}}},
                "agents": {"defaults": {"workspace": "~/clawd"}}}
    config.write_text(json.dumps(original))
    result = _run(home, bin_dir, "--workspace", str(home / "ws"))
    assert result.returncode == 0, result.stderr + result.stdout
    assert "sync" in log.read_text()

    written = json.loads(config.read_text())
    assert written["mcp"]["servers"]["other"] == {"command": "x"}
    assert written["agents"] == original["agents"]
    assert written["skills"]["entries"]["peekaboo"] == {"enabled": True}
    server = written["mcp"]["servers"]["wealth"]
    assert server["command"] == "uv" and server["args"] == ["--directory", str(ROOT.resolve()), "run", "wealth-mcp"]
    env = written["skills"]["entries"]["wealth"]["env"]
    assert env["EXTRA"] == "1" and env["WEALTH_HOME"] == str(ROOT.resolve())
    data = home / ".local" / "share" / "wealth-harness"
    assert env["WEALTH_DB"] == server["env"]["WEALTH_DB"] == str(data / "clients.sqlite3")
    for directory in (data, data / "uploads", data / "uploads" / "me", data / "views"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    skill = home / "ws" / "skills" / "wealth"
    assert (skill / "SKILL.md").read_text() == (INSTALL.parent / "skills" / "wealth" / "SKILL.md").read_text()
    backups = list(config.parent.glob("openclaw.json.wealth-backup-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original
    assert "Next:" in result.stdout

    again = _run(home, bin_dir, "--workspace", str(home / "ws"))
    assert again.returncode == 0, again.stderr
    assert "already up to date" in again.stdout
    assert len(list(config.parent.glob("openclaw.json.wealth-backup-*"))) == 1
    assert json.loads(config.read_text()) == written

    gone = _run(home, bin_dir, "--uninstall", "--workspace", str(home / "ws"))
    assert gone.returncode == 0, gone.stderr
    after = json.loads(config.read_text())
    assert "wealth" not in after["mcp"]["servers"] and "wealth" not in after["skills"]["entries"]
    assert after["mcp"]["servers"]["other"] == {"command": "x"}
    assert not skill.exists() and data.is_dir()  # data is never deleted by the installer


def test_with_the_openclaw_cli_it_uses_mcp_set_and_config_set(home):
    home, bin_dir, _ = home
    calls = home.parent / "openclaw.log"
    stub = bin_dir / "openclaw"
    stub.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\t" "$a"; done >> "{calls}"\necho >> "{calls}"\n')
    stub.chmod(0o755)
    config = home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_text("{ /* json5 is fine here: the CLI edits it */ }")
    result = _run(home, bin_dir)
    assert result.returncode == 0, result.stderr
    lines = [line.rstrip("\t").split("\t") for line in calls.read_text().splitlines()]
    mcp = next(line for line in lines if line[:3] == ["mcp", "set", "wealth"])
    server = json.loads(mcp[3])
    assert server["args"][-1] == "wealth-mcp" and "WEALTH_DB" in server["env"]
    sets = {line[2]: json.loads(line[3]) for line in lines if line[:2] == ["config", "set"]}
    assert set(sets) == {f"skills.entries.wealth.env.{k}" for k in
                         ("WEALTH_HOME", "WEALTH_DB", "WEALTH_UPLOAD_DIR", "WEALTH_VIEW_DIR")}
    assert all(line[-1] == "--strict-json" for line in lines if line[:2] == ["config", "set"])
    assert len(list(config.parent.glob("openclaw.json.wealth-backup-*"))) == 1

    calls.write_text("")
    assert _run(home, bin_dir, "--uninstall").returncode == 0
    lines = [line.rstrip("\t").split("\t") for line in calls.read_text().splitlines()]
    assert ["mcp", "unset", "wealth"] in lines and ["config", "unset", "skills.entries.wealth"] in lines


def test_a_skill_it_did_not_install_is_moved_aside_not_deleted(home):
    home, bin_dir, _ = home
    foreign = home / ".openclaw" / "skills" / "wealth"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: wealth\ndescription: someone else's\n---\n")
    result = _run(home, bin_dir)
    assert result.returncode == 0, result.stderr
    moved = list(foreign.parent.glob("wealth.backup-*"))
    assert len(moved) == 1 and "someone else's" in (moved[0] / "SKILL.md").read_text()
    assert (foreign / ".installed-by-wealth").exists()


def test_json5_config_is_left_untouched(home):
    home, bin_dir, _ = home
    config = home / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    text = '// my config\n{ agents: { defaults: { workspace: "~/clawd", }, }, }\n'
    config.write_text(text)
    result = _run(home, bin_dir)
    assert result.returncode != 0
    assert config.read_text() == text
    assert '"mcp"' in result.stderr  # the snippet to paste by hand


def test_merge_and_remove_are_pure():
    base = {"mcp": {"servers": {"a": {"url": "https://x"}}}}
    merged = openclaw_config.merge(base, "/w", "/d/db", "/d/up", "/d/v")
    assert base == {"mcp": {"servers": {"a": {"url": "https://x"}}}}
    assert set(merged["mcp"]["servers"]) == {"a", "wealth"}
    assert openclaw_config.remove(merged) == {**base, "skills": {"entries": {}}}
    no_mcp = openclaw_config.merge({}, "/w", "/d/db", "/d/up", "/d/v", mcp=False)
    assert "mcp" not in no_mcp


def test_install_script_is_posix_sh():
    text = INSTALL.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert os.access(INSTALL, os.X_OK)
    for bashism in ("[[", "function ", "local ", "$'", "<<<", "declare "):
        assert bashism not in text, bashism
    checker = shutil.which("dash") or shutil.which("sh")
    assert subprocess.run([checker, "-n", str(INSTALL)], capture_output=True).returncode == 0
