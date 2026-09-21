"""CLI deletion requires a person at an interactive terminal."""
from __future__ import annotations

import io
import json

import pytest

from wealth import cli
from wealth.service import WealthService
from wealth.store import ClientNotFoundError


class _Stdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "w.sqlite3"
    WealthService(path).create("ana", "Ana")
    return path


def _exists(path) -> bool:
    try:
        WealthService(path).inspect("ana")
    except ClientNotFoundError:
        return False
    return True


def test_forget_refuses_piped_input(database, monkeypatch, capsys):
    payload = json.dumps({"client_id": "ana", "confirm_client_id": "ana"})
    monkeypatch.setattr("sys.stdin", _Stdin(payload))
    assert cli.main(["forget", "--db", str(database)]) == 2
    assert "interactive terminal" in capsys.readouterr().err
    payload = json.dumps({"action": "forget", "client_id": "ana", "inputs": {"confirm_client_id": "ana"}})
    monkeypatch.setattr("sys.stdin", _Stdin(payload))
    assert cli.main(["client", "--db", str(database)]) == 2
    assert _exists(database)


def test_forget_requires_typed_confirmation(database, monkeypatch):
    monkeypatch.setattr("sys.stdin", _Stdin(tty=True))
    monkeypatch.setattr("builtins.input", lambda: "ana")
    assert cli.main(["forget", "--db", str(database), "--client", "ana"]) == 2
    assert _exists(database)
    monkeypatch.setattr("builtins.input", lambda: "ana DELETE")
    assert cli.main(["forget", "--db", str(database), "--client", "ana"]) == 0
    assert not _exists(database)
