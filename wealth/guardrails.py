"""Guardrails: a play-money sleeve, education cards, a panic circuit breaker, cool-off flags and a scam screen.

Everything here is deterministic and returns data for a calm, warm answer.
Nothing here trades, blocks a choice or names a trade to make: the person
decides.  Mechanics are always explained; only a *recommendation* can be
declined.

* :func:`speculation_check` sizes a speculative idea against a play-money
  policy (a cap on liquid net worth, a single-position loss limit and a
  drawdown stop) and returns ``allow`` / ``allow_with_warning`` /
  ``decline_to_recommend`` with plain reasons and the education card.
* :func:`education_card` explains options, leverage, crypto and fintech
  yield: maximum loss, liquidation and deposit-insurance coverage.
* :func:`panic_check` answers "sell everything" after a drawdown above 10%
  with what is at risk for the goals, what followed similar drawdowns, the
  tax cost of selling and a suggested cool-off.
* :func:`cool_off` flags a trade intent within an hour of a move above 5% or
  late at night local time.
* :func:`scam_check` screens a message or a transfer with fixed patterns and
  amounts and names the official places to verify.

Unknown inputs stay unknown (never zero); each rule says what it could not
check.  Every sourced parameter lives in :data:`PARAMETERS` with its source,
the date it was checked and a status (``verified``, ``statutory``,
``policy`` for Wealth's own rules, or ``needs_verification``).
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

CHECKED_ON = "2026-09-21"

# ------------------------------------------------------------------ sources

ESMA_CFD_URL = "https://www.esma.europa.eu/press-news/esma-news/esma-agrees-prohibit-binary-options-and-restrict-cfds-protect-retail-investors"
FINRA_2360_URL = "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2360"
FINRA_MARGIN_URL = "https://www.finra.org/investors/investing/investment-accounts/brokerage-accounts/margin-accounts"
FINRA_4210_URL = "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210"
OCC_ODD_URL = "https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document"
IPAB_URL = "https://www.gob.mx/ipab/articulos/que-son-el-ipab-y-el-seguro-de-depositos-bancarios-311810"
CONDUSEF_DEPOSIT_URL = "https://www.condusef.gob.mx/?p=contenido&idc=709&idcat=1"
CONDUSEF_SOFIPO_URL = "https://www.condusef.gob.mx/?p=info-sofipo&ide=6"
SIPRES_URL = "https://webapps.condusef.gob.mx/SIPRES/jsp/pub/index.jsp"
CNBV_PES_URL = "https://www.cnbv.gob.mx/Paginas/PADR%C3%93N-DE-ENTIDADES-SUPERVISADAS.aspx"
CONDUSEF_FRAUD_URL = "https://phpapps.condusef.gob.mx/fraudes_financieros/index.php"
CONDUSEF_IMPERSONATION_URL = "https://www.condusef.gob.mx/?p=contenido&idc=1090&idcat=1"
SAT_FAKE_EMAIL_URL = "https://www.sat.gob.mx/minisitio/BuscadorCorreosFalsos/buscador.html"
FTC_INVESTMENT_URL = "https://consumer.ftc.gov/articles/investment-scams"
FTC_PAYMENT_URL = "https://consumer.ftc.gov/consumer-alerts/2026/07/way-spot-scams-how-someone-asks-you-pay"
FTC_REPORT_URL = "https://reportfraud.ftc.gov"
FBI_LEVEL_UP_URL = "https://www.fbi.gov/how-we-can-help-you/victim-services/national-crimes-and-victim-resources/operation-level-up"
IC3_URL = "https://www.ic3.gov"
BROKERCHECK_URL = "https://brokercheck.finra.org"
INVESTOR_GOV_URL = "https://www.investor.gov"
SP500_MILESTONES_URL = "https://en.wikipedia.org/wiki/Closing_milestones_of_the_S%26P_500"
HARTFORD_BEAR_URL = "https://www.hartfordfunds.com/practice-management/client-conversations/managing-volatility/bear-markets.html"


def _param(value: Any, status: str, title: str, url: str | None = None, note: str | None = None) -> dict:
    row = {"value": value, "status": status, "checked_on": CHECKED_ON, "source": {"title": title}}
    if url:
        row["source"]["url"] = url
    if note:
        row["note"] = note
    return row


PARAMETERS: dict[str, dict] = {
    # Wealth's own play-money policy (docs/scope.md 1.6: a capped satellite sleeve of at most 5-10%).
    "speculation_cap_share": _param(0.05, "policy", "Wealth play-money policy (docs/scope.md 1.6)"),
    "speculation_cap_ceiling": _param(0.10, "policy", "Wealth play-money policy: the person may set up to 10%"),
    "speculation_debt_rate": _param(0.15, "policy", "Wealth: no play money while any debt costs more than 15% a year"),
    "max_position_loss_share": _param(0.01, "policy", "Wealth: at most 1% of liquid net worth at risk in one position"),
    "drawdown_stop": _param(0.30, "policy", "Wealth: pause adding when the sleeve is 30% below its high"),
    "panic_drawdown": _param(0.10, "policy", "Wealth circuit breaker: a sell-everything request after a fall above 10%"),
    "cool_off_hours": _param(48, "policy", "Wealth: suggested wait before acting on a panic sale"),
    "cool_off_move": _param(0.05, "policy", "Wealth: a move above 5% within the last hour"),
    "cool_off_window_minutes": _param(60, "policy", "Wealth: the window after a large move"),
    "late_night_hours": _param([23, 5], "policy", "Wealth: 23:00 to 05:00 local time counts as late at night"),
    "scam_monthly_return": _param(0.02, "policy", "Wealth: a promised return above 2% a month (about 27% a year) is flagged",
                                  note="For scale: Mexican 28-day CETES and US T-bills paid well under 1% a month in 2026."),
    "scam_monthly_return_high": _param(0.05, "policy", "Wealth: a promised return of 5% a month or more is high risk"),
    "scam_large_share": _param(0.20, "policy", "Wealth: a transfer to a new payee of 20% or more of liquid assets"),
    "scam_large_absolute": _param({"MXN": 50000, "USD": 3000}, "policy",
                                  "Wealth: fallback 'large transfer' when balances are unknown"),
    # Sourced figures.
    "esma_leverage_caps": _param({"major_fx": 30, "non_major_fx_gold_major_indices": 20,
                                  "commodities_non_major_indices": 10, "individual_equities_other": 5,
                                  "crypto": 2}, "verified",
                                 "ESMA, product intervention on CFDs and binary options (press release 27 March 2018)",
                                 ESMA_CFD_URL),
    "esma_margin_close_out": _param(0.50, "verified", "ESMA 2018: close-out when margin falls to 50% of the minimum",
                                    ESMA_CFD_URL),
    "esma_retail_loss_rate": _param([0.74, 0.89], "verified",
                                    "ESMA 2018: 74-89% of retail CFD accounts typically lose money", ESMA_CFD_URL),
    "reg_t_initial_margin": _param(0.50, "verified", "FINRA, Margin accounts (Regulation T initial margin)",
                                   FINRA_MARGIN_URL),
    "finra_maintenance_margin": _param(0.25, "verified", "FINRA Rule 4210 maintenance margin; firms may require more",
                                       FINRA_MARGIN_URL),
    "finra_options_approval": _param("Rule 2360(b)(16) account approval; ODD delivered at or before approval",
                                     "verified", "FINRA Rule 2360 (Options)", FINRA_2360_URL),
    "ipab_limit_udis": _param(400000, "verified", "IPAB, seguro de depósitos: 400 mil UDIs por persona y por banco",
                              IPAB_URL),
    "prosofipo_limit_udis": _param(25000, "verified", "CONDUSEF, Fondo de Protección SOFIPO: hasta 25 mil UDIs",
                                   CONDUSEF_SOFIPO_URL),
    "udi_value_mxn": _param(8.8188, "needs_verification",
                            "Implied by IPAB's 400,000-UDI limit reported as MXN 3,527,537.20 on 2026-09-15 (press)",
                            "https://www.banxico.org.mx",
                            note="Check the day's UDI value at Banxico before quoting pesos; pass udi_value to override."),
}

DEFAULT_POLICY = {
    "cap_share": PARAMETERS["speculation_cap_share"]["value"],
    "max_position_loss_share": PARAMETERS["max_position_loss_share"]["value"],
    "drawdown_stop": PARAMETERS["drawdown_stop"]["value"],
}
POLICY_KEYS = tuple(DEFAULT_POLICY)
VERDICTS = ("allow", "allow_with_warning", "decline_to_recommend")

GUARDRAIL_FACT_KEYS = ("client.profile", "income.", "spending.monthly", "cash.", "liability.", "investment.",
                       "goals", "reserve", "preference.", "constraint.", "policy.ips", "account.")
"""Facts the guardrail and protection tasks read; the service warns when one is stale."""


def _source(key: str) -> dict:
    row = PARAMETERS[key]
    return {**row["source"], "checked_on": row["checked_on"], "status": row["status"]}


# ------------------------------------------------------------------ small helpers


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _msg(code: str, en: str, es: str, **extra: Any) -> dict:
    return {"code": code, "en": en, "es": es, **extra}


def _money(value: float | None, currency: str | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:,.0f} {currency}" if currency else f"{value:,.0f}"


def _pct(value: float) -> str:
    text = f"{value * 100:.1f}".rstrip("0").rstrip(".")
    return text + "%"


def _country(situation: Mapping[str, Any] | None, explicit: Any = None) -> str | None:
    if isinstance(explicit, str) and explicit.upper() in ("MX", "US"):
        return explicit.upper()
    profile = (situation or {}).get("profile") or {}
    tax = profile.get("tax_residence") or []
    if tax:
        return tax[0]
    return ((profile.get("residence") or {}).get("country")) or None


# ------------------------------------------------------------------ education cards

INSTRUMENTS = {
    "option": "options", "options": "options", "call": "options", "put": "options",
    "margin": "leverage", "cfd": "leverage", "future": "leverage", "futures": "leverage", "perp": "leverage",
    "perpetual": "leverage", "leveraged_etf": "leverage", "short": "leverage", "leverage": "leverage",
    "crypto": "crypto", "cryptocurrency": "crypto", "stablecoin": "crypto", "token": "crypto",
    "fintech_yield": "fintech_yield", "sofipo": "fintech_yield", "staking": "crypto",
    "stock": "single_stock", "single_stock": "single_stock", "penny_stock": "single_stock",
}

_CARDS: dict[str, dict] = {
    "options": {
        "title": {"en": "Options: how they work and how you can lose", "es": "Opciones: cómo funcionan y cómo se pierde"},
        "mechanics": {
            "en": "An option is a contract giving the right (not the obligation) to buy (call) or sell (put) 100 shares "
                  "at a set price until a date. Its price decays as expiry nears; most of its value is time and "
                  "volatility, not the share price.",
            "es": "Una opción es un contrato que da el derecho (no la obligación) de comprar (call) o vender (put) "
                  "100 acciones a un precio fijo hasta una fecha. Su precio se desgasta al acercarse el vencimiento; "
                  "buena parte de su valor es tiempo y volatilidad, no el precio de la acción."},
        "max_loss": {
            "en": "Buying an option: you can lose the whole premium, and often do if the move is late or small. "
                  "Selling an uncovered call: the loss has no ceiling. Selling an uncovered put: up to the strike "
                  "times 100 per contract, less the premium.",
            "es": "Comprar una opción: puedes perder toda la prima, y es común si el movimiento llega tarde o es "
                  "pequeño. Vender un call descubierto: la pérdida no tiene límite. Vender un put descubierto: hasta "
                  "el precio de ejercicio por 100 por contrato, menos la prima."},
        "liquidation": {
            "en": "Short options sit in a margin account: a margin call can force the broker to close positions at "
                  "the worst moment, without asking first.",
            "es": "Las opciones vendidas viven en una cuenta de margen: una llamada de margen puede obligar al "
                  "intermediario a cerrar posiciones en el peor momento, sin consultarte."},
        "protection": {
            "en": "In the US a broker must approve an options account and deliver the Options Disclosure Document "
                  "(FINRA Rule 2360). No deposit insurance covers trading losses.",
            "es": "En EE.UU. el intermediario debe aprobar la cuenta de opciones y entregar el documento de riesgos "
                  "(FINRA Regla 2360). Ningún seguro de depósito cubre pérdidas por operar."},
        "sources": ["finra_options_approval"],
    },
    "leverage": {
        "title": {"en": "Leverage (margin, CFDs, futures, perps, leveraged ETFs)",
                  "es": "Apalancamiento (margen, CFD, futuros, perpetuos, ETF apalancados)"},
        "mechanics": {
            "en": "Leverage borrows to hold a bigger position than your money. At 5:1 a 20% fall wipes out the "
                  "whole deposit; at 30:1 a 3.3% move does. Leveraged ETFs reset daily, so over weeks they can lose "
                  "even when the index ends flat.",
            "es": "El apalancamiento pide prestado para tener una posición mayor que tu dinero. A 5:1 una caída de "
                  "20% borra todo el depósito; a 30:1 basta un movimiento de 3.3%. Los ETF apalancados se "
                  "reajustan a diario, así que en semanas pueden perder aunque el índice termine igual."},
        "max_loss": {
            "en": "At least the whole deposit; with margin or futures and no negative-balance protection, more "
                  "than you put in. A short position has no ceiling.",
            "es": "Como mínimo todo el depósito; con margen o futuros sin protección de saldo negativo, más de lo "
                  "que pusiste. Una posición en corto no tiene límite de pérdida."},
        "liquidation": {
            "en": "Brokers close positions when equity falls below maintenance (US: at least 25% under FINRA Rule "
                  "4210, often more; the EU closes CFDs at 50% of required margin). They may sell without notice.",
            "es": "El intermediario cierra posiciones cuando el capital cae bajo el mínimo (EE.UU.: al menos 25% "
                  "por la Regla FINRA 4210, a menudo más; en la UE los CFD se cierran al 50% del margen "
                  "requerido). Puede vender sin avisarte."},
        "protection": {
            "en": "For retail clients the EU caps leverage at 30:1 for major currencies down to 2:1 for crypto, "
                  "because 74-89% of retail CFD accounts lost money (ESMA, 2018). No deposit insurance covers "
                  "trading losses.",
            "es": "Para clientes minoristas la UE limita el apalancamiento de 30:1 en divisas principales a 2:1 en "
                  "cripto, porque 74-89% de las cuentas minoristas de CFD perdían dinero (ESMA, 2018). Ningún "
                  "seguro de depósito cubre pérdidas por operar."},
        "sources": ["esma_leverage_caps", "esma_margin_close_out", "esma_retail_loss_rate", "reg_t_initial_margin",
                    "finra_maintenance_margin"],
    },
    "crypto": {
        "title": {"en": "Crypto: price, custody and who protects it", "es": "Cripto: precio, custodia y quién la protege"},
        "mechanics": {
            "en": "A crypto asset has no cash flows; its price is what the next buyer pays. Falls of 50-80% have "
                  "happened repeatedly. On an exchange you hold a claim on the platform; in your own wallet, "
                  "a lost seed phrase means the coins are gone.",
            "es": "Un criptoactivo no genera flujos; su precio es lo que pague el siguiente comprador. Caídas de "
                  "50-80% han ocurrido varias veces. En una plataforma tienes un derecho frente a ella; en tu propia "
                  "cartera, perder la frase semilla es perder las monedas."},
        "max_loss": {
            "en": "The whole amount, and more if it is bought with leverage or perps (the EU caps crypto CFDs at 2:1).",
            "es": "Todo lo invertido, y más si se compra con apalancamiento o perpetuos (la UE limita los CFD de "
                  "cripto a 2:1)."},
        "liquidation": {
            "en": "Leveraged crypto positions are closed automatically on a fast move, often at night or on weekends.",
            "es": "Las posiciones apalancadas en cripto se cierran automáticamente con un movimiento rápido, a "
                  "menudo de noche o en fin de semana."},
        "protection": {
            "en": "Crypto balances and crypto 'yield' are not bank deposits: the IPAB does not cover them in Mexico, "
                  "and FDIC or SIPC do not cover the coins in the US. If the platform fails, you are a creditor.",
            "es": "Los saldos y 'rendimientos' en cripto no son depósitos bancarios: el IPAB no los cubre en "
                  "México, y en EE.UU. ni FDIC ni SIPC cubren las monedas. Si la plataforma quiebra, eres un "
                  "acreedor más."},
        "sources": ["esma_leverage_caps", "ipab_limit_udis"],
    },
    "fintech_yield": {
        "title": {"en": "Fintech and SOFIPO yield: who guarantees it", "es": "Rendimiento en fintech y SOFIPO: quién lo respalda"},
        "mechanics": {
            "en": "A higher advertised rate pays for more risk or a promotion that ends. Rates are variable and have "
                  "fallen before.",
            "es": "Una tasa anunciada más alta paga más riesgo o una promoción que termina. Las tasas son variables "
                  "y ya han bajado antes."},
        "max_loss": {
            "en": "Up to the uninsured part of the balance if the entity fails.",
            "es": "Hasta la parte no protegida del saldo si la entidad quiebra."},
        "liquidation": {
            "en": "Not applicable; the risk is the entity failing or freezing withdrawals.",
            "es": "No aplica; el riesgo es que la entidad quiebre o congele retiros."},
        "protection": {
            "en": "In Mexico the IPAB covers deposits at licensed banks up to 400,000 UDIs per person per bank. A "
                  "SOFIPO is covered by its own fund up to 25,000 UDIs. An electronic-payment fund (IFPE) wallet, "
                  "investment funds, casas de bolsa and crypto are outside the IPAB.",
            "es": "En México el IPAB cubre depósitos en bancos hasta 400 mil UDIs por persona y por banco. Una "
                  "SOFIPO tiene su propio fondo de protección hasta 25 mil UDIs. Un saldo en una IFPE (fondo de "
                  "pago electrónico), los fondos de inversión, las casas de bolsa y las cripto quedan fuera del IPAB."},
        "sources": ["ipab_limit_udis", "prosofipo_limit_udis", "udi_value_mxn"],
    },
    "single_stock": {
        "title": {"en": "A single stock as play money", "es": "Una sola acción como dinero de juego"},
        "mechanics": {
            "en": "One company carries its own risk on top of the market's; single stocks fall 50% or more far more "
                  "often than broad funds.",
            "es": "Una sola empresa suma su propio riesgo al del mercado; una acción individual cae 50% o más con "
                  "mucha más frecuencia que un fondo amplio."},
        "max_loss": {"en": "The whole amount.", "es": "Todo lo invertido."},
        "liquidation": {"en": "None without margin.", "es": "Ninguna si no usas margen."},
        "protection": {
            "en": "Securities at a broker are not bank deposits; broker protection (SIPC in the US) covers a broker's "
                  "failure, never a fall in price.",
            "es": "Los valores en una casa de bolsa no son depósitos bancarios; la protección del intermediario (SIPC "
                  "en EE.UU.) cubre su quiebra, nunca una caída de precio."},
        "sources": [],
    },
}


def education_card(instrument: str, *, udi_value: float | None = None) -> dict:
    """The mechanics-and-risks card for an instrument; never a trade call."""
    kind = INSTRUMENTS.get(str(instrument or "").lower())
    if kind is None:
        raise ValueError(f"instrument must be one of {', '.join(sorted(INSTRUMENTS))}")
    card = _CARDS[kind]
    out = {"kind": kind, **{k: dict(card[k]) for k in ("title", "mechanics", "max_loss", "liquidation", "protection")},
           "sources": [_source(key) for key in card["sources"]], "trade_call": None}
    if kind in ("fintech_yield", "crypto"):
        udi = _num(udi_value) or PARAMETERS["udi_value_mxn"]["value"]
        status = "supplied" if _num(udi_value) else PARAMETERS["udi_value_mxn"]["status"]
        out["deposit_insurance_mx"] = {
            "ipab_udis": PARAMETERS["ipab_limit_udis"]["value"], "prosofipo_udis": PARAMETERS["prosofipo_limit_udis"]["value"],
            "udi_value_mxn": udi, "udi_value_status": status,
            "ipab_mxn_approx": round(PARAMETERS["ipab_limit_udis"]["value"] * udi, -3),
            "prosofipo_mxn_approx": round(PARAMETERS["prosofipo_limit_udis"]["value"] * udi, -3),
            "not_covered": ["crypto", "IFPE wallet balances", "fondos de inversión", "casas de bolsa", "AFORE",
                            "insurance savings"],
        }
    return out


# ------------------------------------------------------------------ cool-off


def _parse_dt(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date-time")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date-time") from exc


def cool_off(now: Any = None, tz: str | None = None, last_move: Mapping[str, Any] | None = None) -> dict:
    """A gentle flag for a trade intent within the window after a large move, or late at night.

    ``now``: ISO date-time of the intent (with offset or treated as UTC).
    ``tz``: IANA time zone for "late at night"; unknown means that part is not checked.
    ``last_move``: ``{size: signed share such as -0.06, at: ISO date-time}`` of the latest large move.
    """
    flags: list[dict] = []
    not_checked: list[str] = []
    if now is None:
        return {"flag": False, "flags": [], "not_checked": ["now (time of the intent)"]}
    moment = _parse_dt(now, "now")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if tz:
        try:
            from zoneinfo import ZoneInfo
            local = moment.astimezone(ZoneInfo(tz))
        except Exception as exc:  # zoneinfo raises several types for a bad key
            raise ValueError(f"timezone {tz!r} is not a known IANA zone") from exc
        start, end = PARAMETERS["late_night_hours"]["value"]
        if local.hour >= start or local.hour < end:
            flags.append(_msg("late_night",
                              f"It is {local:%H:%M} where you are. Decisions made late at night are worth one more "
                              "look in the morning.",
                              f"Son las {local:%H:%M} donde estás. Una decisión tomada de noche merece otra mirada "
                              "por la mañana.", local_time=local.isoformat()))
    else:
        not_checked.append("timezone (late-night check)")
    if isinstance(last_move, Mapping) and last_move.get("size") is not None:
        size = _num(last_move.get("size"))
        if size is None:
            raise ValueError("last_move.size must be a number such as -0.06")
        at = _parse_dt(last_move.get("at"), "last_move.at") if last_move.get("at") else None
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        window = timedelta(minutes=PARAMETERS["cool_off_window_minutes"]["value"])
        threshold = PARAMETERS["cool_off_move"]["value"]
        if at is None:
            not_checked.append("last_move.at")
        elif abs(size) > threshold and timedelta(0) <= moment - at <= window:
            minutes = int((moment - at).total_seconds() // 60)
            direction = ("fell", "cayó") if size < 0 else ("rose", "subió")
            flags.append(_msg("fresh_move",
                              f"The market {direction[0]} {_pct(abs(size))} {minutes} minutes ago. Waiting an hour "
                              "usually costs little and lets the first reaction pass.",
                              f"El mercado {direction[1]} {_pct(abs(size))} hace {minutes} minutos. Esperar una hora "
                              "suele costar poco y deja pasar la primera reacción.", minutes_since=minutes))
    else:
        not_checked.append("last_move")
    return {"flag": bool(flags), "flags": flags, "not_checked": not_checked,
            "blocks_choice": False,
            "rule": f"A move above {_pct(PARAMETERS['cool_off_move']['value'])} within "
                    f"{PARAMETERS['cool_off_window_minutes']['value']} minutes, or 23:00-05:00 local time."}


# ------------------------------------------------------------------ speculation sleeve


def _policy(ips: Mapping[str, Any] | None, override: Mapping[str, Any] | None) -> tuple[dict, list[str]]:
    policy = dict(DEFAULT_POLICY)
    basis = []
    constraint = ((ips or {}).get("constraints") or {}).get("speculation") if isinstance(ips, Mapping) else None
    for label, source in (("ips", constraint), ("person", override)):
        if not isinstance(source, Mapping):
            continue
        for key in POLICY_KEYS:
            if source.get(key) is None:
                continue
            value = _num(source[key])
            if value is None or not 0 <= value <= 1:
                raise ValueError(f"speculation policy {key} must be a share between 0 and 1")
            if key == "cap_share" and value > PARAMETERS["speculation_cap_ceiling"]["value"]:
                raise ValueError("cap_share may be at most 0.10 (10% of liquid net worth)")
            policy[key] = value
            basis.append(f"{key} set by {label}")
    return policy, basis


def _worst_loss(proposal: Mapping[str, Any], kind: str | None) -> tuple[float | None, bool, str]:
    """(worst-case loss, unbounded?, how it was worked out) for a buy."""
    amount = _num(proposal.get("amount"))
    instrument = str(proposal.get("instrument") or "").lower()
    side = str(proposal.get("side") or "long").lower()
    if instrument == "short" or (kind == "options" and side == "short" and proposal.get("covered") is not True
                                 and str(proposal.get("option_type") or "call").lower() == "call"):
        return None, True, "a short position or an uncovered call has no loss ceiling"
    if kind == "options" and side == "short":
        strike = _num(proposal.get("strike"))
        contracts = _num(proposal.get("contracts"))
        if proposal.get("covered") is True:
            return amount, False, "a covered position: the loss is on the shares already held"
        if strike is not None and contracts is not None:
            return strike * 100 * contracts - (amount or 0), False, "an uncovered put: strike x 100 x contracts less premium"
        return None, False, "an uncovered put: needs strike and contracts"
    leverage = _num(proposal.get("leverage"))
    if leverage is None and instrument in ("margin", "cfd", "future", "futures", "perp", "perpetual"):
        return None, False, "leverage multiple unknown"
    if amount is None:
        return None, False, "amount unknown"
    if leverage and leverage > 1 and not proposal.get("negative_balance_protection"):
        return amount * leverage, False, f"{leverage:g}x exposure without negative-balance protection can lose more than the deposit"
    return amount, False, "the amount paid can go to zero"


def speculation_check(proposal: Mapping[str, Any], situation: Mapping[str, Any] | None,
                      ips: Mapping[str, Any] | None = None, policy: Mapping[str, Any] | None = None,
                      *, context: Mapping[str, Any] | None = None) -> dict:
    """Size a speculative idea against the play-money policy.

    ``proposal``: ``{action: buy|sell|explain, instrument, amount?, currency?, symbol?, leverage?,
    side?: long|short, option_type?: call|put, covered?, strike?, contracts?, negative_balance_protection?,
    sleeve?: {value, peak}}``.  ``sleeve.value`` is what is already in play money and ``peak`` its high.
    ``situation``: :func:`wealth.situation.build` output.  ``ips``: accepted IPS (leverage and an optional
    ``constraints.speculation``).  ``policy``: the person's ``{cap_share, max_position_loss_share, drawdown_stop}``.
    ``context``: ``{now, timezone, last_move}`` for the cool-off flag.

    Returns ``{verdict, reasons, limits, education, cool_off, the_person_decides}``.  ``explain`` and
    ``sell`` never decline: explaining mechanics and reducing risk are always fine.
    """
    if not isinstance(proposal, Mapping):
        raise ValueError("proposal must be an object")
    action = proposal.get("action") or "buy"
    if action not in ("buy", "sell", "explain"):
        raise ValueError("proposal.action must be buy, sell or explain")
    instrument = str(proposal.get("instrument") or "").lower()
    if not instrument:
        raise ValueError("proposal.instrument is required (options, crypto, margin, cfd, leveraged_etf, stock, ...)")
    kind = INSTRUMENTS.get(instrument)
    if kind is None:
        raise ValueError(f"proposal.instrument must be one of {', '.join(sorted(INSTRUMENTS))}")
    sit = situation or {}
    currency = proposal.get("currency") or sit.get("currency")
    rules, missing, reasons = [], [], []
    education = education_card(instrument, udi_value=(context or {}).get("udi_value"))
    tz = (context or {}).get("timezone") or ((sit.get("profile") or {}).get("timezone"))
    flag = cool_off((context or {}).get("now"), tz, (context or {}).get("last_move")) if context else None
    limits_out: dict[str, Any] = {"currency": currency}
    pol, basis = _policy(ips, policy)

    def rule(code: str, status: str, en: str, es: str, **extra: Any) -> None:
        rules.append({"rule": code, "status": status, **extra})
        if status != "pass":
            reasons.append(_msg(code, en, es))

    if action == "explain":
        return {"verdict": "allow", "reasons": [], "rules": [], "limits": limits_out, "education": education,
                "cool_off": flag, "the_person_decides": True, "trade_call": None, "missing": [],
                "policy": {**pol, "basis": basis}}
    if action == "sell":
        rule("reduce_risk", "pass", "", "")
        return {"verdict": "allow", "reasons": [_msg("reduce_risk", "Selling reduces play-money risk; nothing in the "
                                                     "policy stands against it.",
                                                     "Vender reduce el riesgo del dinero de juego; la política no se "
                                                     "opone.")],
                "rules": rules, "limits": limits_out, "education": education, "cool_off": flag,
                "the_person_decides": True, "trade_call": None, "missing": [], "policy": {**pol, "basis": basis}}

    # -- zero cap while the reserve is short or expensive debt is open
    reserve = sit.get("reserve") or {}
    gap = _num(reserve.get("gap"))
    cap_share = pol["cap_share"]
    if gap is None:
        missing.append("reserve (amount and target)")
        rule("reserve", "warn", "I can't tell yet whether your emergency reserve is full; play money comes after it.",
             "Aún no sé si tu fondo de emergencia está completo; el dinero de juego va después de él.")
    elif gap > 0:
        cap_share = 0.0
        rule("reserve", "decline",
             f"Your reserve is {_money(gap, reserve.get('currency'))} short of its target, so the play-money cap is 0% "
             "until it is full.",
             f"A tu fondo de emergencia le faltan {_money(gap, reserve.get('currency'))} para su meta; mientras tanto "
             "el tope de dinero de juego es 0%.", gap=gap)
    else:
        rule("reserve", "pass", "", "")
    threshold = PARAMETERS["speculation_debt_rate"]["value"]
    costly, unknown_rate = [], []
    for debt in sit.get("liabilities") or []:
        if (_num(debt.get("balance")) or 0) <= 0:
            continue
        rate = _num(debt.get("annual_rate"))
        if rate is None:
            unknown_rate.append(debt.get("name") or debt.get("id"))
        elif rate > threshold:
            costly.append((debt.get("name") or debt.get("id"), rate))
    if costly:
        cap_share = 0.0
        names = ", ".join(f"{n} at {_pct(r)}" for n, r in costly)
        names_es = ", ".join(f"{n} al {_pct(r)}" for n, r in costly)
        rule("costly_debt", "decline",
             f"Paying down {names} earns a sure {_pct(max(r for _, r in costly))}; the play-money cap is 0% until "
             f"no debt costs more than {_pct(threshold)}.",
             f"Pagar {names_es} te da un {_pct(max(r for _, r in costly))} seguro; el tope de dinero de juego es 0% "
             f"hasta que ninguna deuda cueste más de {_pct(threshold)}.")
    elif unknown_rate:
        missing += [f"liability rate: {n}" for n in unknown_rate]
        rule("costly_debt", "warn", f"I don't know the rate on {', '.join(map(str, unknown_rate))}; above "
             f"{_pct(threshold)} it should come first.",
             f"No sé la tasa de {', '.join(map(str, unknown_rate))}; si pasa de {_pct(threshold)} va primero.")
    else:
        rule("costly_debt", "pass", "", "")

    # -- leverage against the IPS
    levered = kind == "leverage" or (_num(proposal.get("leverage")) or 1) > 1 or (
        kind == "options" and str(proposal.get("side") or "long").lower() == "short")
    allowed = ((ips or {}).get("constraints") or {}).get("leverage", {}).get("allowed") if isinstance(ips, Mapping) else None
    if levered and ips is not None and not allowed:
        rule("ips_leverage", "decline", "Your investment policy rules out leverage, margin and short selling.",
             "Tu política de inversión excluye apalancamiento, margen y ventas en corto.")

    # -- the cap on liquid net worth
    liquid = _num((sit.get("net_worth") or {}).get("liquid"))
    amount = _num(proposal.get("amount"))
    sleeve = proposal.get("sleeve") if isinstance(proposal.get("sleeve"), Mapping) else {}
    held = _num(sleeve.get("value"))
    if amount is None:
        missing.append("proposal.amount")
    if liquid is None:
        missing.append("liquid net worth (cash and investments)")
        rule("cap", "decline" if cap_share > 0 else "pass",
             "I can't size play money until I know your liquid savings and investments.",
             "No puedo dimensionar dinero de juego hasta conocer tus ahorros e inversiones líquidas.")
    else:
        cap = cap_share * liquid
        limits_out.update(cap=round(cap, 2), cap_share=cap_share, liquid_net_worth=liquid)
        if held is None:
            missing.append("proposal.sleeve.value (play money already held)")
        after = (held or 0) + (amount or 0)
        limits_out["sleeve_after"] = round(after, 2) if held is not None and amount is not None else None
        if cap_share == 0:
            pass  # the reason is already given by the reserve or debt rule
        elif amount is not None and after > cap + 1e-9:
            rule("cap", "decline",
                 f"This would put {_money(after, currency)} in play money, above the {_pct(cap_share)} cap of "
                 f"{_money(cap, currency)}.",
                 f"Esto dejaría {_money(after, currency)} en dinero de juego, arriba del tope de {_pct(cap_share)} "
                 f"({_money(cap, currency)}).", after=after, cap=cap)
        elif held is None:
            rule("cap", "warn",
                 f"The cap is {_money(cap, currency)} ({_pct(cap_share)}); I don't know what is already in play money, "
                 "so count it against the cap.",
                 f"El tope es {_money(cap, currency)} ({_pct(cap_share)}); no sé cuánto tienes ya en dinero de juego, "
                 "así que cuéntalo contra el tope.")
        else:
            rule("cap", "pass", "", "", after=after, cap=cap)

    # -- single-position loss
    worst, unbounded, how = _worst_loss(proposal, kind)
    limits_out["worst_case_loss"] = None if unbounded else (round(worst, 2) if worst is not None else None)
    limits_out["worst_case_basis"] = how
    if unbounded:
        rule("position_loss", "decline",
             "This position has no ceiling on what it can lose, so it can't fit a play-money budget.",
             "Esta posición no tiene techo de pérdida, así que no cabe en un presupuesto de dinero de juego.")
    elif liquid is not None and worst is not None:
        max_loss = pol["max_position_loss_share"] * liquid
        limits_out["max_position_loss"] = round(max_loss, 2)
        if worst > max_loss + 1e-9:
            rule("position_loss", "decline" if cap_share > 0 else "pass",
                 f"The worst case here is {_money(worst, currency)} ({how}), above the {_pct(pol['max_position_loss_share'])} "
                 f"single-position limit of {_money(max_loss, currency)}.",
                 f"En el peor caso pierdes {_money(worst, currency)}, arriba del límite por posición de "
                 f"{_pct(pol['max_position_loss_share'])} ({_money(max_loss, currency)}).")
        else:
            rule("position_loss", "pass", "", "")
    elif worst is None and not unbounded:
        rule("position_loss", "warn", f"I can't work out the worst case yet ({how}).",
             f"Aún no puedo calcular el peor caso ({how}).")
        missing.append(f"worst case: {how}")

    # -- drawdown stop
    value, peak = held, _num(sleeve.get("peak"))
    if value is not None and peak:
        drawdown = max(0.0, 1 - value / peak)
        limits_out["sleeve_drawdown"] = round(drawdown, 4)
        if drawdown >= pol["drawdown_stop"]:
            rule("drawdown_stop", "decline",
                 f"Play money is {_pct(drawdown)} below its high; the plan pauses adding at {_pct(pol['drawdown_stop'])}. "
                 "A written look back at what happened comes first.",
                 f"El dinero de juego está {_pct(drawdown)} abajo de su máximo; el plan pausa nuevas compras al "
                 f"{_pct(pol['drawdown_stop'])}. Primero conviene revisar por escrito qué pasó.", drawdown=drawdown)
        else:
            rule("drawdown_stop", "pass", "", "")
    else:
        rules.append({"rule": "drawdown_stop", "status": "not_checked", "needs": "sleeve.value and sleeve.peak"})

    if flag and flag["flag"]:
        reasons += flag["flags"]
    statuses = {r["status"] for r in rules}
    if "decline" in statuses:
        verdict = "decline_to_recommend"
    elif "warn" in statuses or (flag and flag["flag"]):
        verdict = "allow_with_warning"
    else:
        verdict = "allow"
    return {"verdict": verdict, "reasons": [r for r in reasons if r.get("en")], "rules": rules, "limits": limits_out,
            "education": education, "cool_off": flag, "missing": missing, "trade_call": None,
            "the_person_decides": True, "policy": {**pol, "basis": basis},
            "note": "decline_to_recommend means Wealth will not recommend it; the mechanics are still explained and "
                    "the choice stays with the person."}


# ------------------------------------------------------------------ panic circuit breaker

# S&P 500 price drawdowns (closing basis).  Secondary sources; recovery = first close above the prior peak.
HISTORICAL_DRAWDOWNS: tuple[dict, ...] = (
    {"name": "2018 Q4 selloff", "peak": "2018-09-20", "trough": "2018-12-24", "decline": -0.198,
     "recovered": "2019-04-23", "stress_window": ("2018 Q4 selloff", "2018-10-01", "2018-12-24"),
     "status": "needs_verification"},
    {"name": "2020 COVID crash", "peak": "2020-02-19", "trough": "2020-03-23", "decline": -0.34,
     "recovered": "2020-08-18", "stress_window": ("2020 COVID crash", "2020-02-19", "2020-03-23"),
     "status": "needs_verification"},
    {"name": "2022 hiking cycle", "peak": "2022-01-03", "trough": "2022-10-12", "decline": -0.254,
     "recovered": "2024-01-19", "stress_window": ("2022 hiking cycle", "2022-01-01", "2022-10-12"),
     "status": "needs_verification"},
    {"name": "2007-09 financial crisis", "peak": "2007-10-09", "trough": "2009-03-09", "decline": -0.568,
     "recovered": "2013-03-28", "stress_window": None, "status": "needs_verification"},
    {"name": "2000-02 dot-com bust", "peak": "2000-03-24", "trough": "2002-10-09", "decline": -0.49,
     "recovered": "2007-05-30", "stress_window": None, "status": "needs_verification"},
)
_HISTORY_SOURCES = [
    {"title": "Closing milestones of the S&P 500 (Wikipedia; price index, closing basis)", "url": SP500_MILESTONES_URL,
     "checked_on": CHECKED_ON, "status": "needs_verification"},
    {"title": "Hartford Funds, 10 Things You Should Know About Bear Markets", "url": HARTFORD_BEAR_URL,
     "checked_on": CHECKED_ON, "status": "needs_verification"},
]
_TAX_DEFERRED = {"ira", "traditional_ira", "roth_ira", "roth", "401k", "403b", "hsa", "afore", "ppr", "retirement", "pension"}


def _months_between(a: str, b: str) -> int:
    x, y = date.fromisoformat(a), date.fromisoformat(b)
    return (y.year - x.year) * 12 + y.month - x.month


def _history(drawdown: float, supplied: Any) -> dict:
    rows = []
    for row in HISTORICAL_DRAWDOWNS:
        rows.append({"name": row["name"], "peak": row["peak"], "trough": row["trough"], "decline": row["decline"],
                     "recovered": row["recovered"],
                     "months_peak_to_trough": _months_between(row["peak"], row["trough"]),
                     "months_trough_to_recovery": _months_between(row["trough"], row["recovered"]),
                     "at_least_as_deep": abs(row["decline"]) >= drawdown, "status": row["status"]})
    similar = [r for r in rows if r["at_least_as_deep"]]
    windows = [{"name": w[0], "start": w[1], "end": w[2]} for w in
               (r["stress_window"] for r in HISTORICAL_DRAWDOWNS) if w]
    personal = None
    if isinstance(supplied, list):
        personal = [{"name": s.get("name"), "portfolio_return": _num(s.get("portfolio_return"))}
                    for s in supplied if isinstance(s, Mapping)]
    return {
        "index": "S&P 500 price index", "episodes": rows, "similar_or_deeper": [r["name"] for r in similar],
        "every_episode_recovered": all(r["recovered"] for r in rows),
        "longest_recovery_months": max(r["months_trough_to_recovery"] for r in rows),
        "shortest_recovery_months": min(r["months_trough_to_recovery"] for r in rows),
        "your_portfolio_in_those_windows": personal,
        "stress_request": {"task": "stress", "inputs": {"scenarios": windows}},
        "caveat": {"en": "Past recoveries are history, not a promise; a single stock or a narrow fund may never recover.",
                   "es": "Las recuperaciones pasadas son historia, no una promesa; una sola acción o un fondo "
                         "concentrado puede no recuperarse nunca."},
        "sources": _HISTORY_SOURCES,
    }


def _tax_cost(holdings: Any, country: str | None, as_of: date) -> dict:
    """Gains realised by selling everything, with a tax range by jurisdiction; unknown basis stays unknown."""
    if not isinstance(holdings, list) or not holdings:
        return {"status": "unknown", "missing": ["holdings [{symbol, value, cost_basis, account_type?, acquired_on?}]"],
                "hint": "For an exact figure run tax (US) or mx_holdings (Mexico) on the lots."}
    gains = {"short": 0.0, "long": 0.0, "mx_listed": 0.0, "mx_other": 0.0, "unknown_term": 0.0}
    losses, deferred_value, unknown = 0.0, 0.0, []
    for index, holding in enumerate(holdings):
        if not isinstance(holding, Mapping):
            raise ValueError(f"holdings[{index}] must be an object")
        value = _num(holding.get("value"))
        symbol = holding.get("symbol") or f"holdings[{index}]"
        if value is None:
            unknown.append(f"{symbol}: value")
            continue
        if str(holding.get("account_type") or "").lower() in _TAX_DEFERRED:
            deferred_value += value
            continue
        basis = _num(holding.get("cost_basis"))
        if basis is None:
            unknown.append(f"{symbol}: cost_basis")
            continue
        gain = value - basis
        if gain <= 0:
            losses += -gain
            continue
        if country == "MX":
            gains["mx_listed" if holding.get("listed_in_mx", True) else "mx_other"] += gain
        else:
            acquired = holding.get("acquired_on")
            if acquired:
                held_days = (as_of - date.fromisoformat(str(acquired)[:10])).days
                gains["long" if held_days > 365 else "short"] += gain
            else:
                gains["unknown_term"] += gain
    if country == "MX":
        low = 0.10 * gains["mx_listed"] + 0.0 * gains["mx_other"]
        high = 0.10 * gains["mx_listed"] + 0.35 * gains["mx_other"]
        basis = ("Mexico: 10% on net gains from BMV/SIC-listed shares and ETFs (LISR Art. 129); other foreign "
                 "securities at the marginal rate, up to 35%. Figures ignore inflation adjustment and loss offsets.")
    elif country == "US":
        low = 0.0 * gains["long"] + 0.10 * gains["short"] + 0.0 * gains["unknown_term"]
        high = 0.238 * gains["long"] + 0.408 * gains["short"] + 0.408 * gains["unknown_term"]
        basis = ("US federal: long-term gains 0-20% plus 3.8% NIIT; short-term at ordinary rates 10-37% plus 3.8%. "
                 "State tax not included.")
    else:
        low = high = None
        basis = "Tax residence unknown, so the rate is unknown; the gains are shown."
    total_gain = sum(gains.values())
    return {"status": "partial" if unknown else "ready", "realized_gain": round(total_gain, 2),
            "gains": {k: round(v, 2) for k, v in gains.items() if v}, "realized_loss": round(losses, 2),
            "tax_range": None if low is None else [round(low, 2), round(high, 2)],
            "inside_tax_deferred_accounts": round(deferred_value, 2),
            "unknown": unknown, "basis": basis,
            "note": "Selling inside a retirement account (IRA, 401k, AFORE, PPR) has no tax at the sale itself."}


def panic_check(request: Mapping[str, Any], situation: Mapping[str, Any] | None, *,
                context: Mapping[str, Any] | None = None, as_of: date | None = None) -> dict:
    """Data for a calm answer to "sell everything" after a fall; it never blocks the choice.

    ``request``: ``{action: sell_all|sell, share?: fraction of holdings, drawdown: fall from the peak as a share
    (0.14), portfolio_value?, holdings?: [{symbol, value, cost_basis, account_type?, acquired_on?, listed_in_mx?}],
    history?: stress-task scenario rows, jurisdiction?}``.
    """
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    sit = situation or {}
    today = as_of or date.fromisoformat(sit.get("as_of") or datetime.now(timezone.utc).date().isoformat())
    action = request.get("action") or "sell_all"
    if action not in ("sell_all", "sell"):
        raise ValueError("request.action must be sell_all or sell")
    share = 1.0 if action == "sell_all" else _num(request.get("share"))
    drawdown = _num(request.get("drawdown"))
    missing = []
    if drawdown is None:
        missing.append("drawdown (fall from the peak, e.g. 0.14)")
    elif not 0 <= abs(drawdown) < 1:
        raise ValueError("drawdown must be a share between 0 and 1, such as 0.14")
    else:
        drawdown = abs(drawdown)
    if share is None:
        missing.append("share (fraction of holdings to sell)")
    threshold = PARAMETERS["panic_drawdown"]["value"]
    triggered = bool(drawdown is not None and drawdown > threshold and share is not None and share >= 0.5)
    country = _country(sit, request.get("jurisdiction"))
    currency = sit.get("currency")

    # what is actually at risk for the goals
    goals = []
    for goal in sit.get("goals") or []:
        if goal.get("status") != "active":
            continue
        months = goal.get("months_left")
        horizon = None if months is None else ("under 3 years" if months < 36 else "3-10 years" if months < 120
                                               else "10+ years")
        goals.append({"name": goal.get("name"), "months_left": months, "horizon": horizon,
                      "exposed": None if months is None else months < 36,
                      "why": None if months is None else (
                          "due soon: this money should not depend on markets recovering" if months < 36 else
                          "time to recover: the fall matters only if you sell")})
    reserve = sit.get("reserve") or {}
    portfolio = _num(request.get("portfolio_value"))
    if portfolio is None:
        holdings_total = _num((sit.get("holdings") or {}).get("total"))
        portfolio = holdings_total
    paper_loss = round(portfolio * drawdown / (1 - drawdown), 2) if portfolio is not None and drawdown is not None else None
    at_risk = {
        "currency": currency, "portfolio_value": portfolio, "fall_from_peak": drawdown, "paper_loss": paper_loss,
        "reserve_months": reserve.get("months"), "reserve_untouched_by_markets": reserve.get("amount") is not None,
        "goals": goals, "near_goals": [g["name"] for g in goals if g["exposed"]],
        "paper_vs_realised": {"en": "The fall is a paper loss until you sell; selling makes it permanent.",
                              "es": "La caída es una pérdida en papel hasta que vendes; vender la vuelve definitiva."},
    }
    precommitment = (((sit.get("profile") or {}).get("risk")) or {}).get("drop_reaction")
    hours = PARAMETERS["cool_off_hours"]["value"]
    cool = {
        "suggested_hours": hours,
        "en": f"Wait {hours} hours before selling, and if you still want out, sell in two or three steps.",
        "es": f"Espera {hours} horas antes de vender y, si sigues queriendo salir, vende en dos o tres partes.",
        "precommitment": None if not precommitment else {
            "drop_reaction": precommitment,
            "en": f"When we set up your plan you said that after a 20% fall you would {precommitment.replace('_', ' ')}.",
            "es": ("Cuando armamos tu plan dijiste que ante una caída de 20% "
                   + {"hold": "mantendrías", "sell": "venderías", "buy_more": "comprarías más"}.get(precommitment, precommitment)
                   + ".")},
    }
    flag = cool_off((context or {}).get("now"), (context or {}).get("timezone") or (sit.get("profile") or {}).get("timezone"),
                    (context or {}).get("last_move")) if context else None
    result = {
        "triggered": triggered, "threshold": threshold, "at_risk": at_risk,
        "history": _history(drawdown or threshold, request.get("history")),
        "tax_cost": _tax_cost(request.get("holdings"), country, today), "cool_off": cool, "cool_off_flag": flag,
        "jurisdiction": country, "blocks_choice": False, "the_person_decides": True, "trade_call": None,
        "tone": {"en": "Answer the person first: name the worry, then the few numbers that matter.",
                 "es": "Primero atiende a la persona: nombra la preocupación y luego las pocas cifras que importan."},
    }
    if not triggered:
        result["why_not_triggered"] = (
            f"The circuit breaker is for selling half or more after a fall above {_pct(threshold)}."
            if drawdown is not None and share is not None else "Not enough to tell; see missing.")
    return {"result": result, "missing": missing}


# ------------------------------------------------------------------ scam screen

_FLAGS: tuple[tuple[str, str, str, str, str], ...] = (
    # code, severity, pattern, en reason, es reason
    ("guaranteed_return", "high",
     r"guarantee(d)?\s+(return|profit|income|gains?)|risk[- ]free|no risk|zero risk|garantizad[oa]s?|sin riesgo|"
     r"rendimientos? asegurados?|ganancias? seguras?",
     "It promises guaranteed or risk-free returns. No real investment can.",
     "Promete rendimientos garantizados o sin riesgo. Ninguna inversión real puede hacerlo."),
    ("credentials", "high",
     r"(verification|security|one[- ]time|otp|sms)\s+code|passcode|password|contraseñ?a|\bnip\b|\bpin\b|\bcvv\b|"
     r"código (de )?(verificación|seguridad|acceso|confirmación)|(el|tu|ese) código que (te )?(llegó|enviamos|recibiste)|"
     r"(dame|compárteme|compárteme|envíame|pásame) (el|tu) código|security token|token (de seguridad|bancario)|"
     r"e\.?firma|seed phrase|frase semilla|recovery phrase|clave (de acceso|dinámica|ciec)",
     "It asks for a code, password or key. Banks, CONDUSEF, the SAT and the IRS never ask for these.",
     "Pide un código, contraseña o clave. Ni los bancos, ni la CONDUSEF, ni el SAT los piden."),
    ("safe_account", "high",
     r"safe account|secure account|protect your (money|funds)|cuenta segura|proteger tu dinero|resguardar tu dinero",
     "It asks you to move money to a 'safe account'. Real institutions never do this.",
     "Te pide mover dinero a una 'cuenta segura'. Ninguna institución real lo hace."),
    ("pay_to_withdraw", "high",
     r"(fee|tax|deposit)\s+(to|before you can)\s+(withdraw|release|unlock)|withdrawal fee|unlock (your )?(funds|account)|"
     r"(comisión|impuesto|depósito) (para|antes de) (retirar|liberar)|liberar (tus|los) fondos",
     "It asks you to pay before you can withdraw. This is how fake trading platforms keep money.",
     "Te piden pagar para poder retirar. Así retienen el dinero las plataformas falsas."),
    ("advance_fee_loan", "high",
     r"(anticipo|pago por adelantado|depósito previo|fianza|gastos de (apertura|gestión)).{0,40}(crédito|préstamo)|"
     r"(crédito|préstamo).{0,40}(anticipo|pago por adelantado|depósito previo|fianza)|advance fee|upfront fee",
     "It asks for money up front to release a loan. CONDUSEF warns this is a common fraud.",
     "Pide dinero por adelantado para liberar un crédito. La CONDUSEF advierte que es un fraude común."),
    ("urgency", "medium",
     r"urgent|immediately|right now|act now|today only|last chance|expires? (today|in \d+)|within \d+ hours|"
     r"urgente|inmediat[oa]|solo hoy|sólo hoy|última oportunidad|hoy mismo|en las próximas \d+ horas|"
     r"será (bloquead|suspendid|cancelad)|will be (blocked|suspended|closed)",
     "It pushes you to act fast. Pressure is meant to stop you checking.",
     "Te presiona para actuar rápido. La prisa busca que no verifiques."),
    ("hard_to_trace_payment", "medium",
     r"gift ?cards?|tarjetas? de regalo|wire (transfer|the money)|western union|moneygram|bitcoin atm|"
     r"cajero de bitcoin|pay (only )?(in|with) (crypto|bitcoin|usdt)|paga(r)? (solo )?(en|con) (cripto|bitcoin|usdt)|"
     r"depósito en oxxo|deposita en oxxo",
     "It asks for payment by gift card, wire, crypto or cash deposit, which is hard to trace or reverse.",
     "Pide pago con tarjetas de regalo, transferencia, cripto o depósito en efectivo, difícil de rastrear o revertir."),
    ("impersonation", "medium",
     r"\b(CONDUSEF|Condusef|SAT|IMSS|IRS|SSA|FTC|FBI|CNBV|Banxico|INFONAVIT|Infonavit)\b|"
     r"(?i:social security administration|servicio de administración tributaria|"
     r"(bank|banco).{0,30}(security|fraud|seguridad|fraudes) (department|departamento|área)|"
     r"(área|departamento) de (seguridad|fraudes) de(l| tu) banco)",
     "It claims to come from a government agency or a bank's security team. They do not ask for money or data "
     "this way.",
     "Dice venir de una autoridad o del área de seguridad de un banco. No piden dinero ni datos por esta vía."),
    ("pig_butchering", "medium",
     r"wrong number|número equivocado|trading (mentor|teacher|group|signals?)|investment (mentor|coach)|"
     r"(mentor|maestr[oa]|profesor[a]?) de (trading|inversión)|grupo de (whatsapp|telegram)|"
     r"(whatsapp|telegram) (group|grupo)|my uncle|mi tío.{0,40}(plataforma|trading)|usdt|"
     r"(platform|plataforma).{0,40}(profit|ganancia)",
     "It matches a 'pig butchering' script: a friendly contact who steers you to a crypto or trading platform.",
     "Coincide con el guion de 'pig butchering': un contacto amable que te lleva a una plataforma de cripto o trading."),
    ("recovery_scam", "medium",
     r"recover (your )?(lost|stolen) (funds|money)|recuperar (tu|el) dinero (perdido|robado)|fund recovery",
     "It offers to recover lost money for a fee. After one scam, this is often the second.",
     "Ofrece recuperar dinero perdido a cambio de un pago. Tras un fraude, suele ser el segundo."),
)
_MONTHLY_RETURN = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*%\s*(?:de\s+)?(?:rendimiento\s+|return\s+|ganancia\s+|interest\s+|interés\s+)?"
    r"(mensual(?:es)?|al mes|por mes|cada mes|a month|per month|monthly|semanal(?:es)?|a la semana|per week|weekly|a week|"
    r"diari[oa]s?|al día|por día|daily|per day|a day)", re.IGNORECASE)
_PER_MONTH = {"week": 52 / 12, "day": 30.0}


def _period(word: str) -> str:
    word = word.lower()
    if any(w in word for w in ("seman", "week")):
        return "week"
    if any(w in word for w in ("diari", "día", "dia", "day", "dail")):
        return "day"
    return "month"


def _verify_places(country: str | None) -> list[dict]:
    mx = [
        {"name": "CONDUSEF SIPRES (registry of authorised financial institutions)", "url": SIPRES_URL, "country": "MX"},
        {"name": "CNBV Padrón de Entidades Supervisadas", "url": CNBV_PES_URL, "country": "MX"},
        {"name": "CONDUSEF Portal de Fraudes Financieros", "url": CONDUSEF_FRAUD_URL, "country": "MX"},
        {"name": "SAT buscador de correos falsos", "url": SAT_FAKE_EMAIL_URL, "country": "MX"},
    ]
    us = [
        {"name": "FINRA BrokerCheck", "url": BROKERCHECK_URL, "country": "US"},
        {"name": "SEC Investor.gov (adviser and broker search)", "url": INVESTOR_GOV_URL, "country": "US"},
        {"name": "FTC ReportFraud", "url": FTC_REPORT_URL, "country": "US"},
        {"name": "FBI Internet Crime Complaint Center (IC3)", "url": IC3_URL, "country": "US"},
    ]
    if country == "MX":
        return mx
    if country == "US":
        return us
    return mx + us


def scam_check(item: Any, situation: Mapping[str, Any] | None = None, *, jurisdiction: str | None = None,
               typical_transfer: float | None = None) -> dict:
    """Screen a message or a transfer; deterministic patterns plus amounts.

    ``item``: text, or ``{text?, amount?, currency?, payee?, new_payee?: bool, channel?}``.  A transfer is large
    when it is at least 20% of liquid assets, three times ``typical_transfer``, or (balances unknown) above a
    fixed per-currency amount.  Returns ``{risk: low|medium|high, reasons, verify, next_steps}``.
    """
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, Mapping):
        raise ValueError("item must be text or an object {text?, amount?, currency?, payee?, new_payee?}")
    text = " ".join(str(item.get(k) or "") for k in ("text", "payee", "description", "memo")).strip()
    if not text and item.get("amount") is None:
        raise ValueError("give the message text or the transfer (amount, payee)")
    sit = situation or {}
    country = _country(sit, jurisdiction or item.get("jurisdiction"))
    lowered = text.lower()
    reasons: list[dict] = []
    for code, severity, pattern, en, es in _FLAGS:
        # Agency acronyms are matched case-sensitively so ordinary words ("I sat down") do not trip them.
        match = (re.search(pattern, text) if code == "impersonation"
                 else re.search(pattern, lowered, re.IGNORECASE))
        if match:
            reasons.append(_msg(code, en, es, severity=severity, evidence=match.group(0)))
    promised = []
    for match in _MONTHLY_RETURN.finditer(text):
        rate = float(match.group(1).replace(",", ".")) / 100
        monthly = rate * _PER_MONTH.get(_period(match.group(2)), 1.0)
        promised.append(monthly)
    if promised:
        monthly = max(promised)
        flag, high = PARAMETERS["scam_monthly_return"]["value"], PARAMETERS["scam_monthly_return_high"]["value"]
        if monthly > flag:
            reasons.append(_msg("promised_monthly_return",
                                f"It promises about {_pct(monthly)} a month ({_pct((1 + monthly) ** 12 - 1)} a year "
                                "compounded). Government paper pays well under 1% a month.",
                                f"Promete cerca de {_pct(monthly)} al mes ({_pct((1 + monthly) ** 12 - 1)} al año "
                                "compuesto). CETES pagan bastante menos de 1% al mes.",
                                severity="high" if monthly >= high else "medium", monthly=round(monthly, 4)))
    # amounts
    amount = _num(item.get("amount"))
    amount_note = None
    if amount is not None:
        currency = item.get("currency") or sit.get("currency")
        liquid = _num((sit.get("net_worth") or {}).get("liquid"))
        if liquid is not None and currency and currency != sit.get("currency"):
            liquid = None
        new = item.get("new_payee")
        large, basis = None, None
        if liquid is not None and liquid > 0:
            large, basis = amount >= PARAMETERS["scam_large_share"]["value"] * liquid, \
                f"{_pct(amount / liquid)} of liquid assets"
        elif _num(typical_transfer):
            large, basis = amount >= 3 * _num(typical_transfer), f"{amount / _num(typical_transfer):.1f}x the usual transfer"
        elif currency in PARAMETERS["scam_large_absolute"]["value"]:
            large = amount >= PARAMETERS["scam_large_absolute"]["value"][currency]
            basis = f"above {PARAMETERS['scam_large_absolute']['value'][currency]:,} {currency} (balances unknown)"
        else:
            amount_note = "Balances and currency unknown, so I can't tell whether this transfer is large."
        if new is None:
            amount_note = "Whether the payee is new is unknown."
        if large and new is True:
            reasons.append(_msg("large_new_payee", f"A large transfer ({basis}) to a new payee.",
                                f"Una transferencia grande ({basis}) a un destinatario nuevo.", severity="medium"))
    highs = [r for r in reasons if r["severity"] == "high"]
    mediums = [r for r in reasons if r["severity"] == "medium"]
    if highs or len(mediums) >= 2:
        risk = "high"
    elif mediums:
        risk = "medium"
    else:
        risk = "low"
    steps = [
        _msg("pause", "Don't send money, codes or documents until it is verified.",
             "No envíes dinero, códigos ni documentos hasta verificar."),
        _msg("call_back", "Call the institution on the number printed on your card or its official site, not the "
             "number in the message.",
             "Llama a la institución al número de tu tarjeta o de su sitio oficial, no al del mensaje."),
    ]
    if risk != "low":
        steps.append(_msg("report", "If money already left, call your bank now to try to stop it, then report it "
                          + ("(CONDUSEF, police)." if country == "MX" else "(FTC, IC3, police)." if country == "US"
                             else "(CONDUSEF or FTC/IC3, and the police)."),
                          "Si ya salió dinero, llama a tu banco de inmediato para intentar detenerlo y denúncialo "
                          + ("(CONDUSEF, policía)." if country == "MX" else "(FTC, IC3, policía)." if country == "US"
                             else "(CONDUSEF o FTC/IC3, y policía).")))
    if any(r["code"] in ("guaranteed_return", "promised_monthly_return", "pig_butchering") for r in reasons):
        steps.append(_msg("registry", "Look the company up in the official registry before anything else; an "
                          "unlisted entity cannot legally take investments.",
                          "Busca la empresa en el registro oficial antes que nada; una entidad que no aparece no "
                          "puede captar inversiones legalmente."))
    sources = [{"title": "FTC, Investment scams", "url": FTC_INVESTMENT_URL},
               {"title": "FTC, A way to spot scams: how someone asks you to pay", "url": FTC_PAYMENT_URL},
               {"title": "FBI, Operation Level Up (confidence/crypto investment fraud)", "url": FBI_LEVEL_UP_URL},
               {"title": "CONDUSEF, alerta por suplantación de entidades financieras", "url": CONDUSEF_IMPERSONATION_URL},
               {"title": "SAT, buscador de correos falsos", "url": SAT_FAKE_EMAIL_URL}]
    for source in sources:
        source.update(checked_on=CHECKED_ON, status="verified")
    return {"risk": risk, "reasons": reasons, "verify": _verify_places(country), "next_steps": steps,
            "jurisdiction": country, "amount_note": amount_note, "screened": "patterns and amounts only",
            "the_person_decides": True, "trade_call": None, "sources": sources}


# ------------------------------------------------------------------ service tasks

TASKS = ("speculation_check", "panic_check", "scam_check")


def run_task(task: str, inputs: Mapping[str, Any], situation: Mapping[str, Any], ips: Mapping[str, Any] | None,
             preferences: Mapping[str, Any] | None = None) -> dict:
    """Service entry: returns the module envelope (``status``, ``result``, ``missing``, ...)."""
    if task not in TASKS:
        raise ValueError(f"guardrail tasks are {', '.join(TASKS)}")
    inputs = dict(inputs)
    if task == "speculation_check":
        allowed = {"proposal", "policy", "ips", "context"}
        unknown = sorted(set(inputs) - allowed)
        if unknown or "proposal" not in inputs:
            raise ValueError("speculation_check inputs: "
                             + (f"unknown {unknown}" if unknown else "missing proposal")
                             + "; expected {proposal, policy?, ips?, context?, facts?, as_of?}")
        stored = ((preferences or {}).get("preference.speculation") or {}).get("value")
        policy = inputs.get("policy") if inputs.get("policy") is not None else stored
        result = speculation_check(inputs["proposal"], situation, inputs.get("ips", ips), policy,
                                   context=inputs.get("context"))
        missing = result.pop("missing")
        status = "partial" if missing else "ready"
        sources = [PARAMETERS[k]["source"] | {"status": PARAMETERS[k]["status"], "checked_on": CHECKED_ON}
                   for k in ("speculation_cap_share", "max_position_loss_share", "drawdown_stop")]
        return {"status": status, "result": result, "missing": missing, "warnings": [],
                "sources": sources + result["education"]["sources"],
                "assumptions": ["Play-money limits are Wealth's policy rules, not forecasts; the person may set a "
                                "lower cap or a cap up to 10%."]}
    if task == "panic_check":
        allowed = {"request", "context"}
        unknown = sorted(set(inputs) - allowed)
        if unknown or "request" not in inputs:
            raise ValueError("panic_check inputs: " + (f"unknown {unknown}" if unknown else "missing request")
                             + "; expected {request, context?, facts?, as_of?}")
        out = panic_check(inputs["request"], situation, context=inputs.get("context"))
        missing = out["missing"] + list(out["result"]["tax_cost"].get("missing") or [])
        status = "partial" if missing or out["result"]["tax_cost"]["status"] != "ready" else "ready"
        return {"status": status, "result": out["result"], "missing": missing, "warnings": [],
                "sources": list(out["result"]["history"]["sources"]),
                "assumptions": ["Historical recoveries use the S&P 500 price index; a different portfolio recovers "
                                "differently (run the stress_request for this portfolio).",
                                "Tax ranges are rough statutory ranges; tax or mx_holdings gives the exact figure."]}
    allowed = {"item", "text", "transaction", "jurisdiction", "typical_transfer"}
    unknown = sorted(set(inputs) - allowed)
    item = inputs.get("item", inputs.get("transaction", inputs.get("text")))
    if unknown or item is None:
        raise ValueError("scam_check inputs: " + (f"unknown {unknown}" if unknown else "missing item")
                         + "; expected {item (text or {text?, amount?, currency?, payee?, new_payee?}), "
                           "jurisdiction?, typical_transfer?}")
    result = scam_check(item, situation, jurisdiction=inputs.get("jurisdiction"),
                        typical_transfer=inputs.get("typical_transfer"))
    return {"status": "ready", "result": result, "missing": [], "warnings": [],
            "sources": result["sources"],
            "assumptions": ["The screen is deterministic (patterns and amounts); a low result is not a guarantee "
                            "that something is safe."]}


def _no_trade_call_text(value: Any) -> Iterable[str]:
    """Every string in an output (used by tests)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _no_trade_call_text(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _no_trade_call_text(v)


__all__ = ["DEFAULT_POLICY", "GUARDRAIL_FACT_KEYS", "HISTORICAL_DRAWDOWNS", "PARAMETERS", "TASKS", "cool_off",
           "education_card", "panic_check", "run_task", "scam_check", "speculation_check"]
