"""Read model for the "My profile" dashboard, plus the fact edits it can request.

Everything here is pure with respect to storage: ``profile_view`` only reads
through ``WealthService.inspect`` (and ``situation`` when the service has it);
amounts, names and "missing" come from the canonical model
(``wealth.situation.build``), so what the conversation saves is what this page
shows; ``fact_action`` and ``form_facts`` return fact
payloads that the web layer passes to ``WealthService.remember`` with the
revision the page was rendered at.  Unknown values stay ``None``; they are never zero.
The code tolerates store shape differences (missing fields, legacy values).
"""
from __future__ import annotations

from .helpers import (  # noqa: F401
    FORM_FIELDS,
)
from .classify import (  # noqa: F401
    classify_key, GROUPS, account_label, memory_groups, completeness, fact_detail,
)
from .overviews import (  # noqa: F401
    overview, situation_overview, differences, goals_view, fact_labels, upcoming,
)
from .perf import (  # noqa: F401
    time_weighted_return, xirr, performance,
)
from .memory import (  # noqa: F401
    MEMORY_TOPICS, memory_view,
)
from .picture import (  # noqa: F401
    PICTURE_TOP, PICTURE_POINTS, PICTURE_WEEKS, PICTURE_DOTS, PICTURE_PRICE_BUDGET, _instrument_label, _flow,
    _debts, _goal_funded, picture_view,
)
from .surfaces import (  # noqa: F401
    profile_view, TODAY_DUE_DAYS, SNOOZE_DAYS, _value_spans, today_view, REVIEW_QUARTERS, quarter_bounds,
    review_quarters, _pct_words, _label, _index_label, _plan_names, _review_summary, review_view,
    connections_view, export_payload,
)
from .writes import (  # noqa: F401
    fact_action, form_facts,
)


__all__ = ["profile_view", "situation_overview", "differences", "fact_detail", "fact_action", "form_facts", "time_weighted_return", "xirr", "classify_key",
           "memory_groups", "completeness", "overview", "performance", "goals_view", "upcoming",
           "today_view", "review_view", "review_quarters", "quarter_bounds", "connections_view", "export_payload"]
