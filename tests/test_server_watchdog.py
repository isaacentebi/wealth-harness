"""The MCP server exits once its host is gone, even when Stop's killpg cannot reach its process group."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups and reparenting are POSIX")


def _env(tmp_path):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TMPDIR")}
    env.update(HOME=str(tmp_path), WEALTH_DB=str(tmp_path / "w.sqlite3"), XDG_DATA_HOME=str(tmp_path / "data"),
               WEALTH_OFFLINE="1")
    return env


def _gone(pid, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_stdin_eof_ends_the_server_promptly(tmp_path):
    server = subprocess.Popen([sys.executable, "-m", "wealth.server"], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env(tmp_path),
                              start_new_session=True)
    try:
        time.sleep(1.0)  # started and serving
        assert server.poll() is None
        started = time.monotonic()
        server.stdin.close()
        server.wait(timeout=5)
        assert time.monotonic() - started < 3
    finally:
        if server.poll() is None:
            server.kill()


_LAUNCHER = """
import subprocess, sys
server = subprocess.Popen([sys.executable, "-m", "wealth.server"], stdin=int(sys.argv[1]), pass_fds=(int(sys.argv[1]),),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
print(server.pid, flush=True)
import time; time.sleep(1.5)  # the server is up and serving when its host dies
"""


def test_a_server_whose_host_died_exits(tmp_path):
    """Its stdin stays open (the test holds the pipe), so only the parent change can end it."""
    read_end, write_end = os.pipe()
    try:
        launcher = subprocess.run([sys.executable, "-c", _LAUNCHER, str(read_end)], pass_fds=(read_end,),
                                  capture_output=True, text=True, env=_env(tmp_path), timeout=30)
        pid = int(launcher.stdout.strip())  # the launcher has exited: the server is reparented
        assert _gone(pid, 8), "the orphaned MCP server kept running"
    finally:
        os.close(read_end)
        os.close(write_end)
