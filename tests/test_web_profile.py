"""Profile page endpoints on the local chat server."""
from __future__ import annotations

import json
from datetime import date
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from wealth import web
from wealth.service import WealthService

from test_web_streaming import _post, serving


def _get(base, path):
    return urlopen(base + path, timeout=10)


def _seed(db):
    service = WealthService(db)
    service.create("personal", "personal")
    service.remember("personal", [{
        "key": "client.profile",
        "value": {"monthly_income": 85000, "currency": "MXN", "tax_residence": "Mexico"},
        "source": {"kind": "user", "ref": "chat", "observed_on": date.today().isoformat()},
    }])


def test_profile_page_and_view_are_served(tmp_path):
    db = tmp_path / "w.sqlite3"
    _seed(db)
    chat = web.Chat(db, "personal")
    with serving(chat) as (base, _):
        with _get(base, "/profile") as response:
            assert b"<html" in response.read().lower()
        with _get(base, "/api/profile") as response:
            view = json.loads(response.read())
    assert view["client"]["revision"] >= 1


def test_fact_confirm_returns_the_new_view_and_detects_conflicts(tmp_path):
    db = tmp_path / "w.sqlite3"
    _seed(db)
    chat = web.Chat(db, "personal")
    path = "/api/facts/" + quote("client.profile", safe="")
    with serving(chat) as (base, _):
        revision = json.loads(_get(base, "/api/profile").read())["client"]["revision"]
        body = json.dumps({"action": "confirm", "expected_revision": revision}).encode()
        with _post(base, path, chat.token, body) as response:
            assert response.status == 200
            assert json.loads(response.read())["profile"]["client"]["revision"] > revision
        with pytest.raises(HTTPError) as stale:
            _post(base, path, chat.token, body)
        assert stale.value.code == 409


def test_profile_writes_need_the_token_and_a_known_fact(tmp_path):
    db = tmp_path / "w.sqlite3"
    _seed(db)
    chat = web.Chat(db, "personal")
    with serving(chat) as (base, _):
        with pytest.raises(HTTPError) as forbidden:
            _post(base, "/api/facts/client.profile", "wrong", b'{"action": "confirm"}')
        assert forbidden.value.code == 403
        with pytest.raises(HTTPError) as missing:
            _post(base, "/api/facts/nope", chat.token, b'{"action": "confirm"}')
        assert missing.value.code == 422
        with pytest.raises(HTTPError) as empty:
            _post(base, "/api/profile/form", chat.token, b'{"form": {}}')
        assert empty.value.code == 400
        with pytest.raises(HTTPError) as unknown:
            urlopen(Request(base + "/api/facts/nope"), timeout=10)
        assert unknown.value.code == 422
