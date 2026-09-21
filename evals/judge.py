"""Pluggable judge backends. A judge maps a prompt string to reply text.

Specs:
  codex            codex exec with the user's default Codex model (from config.toml)
  codex:<model>    codex exec with an explicit model or alias (sol, luna, ...)
  cmd:<command>    any shell command that reads the prompt on stdin and prints the reply
  none             deterministic checks only
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import tomllib
from typing import Callable

from wealth.agent import parse_events, resolve_model

Judge = Callable[[str], str]


class JudgeError(RuntimeError):
    pass


def default_codex_model() -> str | None:
    """The model the user configured for Codex, or None to let Codex decide."""

    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    model = config.get("model")
    return model if isinstance(model, str) and model else None


def codex_command(model: str | None, workdir: str, reasoning: str = "medium") -> list[str]:
    command = [
        "codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "read-only", "--json", "-C", workdir,
        "-c", f'model_reasoning_effort="{reasoning}"',
        "-c", "project_doc_max_bytes=0",
        "-c", "features.shell_tool=false",
        "-c", "features.apps=false",
        "-c", "features.plugins=false",
        "-c", "features.multi_agent=false",
        "-c", "features.skip_host_skill_discovery=true",
        "-c", 'web_search="disabled"',
    ]
    if model:
        command += ["--model", resolve_model(model)]
    return command + ["-"]


def codex_judge(model: str | None = None, *, reasoning: str = "medium", timeout: float = 300) -> Judge:
    chosen = model or default_codex_model()

    def judge(prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="wealth-judge-") as workdir:
            try:
                result = subprocess.run(
                    codex_command(chosen, workdir, reasoning), input=prompt, text=True,
                    capture_output=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise JudgeError(f"judge did not finish within {timeout:g}s") from exc
        events = parse_events(result.stdout)
        if result.returncode != 0 or not events.messages:
            detail = events.errors[-1] if events.errors else f"exit status {result.returncode}"
            raise JudgeError(f"codex judge failed: {detail}")
        return events.messages[-1]

    judge.label = f"codex:{chosen or 'codex-default'}"  # type: ignore[attr-defined]
    return judge


def command_judge(command: str, *, timeout: float = 300) -> Judge:
    def judge(prompt: str) -> str:
        try:
            result = subprocess.run(shlex.split(command), input=prompt, text=True,
                                    capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise JudgeError(f"judge command did not finish within {timeout:g}s") from exc
        if result.returncode != 0:
            raise JudgeError(f"judge command exited {result.returncode}: {result.stderr.strip()[:300]}")
        return result.stdout

    judge.label = f"cmd:{command}"  # type: ignore[attr-defined]
    return judge


def make_judge(spec: str | None, *, reasoning: str = "medium", timeout: float = 300) -> Judge | None:
    if not spec or spec == "none":
        return None
    if spec == "codex":
        return codex_judge(None, reasoning=reasoning, timeout=timeout)
    if spec.startswith("codex:"):
        return codex_judge(spec.split(":", 1)[1] or None, reasoning=reasoning, timeout=timeout)
    if spec.startswith("cmd:"):
        return command_judge(spec.split(":", 1)[1], timeout=timeout)
    raise ValueError("judge must be codex, codex:<model>, cmd:<command> or none")
