# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yfinance>=0.2.50",
#   "pandas",
#   "numpy",
#   "scipy",
#   "statsmodels",
#   "openpyxl",
#   "pypdf",
# ]
# ///
"""Compatibility launcher for the packaged wealth analytics engine.

The engine (``wealth/legacy.py``) and its presentation layer
(``wealth/render.py``) are executed in this module's globals deliberately:
historical tests and callers that monkeypatch this module continue to affect
invoked functions. ``__file__`` remains this launcher, preserving the engine's
repository ROOT. The engine's own ``__main__`` block is suppressed until the
renderer is loaded, then the CLI runs once.
"""
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent.parent / "wealth"
_LAUNCHER_NAME = __name__
__name__ = "wealth_engine"  # noqa: A001 - keep the engine's __main__ block inert while loading
for _source in (_PACKAGE / "legacy.py", _PACKAGE / "render.py"):
    exec(compile(_source.read_bytes(), str(_source), "exec"), globals())
__name__ = _LAUNCHER_NAME  # noqa: A001

if __name__ == "__main__":
    cli_entry()  # noqa: F821 - defined by the engine source above
