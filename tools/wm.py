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

The source is executed in this module's globals deliberately: historical tests
and callers that monkeypatch this module continue to affect invoked functions.
``__file__`` remains this launcher, preserving the engine's repository ROOT.
"""
from pathlib import Path

_PACKAGE_SOURCE = Path(__file__).resolve().parent.parent / "wealth" / "legacy.py"
exec(compile(_PACKAGE_SOURCE.read_bytes(), str(_PACKAGE_SOURCE), "exec"), globals())
