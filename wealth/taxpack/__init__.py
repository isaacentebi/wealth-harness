"""The annual tax pack: what a person hands their contador (Mexico) or CPA (US).

``tax_pack`` builds one year's working papers from the transaction ledger and
the saved facts; it never prepares or files a return.  Numbers come from the
existing engines and dated tables:

* Mexico (persona fisica): Art. 129 sales per broker with cost updated by INPC
  (average cost, CFF Art. 17-A factor), the net result, the 10% and loss
  carryforwards; interest nominal and real per institution with the ISR
  withheld; domestic and foreign dividends; foreign securities at a foreign
  broker through :func:`wealth.mexico.foreign_securities`; Art. 151/185
  deductions through :func:`wealth.mexico.personal_deductions` with the CFDI
  checklist; aguinaldo/PTU exemptions only when stated.
* US: a Form 8949-style list of lots with wash-sale adjustments (code W),
  Schedule D totals and carryovers, 1099-DIV/INT summaries, foreign tax paid
  (Form 1116 inputs), IRA/Roth contributions against the limit, RMDs taken, and
  FBAR/Form 8938 flags for foreign accounts.

Honesty rules: unknown is never zero (a figure that cannot be computed is
``None`` and appears in ``pendientes``); where an institution's constancia or
1099 was saved (``constancia.<id>``) it is the source of truth, and the pack
shows our computation, the document and the difference.

Saved facts read: ``client.profile``, ``tax.<year>`` (the year's stated tax
facts), ``constancia.<id>`` (documents), ``income.<id>`` (aguinaldo/PTU),
``cash.<id>``/``investment.<id>`` (foreign-account values).  See
:data:`wealth.situation.schema.SCHEMA` for their shapes.
"""
from __future__ import annotations

from .sources import (  # noqa: F401
    ZERO, CENT, FX_AGE_DAYS, LISR, CFF_URL, SAT_USO_CFDI_URL, SAT_ANUAL_URL, IRS_8949_URL, IRS_SCHED_D_URL,
    IRS_PUB_550_URL, IRS_1116_URL, IRS_1099DIV_URL, IRS_PUB_590A_URL, IRS_PUB_590B_URL, FINCEN_FBAR_URL,
    ECFR_FBAR_URL, ECFR_FBAR_DUE_URL, IRS_8938_URL, IRS_8938_VS_FBAR_URL, TREASURY_RATES_URL, SRC_ART129,
    SRC_ART22_23, SRC_CFF17A, SRC_INPC, SRC_ART133_136, SRC_ART55, SRC_ART140, SRC_ART142V, SRC_ART5,
    SRC_ART151, SRC_ART93_XIV, SRC_USO_CFDI, SRC_ANUAL, SRC_8949, SRC_SCHED_D, SRC_PUB550, SRC_RR_2008_5,
    SRC_1099DIV, SRC_1116, SRC_590A, SRC_590B, SRC_FBAR, SRC_FBAR_DUE, SRC_8938, SRC_TREASURY_RATES,
    FBAR_THRESHOLD_USD, FORM_8938_THRESHOLDS, ART140_GROSS_UP, ART140_CORPORATE_RATE, US_CAPITAL_LOSS_LIMIT,
    US_DEFERRAL_LIMITS, CFDI_CHECKLIST, DOCUMENT_BLOCKS,
)
from .render import (  # noqa: F401
    csv_files, render_html, write_exports,
)
from .task import (  # noqa: F401
    ALLOWED_INPUTS, run_task,
)


__all__ = ["run_task", "csv_files", "render_html", "write_exports", "FBAR_THRESHOLD_USD", "FORM_8938_THRESHOLDS"]
