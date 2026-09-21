"""The person's canonical financial picture.

* :mod:`.schema` — the fact contract and ``validate`` (called by the store on
  every write under a canonical key).
* :mod:`.model` — ``build(snapshot, ledger, today)``: the one reader of the
  model; legacy shapes are adapted here.
* :mod:`.text` — ``brief`` (per-turn prompt block) and ``sentences`` (profile memory).
* :mod:`.insights` — statement insights after an upload and the picture after a confirm.
* :mod:`.plans` — debt payoff and the plan/calendar inputs derived from the model.
"""
from __future__ import annotations

from .insights import picture_delta, statement_insights
from .model import build, missing_for_onboarding
from .plans import calendar_inputs, debt_payoff, plan_inputs
from .schema import SCHEMA, SchemaError, validate
from .text import brief, sentences

__all__ = ["SCHEMA", "SchemaError", "brief", "build", "calendar_inputs", "debt_payoff", "missing_for_onboarding",
           "picture_delta", "plan_inputs", "sentences", "statement_insights", "validate"]
