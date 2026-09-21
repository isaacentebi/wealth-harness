"""Engine-drawn views: typed, bounded specs a calculation result carries.

The model never draws numbers. ``views_for(task, envelope)`` turns a successful
run result into at most two view specs; every figure in a spec is copied from
the envelope (the only derived numbers are allocation shares, computed here in
exact decimal arithmetic from envelope amounts). The model only places a view,
by writing a line containing nothing but ``[[view:<id>]]``; the page, or
``render_svg``/``render_png`` for text channels, draws it.

A spec is ``{id, kind, title, data, caption?, source}``:

* ``id``: ``<task>-<10 hex>``, a hash of the kind, title and data, so the same
  result always yields the same id (the service lists it; the parser rebuilds it).
* ``title``/``caption`` and every label are ``{"en", "es"}`` text, or a plain
  string for a proper name (an instrument, a merchant), or ``{"date": iso}``.
* values are ``{"t": type, "v": raw envelope value}``; ``v`` null means
  unknown and is drawn as "—", never as zero.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence
from xml.sax.saxutils import escape as _xml

KINDS = ("ticket", "allocation", "series", "comparison", "payoff")
VALUE_TYPES = ("money", "ratio", "percent", "months", "date", "count", "range", "text")
MAX_VIEWS = 2          # per result, and per answer
MAX_ROWS = 12          # ticket, allocation and payoff rows
MAX_OPTIONS = 4        # comparison columns
MAX_METRICS = 6
MAX_POINTS = 400
VIEW_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}-[0-9a-f]{10}$")
PLACEMENT = re.compile(r"^[ \t]*\[\[view:([a-z][a-z0-9_]{0,31}-[0-9a-f]{10})\]\][ \t]*$", re.M)
_ISO = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_OK = {"ready", "partial"}


# --------------------------------------------------------------------------- text and values


def L(en: str, es: str) -> dict[str, str]:
    return {"en": en, "es": es}


def _raw(value: Any) -> Any:
    """The envelope's own number (string, int or float), or None for anything else."""
    if isinstance(value, Mapping) and "amount" in value:
        value = value["amount"]
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float)):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        try:
            Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        return value
    return None


def money(value: Any, currency: str | None) -> dict[str, Any]:
    if currency is None and isinstance(value, Mapping):
        currency = value.get("currency")
    return {"t": "money", "v": _raw(value), "cur": str(currency or "")}


def ratio(value: Any) -> dict[str, Any]:
    return {"t": "ratio", "v": _raw(value)}


def percent(value: Any) -> dict[str, Any]:
    return {"t": "percent", "v": _raw(value)}


def months(value: Any) -> dict[str, Any]:
    return {"t": "months", "v": _raw(value)}


def count(value: Any) -> dict[str, Any]:
    return {"t": "count", "v": _raw(value)}


def when(value: Any) -> dict[str, Any]:
    return {"t": "date", "v": value if isinstance(value, str) and _ISO.match(value) else None}


def span(low: Any, high: Any, currency: str | None) -> dict[str, Any]:
    return {"t": "range", "lo": _raw(low), "hi": _raw(high), "cur": str(currency or "")}


def _dec(value: Any) -> Decimal | None:
    raw = _raw(value)
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _name(value: Any) -> str:
    text = " ".join(str(value or "").split())[:60]
    return text or "—"


def _title_name(value: Any) -> str:
    """A merchant or id as a person reads it: 'netflix com' -> 'Netflix com'."""
    text = _name(value)
    return text[:1].upper() + text[1:]


def _as_of(envelope: Mapping[str, Any], result: Mapping[str, Any]) -> str | None:
    for value in (result.get("end"), result.get("as_of"), result.get("sale_date"),
                  (result.get("horizon") or {}).get("as_of") if isinstance(result.get("horizon"), Mapping) else None,
                  str(envelope.get("evaluated_at") or "")[:10]):
        if isinstance(value, str) and _ISO.match(value):
            return value
    return None


# --------------------------------------------------------------------------- spec


TASK_LABELS = {
    "spending": L("Spending", "Gastos"), "dca": L("Recurring investing", "Inversión periódica"),
    "performance": L("Performance", "Rendimiento"), "project": L("Projection", "Proyección"),
    "income": L("Retirement income", "Ingreso para el retiro"), "stress": L("Stress test", "Prueba de estrés"),
    "debt_payoff": L("Debt payoff", "Pago de deudas"), "tax": L("Tax", "Impuestos"),
    "rebalance": L("Rebalance", "Rebalanceo"), "asset_location": L("Asset location", "Ubicación de activos"),
    "situation": L("Your picture", "Tu panorama"),
}


def _spec(task: str, kind: str, title: dict, data: dict, envelope: Mapping[str, Any],
          result: Mapping[str, Any], caption: dict | None = None) -> dict[str, Any]:
    digest = hashlib.sha256(json.dumps([kind, title, data], sort_keys=True, default=str).encode()).hexdigest()[:10]
    prefix = re.sub(r"[^a-z0-9_]", "_", task.lower())[:32] or "view"
    spec = {"id": f"{prefix}-{digest}", "kind": kind, "title": title, "data": data,
            "source": {"task": task, "label": TASK_LABELS.get(task, L(task, task)), "as_of": _as_of(envelope, result)}}
    if caption:
        spec["caption"] = caption
    return spec


def _label_ok(value: Any) -> bool:
    if isinstance(value, str):
        return 0 < len(value) <= 80
    if isinstance(value, Mapping):
        if set(value) == {"date"}:
            return isinstance(value["date"], str) and bool(_ISO.match(value["date"]))
        return set(value) == {"en", "es"} and all(isinstance(v, str) and 0 < len(v) <= 120 for v in value.values())
    return False


def _value_ok(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("t") not in VALUE_TYPES:
        return False
    kind = value["t"]
    if kind == "range":
        return set(value) == {"t", "lo", "hi", "cur"} and all(_raw(value[k]) == value[k] for k in ("lo", "hi"))
    if kind == "text":
        return set(value) == {"t", "v"} and _label_ok(value["v"])
    if kind == "date":
        return set(value) == {"t", "v"} and (value["v"] is None or bool(_ISO.match(str(value["v"]))))
    allowed = {"t", "v", "cur"} if kind == "money" else {"t", "v"}
    return set(value) <= allowed and "v" in value and _raw(value["v"]) == value["v"]


def validate(spec: Mapping[str, Any]) -> None:
    """Raise ValueError unless ``spec`` follows its kind's strict schema."""
    def need(ok: bool, what: str) -> None:
        if not ok:
            raise ValueError(f"view {spec.get('id')!r}: {what}")

    need(isinstance(spec, Mapping) and set(spec) <= {"id", "kind", "title", "data", "caption", "source"}, "unknown fields")
    need(isinstance(spec.get("id"), str) and bool(VIEW_ID.match(spec["id"])), "bad id")
    need(spec.get("kind") in KINDS, "bad kind")
    need(_label_ok(spec.get("title")) and not isinstance(spec.get("title"), str), "bad title")
    need("caption" not in spec or (_label_ok(spec["caption"]) and isinstance(spec["caption"], Mapping)), "bad caption")
    source = spec.get("source")
    need(isinstance(source, Mapping) and isinstance(source.get("task"), str) and _label_ok(source.get("label")), "bad source")
    data, kind = spec.get("data"), spec["kind"]
    need(isinstance(data, Mapping), "data must be an object")
    if kind == "ticket":
        need(set(data) == {"rows", "total"}, "ticket needs rows and total")
        rows = data["rows"]
        need(isinstance(rows, list) and 0 < len(rows) <= MAX_ROWS, "ticket rows")
        for row in rows:
            need(isinstance(row, Mapping) and set(row) <= {"label", "value", "sub"} and _label_ok(row.get("label"))
                 and _value_ok(row.get("value")), "ticket row")
        total = data["total"]
        need(total is None or (isinstance(total, Mapping) and set(total) == {"label", "value"}
                               and _label_ok(total["label"]) and _value_ok(total["value"])), "ticket total")
    elif kind == "allocation":
        need(set(data) == {"rows", "total"}, "allocation needs rows and total")
        rows = data["rows"]
        need(isinstance(rows, list) and 0 < len(rows) <= MAX_ROWS, "allocation rows")
        for row in rows:
            need(isinstance(row, Mapping) and set(row) <= {"label", "value", "share"} and _label_ok(row.get("label"))
                 and ("value" not in row or _value_ok(row["value"])), "allocation row")
            share = _dec(row.get("share"))
            need(share is not None and Decimal(0) <= share <= Decimal(1), "allocation share in [0, 1]")
        need(data["total"] is None or _value_ok(data["total"]), "allocation total")
    elif kind == "series":
        need(set(data) <= {"unit", "points", "reference", "band", "current"} and "points" in data and "unit" in data,
             "series fields")
        unit = data["unit"]
        need(isinstance(unit, Mapping) and unit.get("t") in {"money", "ratio", "percent", "count"}, "series unit")
        points = data["points"]
        need(isinstance(points, list) and len(points) <= MAX_POINTS, "series points")
        for point in points:
            need(isinstance(point, Mapping) and set(point) == {"x", "y"} and bool(_ISO.match(str(point["x"])))
                 and _raw(point["y"]) == point["y"] and point["y"] is not None, "series point")
        need(len(points) >= 2 or "band" in data, "a series needs two points or a band")
        if "reference" in data:
            ref = data["reference"]
            need(isinstance(ref, Mapping) and set(ref) == {"label", "y"} and _label_ok(ref["label"])
                 and _raw(ref["y"]) == ref["y"] and ref["y"] is not None, "series reference")
        if "band" in data:
            band = data["band"]
            need(isinstance(band, Mapping) and set(band) == {"x", "low", "mid", "high"}
                 and bool(_ISO.match(str(band["x"]))), "series band")
            need(all(_raw(band[k]) == band[k] and band[k] is not None for k in ("low", "mid", "high")), "band values")
        need(isinstance(data.get("current", False), bool), "series current")
    elif kind == "comparison":
        need(set(data) == {"options", "metrics"}, "comparison needs options and metrics")
        options, metrics = data["options"], data["metrics"]
        need(isinstance(options, list) and 2 <= len(options) <= MAX_OPTIONS, "2-4 options")
        for option in options:
            need(isinstance(option, Mapping) and set(option) <= {"label", "best"} and _label_ok(option.get("label"))
                 and isinstance(option.get("best", False), bool), "comparison option")
        need(isinstance(metrics, list) and 0 < len(metrics) <= MAX_METRICS, "comparison metrics")
        for metric in metrics:
            need(isinstance(metric, Mapping) and set(metric) <= {"label", "values", "better"} and _label_ok(metric.get("label"))
                 and isinstance(metric.get("values"), list) and len(metric["values"]) == len(options)
                 and all(_value_ok(v) for v in metric["values"]) and metric.get("better") in (None, "lower", "higher"),
                 "comparison metric")
    elif kind == "payoff":
        need(set(data) == {"rows", "total", "method", "alt_label"}, "payoff fields")
        rows = data["rows"]
        need(isinstance(rows, list) and 0 < len(rows) <= MAX_ROWS, "payoff rows")
        for row in rows:
            need(isinstance(row, Mapping) and set(row) <= {"label", "date", "months", "alt_date"}
                 and _label_ok(row.get("label")) and _value_ok(row.get("date")) and _value_ok(row.get("months"))
                 and ("alt_date" not in row or _value_ok(row["alt_date"])), "payoff row")
        total = data["total"]
        need(isinstance(total, Mapping) and set(total) == {"date", "months", "interest", "saved"}
             and all(_value_ok(total[k]) for k in total), "payoff total")
        need(data["method"] is None or _label_ok(data["method"]), "payoff method")
        need(data["alt_label"] is None or _label_ok(data["alt_label"]), "payoff alt label")


# --------------------------------------------------------------------------- labels

CATEGORY = {
    "bank_fees": L("Bank fees", "Comisiones bancarias"), "cash_withdrawal": L("Cash withdrawals", "Retiros de efectivo"),
    "childcare": L("Childcare", "Cuidado infantil"), "convenience": L("Convenience stores", "Tiendas de conveniencia"),
    "dining": L("Dining out", "Restaurantes"), "education": L("Education", "Educación"),
    "entertainment": L("Entertainment", "Entretenimiento"), "food_delivery": L("Food delivery", "Comida a domicilio"),
    "gifts_donations": L("Gifts and donations", "Regalos y donativos"), "groceries": L("Groceries", "Súper"),
    "health": L("Health", "Salud"), "housing": L("Housing", "Vivienda"), "insurance": L("Insurance", "Seguros"),
    "other": L("Other", "Otros"), "personal_care": L("Personal care", "Cuidado personal"),
    "shopping": L("Shopping", "Compras"), "subscriptions": L("Subscriptions", "Suscripciones"),
    "taxes": L("Taxes", "Impuestos"), "telecom": L("Phone and internet", "Teléfono e internet"),
    "transport": L("Transport", "Transporte"), "travel": L("Travel", "Viajes"), "utilities": L("Utilities", "Servicios"),
    "uncategorized": L("Not yet sorted", "Sin clasificar"),
}
METHOD = {
    "fifo": L("First in", "Primeras en entrar"), "lifo": L("Last in", "Últimas en entrar"),
    "hifo": L("Highest cost", "Mayor costo"), "specific_id": L("Lots you chose", "Lotes elegidos"),
    "tax_min": L("Lowest tax", "Menor impuesto"), "avalanche": L("Highest rate first", "Tasa más alta primero"),
    "snowball": L("Smallest balance first", "Saldo menor primero"),
    "dividend_cash": L("Live on dividends", "Vivir de dividendos"),
    "total_return_sales": L("Sell shares as needed", "Vender según se necesite"),
}
DEBT = {"car": L("Car loan", "Crédito automotriz"), "auto": L("Car loan", "Crédito automotriz"),
        "mortgage": L("Mortgage", "Hipoteca"), "card": L("Credit card", "Tarjeta de crédito"),
        "credit_card": L("Credit card", "Tarjeta de crédito"), "student": L("Student loan", "Crédito educativo"),
        "personal": L("Personal loan", "Préstamo personal")}


def _category(key: Any) -> dict | str:
    return CATEGORY.get(str(key), _title_name(str(key).replace("_", " ")))


def _method(key: Any) -> dict | str:
    return METHOD.get(str(key), _title_name(str(key).replace("_", " ")))


# --------------------------------------------------------------------------- builders per task


def _shares(items: Sequence[tuple[Any, Any]], currency: str) -> dict[str, Any] | None:
    """Allocation rows from (label, amount) pairs; shares are exact decimals of the envelope amounts."""
    amounts = [(label, _dec(value), value) for label, value in items]
    amounts = [(label, amount, raw) for label, amount, raw in amounts if amount is not None and amount > 0]
    if not amounts:
        return None
    amounts.sort(key=lambda item: item[1], reverse=True)
    total = sum(amount for _, amount, _ in amounts)
    rows = [{"label": label, "value": money(raw, currency),
             "share": str((amount / total).quantize(Decimal("0.0001")))} for label, amount, raw in amounts[:MAX_ROWS]]
    return {"rows": rows, "total": None}


def _spending(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    cur = result.get("currency")
    out: list[dict] = []
    if isinstance(result.get("months"), Mapping) and "by_status" in result:  # view=monthly
        months_ = sorted(result["months"].items())
        points = [{"x": month, "y": _raw(values.get("total"))} for month, values in months_
                  if isinstance(values, Mapping) and _raw(values.get("total")) is not None]
        if months_:
            month, latest = months_[-1]
            data = _shares([(_category(k), v) for k, v in (latest.get("by_category") or {}).items()], cur)
            if data:
                data["total"] = money(latest.get("total"), cur)
                out.append(_spec(task, "allocation", L("Where the money went", "En qué se fue el dinero"), data,
                                 envelope, result))
        if len(points) >= 2:
            out.append(_spec(task, "series", L("Spending by month", "Gasto por mes"),
                             {"unit": {"t": "money", "cur": str(cur or "")}, "points": points}, envelope, result))
        return out
    monthly = result.get("monthly")
    if isinstance(monthly, Mapping):  # view=surplus
        rows = [
            {"label": L("Regular income", "Ingreso regular"), "value": money(monthly.get("regular_income"), cur)},
            {"label": L("Essentials", "Gastos esenciales"), "value": money(monthly.get("essential_spending"), cur)},
            {"label": L("Everything else", "Otros gastos"), "value": money(monthly.get("discretionary_spending"), cur)},
            {"label": L("Debt payments", "Pagos de deudas"), "value": money(monthly.get("debt_service"), cur)},
            {"label": L("Left before the reserve", "Queda antes del fondo"),
             "value": money(monthly.get("surplus_before_reserve"), cur), "sub": True},
            {"label": L("To the emergency reserve", "Al fondo de emergencia"), "value": money(monthly.get("reserve_top_up"), cur)},
        ]
        total = {"label": L("Free to invest each month", "Libre para invertir al mes"),
                 "value": money(monthly.get("investable_surplus"), cur)}
        return [_spec(task, "ticket", L("Monthly cash flow", "Flujo mensual"), {"rows": rows, "total": total},
                      envelope, result)]
    if "regular_monthly_median" in result:  # view=income
        rows = [
            {"label": L("Typical month", "Mes típico"), "value": money(result.get("regular_monthly_median"), cur)},
            {"label": L("Monthly average", "Promedio mensual"), "value": money(result.get("regular_monthly_mean"), cur)},
            {"label": L("One-off income in the period", "Ingresos extraordinarios del periodo"),
             "value": money(result.get("irregular_total"), cur)},
            {"label": L("Months without regular income", "Meses sin ingreso regular"),
             "value": count(result.get("months_without_regular_income"))},
        ]
        return [_spec(task, "ticket", L("Income", "Ingresos"), {"rows": rows, "total": None}, envelope, result)]
    series = result.get("series")
    if isinstance(series, list):  # view=recurring
        charges = [s for s in series if isinstance(s, Mapping) and s.get("kind") == "expense"
                   and _dec(s.get("typical_amount")) is not None]
        charges.sort(key=lambda s: _dec(s["typical_amount"]), reverse=True)
        rows = [{"label": _title_name(s.get("merchant")), "value": money(s.get("typical_amount"), s.get("currency"))}
                for s in charges[:8]]
        if rows:
            return [_spec(task, "ticket", L("Recurring charges", "Cargos recurrentes"), {"rows": rows, "total": None},
                          envelope, result)]
    return out


def _dca(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    cur = result.get("currency")
    if "dca_end_value" in result:  # backtest
        caption = L("Historical prices, not a forecast", "Precios históricos, no un pronóstico")
        rows = [
            {"label": L("Invested", "Invertido"), "value": money(result.get("invested"), cur)},
            {"label": L("Value investing monthly", "Valor invirtiendo cada mes"), "value": money(result.get("dca_end_value"), cur)},
            {"label": L("Value investing it all at the start", "Valor invirtiendo todo al inicio"),
             "value": money(result.get("lump_sum_end_value"), cur)},
        ]
        out = [_spec(task, "ticket", L("Monthly investing, looking back", "Invertir cada mes, en retrospectiva"),
                     {"rows": rows, "total": {"label": L("Monthly minus all at once", "Mensual menos todo de una vez"),
                                              "value": money(result.get("dca_minus_lump_sum"), cur)}},
                     envelope, result, caption)]
        legs = result.get("legs") or []
        if len(legs) == 1 and isinstance(legs[0], Mapping):
            leg = legs[0]
            points = [{"x": b["executed"], "y": _raw(b.get("price"))} for b in leg.get("buys") or []
                      if isinstance(b, Mapping) and isinstance(b.get("executed"), str) and _ISO.match(b["executed"])
                      and _raw(b.get("price")) is not None]
            if len(points) >= 2:
                data: dict[str, Any] = {"unit": {"t": "money", "cur": str(cur or "")}, "points": points}
                if _raw(leg.get("dca_average_price")) is not None:
                    data["reference"] = {"label": L("Average price paid", "Precio promedio pagado"),
                                         "y": _raw(leg["dca_average_price"])}
                out.append(_spec(task, "series", L(f"Price paid for {_name(leg.get('instrument_id'))}",
                                                   f"Precio pagado por {_name(leg.get('instrument_id'))}"),
                                 data, envelope, result, caption))
        return out
    if "due_dates" in result:  # schedule
        plan = result.get("plan") or {}
        legs = [leg for leg in plan.get("legs") or [] if isinstance(leg, Mapping)]
        cur = plan.get("currency") or cur
        if len(legs) == 1:
            rows = [{"label": {"date": d}, "value": money(legs[0].get("amount"), cur)}
                    for d in result["due_dates"][:MAX_ROWS] if isinstance(d, str) and _ISO.match(d)]
            title = L("Contribution dates", "Fechas de aportación")
        else:  # several legs: what each contribution buys (the dates are the plan's cadence)
            rows = [{"label": _name(leg.get("instrument_id")), "value": money(leg.get("amount"), cur)}
                    for leg in legs[:MAX_ROWS]]
            title = L("Each contribution", "Cada aportación")
        if not rows:
            return []
        return [_spec(task, "ticket", title, {"rows": rows, "total": None}, envelope, result)]
    if "on_time_rate" in result:  # adherence
        rows = [
            {"label": L("Planned so far", "Planeado a la fecha"), "value": money(result.get("planned_to_date"), cur)},
            {"label": L("Invested so far", "Invertido a la fecha"), "value": money(result.get("invested_to_date"), cur)},
            {"label": L("On time", "A tiempo"), "value": ratio(result.get("on_time_rate"))},
            {"label": L("Next contribution", "Siguiente aportación"), "value": when(result.get("next_due"))},
        ]
        return [_spec(task, "ticket", L("How the plan is going", "Cómo va el plan"),
                      {"rows": rows, "total": {"label": L("Ahead or behind plan", "Adelanto o atraso"),
                                               "value": money(result.get("cumulative_variance"), cur)}},
                      envelope, result)]
    rng = result.get("range")
    if isinstance(rng, Mapping) and isinstance(result.get("split"), Mapping):  # suggest
        rows = [{"label": _name(k), "value": span(v.get("low"), v.get("high"), cur)}
                for k, v in sorted(result["split"].items()) if isinstance(v, Mapping)]
        if not rows:
            return []
        return [_spec(task, "ticket", L("A monthly amount to consider", "Un monto mensual a considerar"),
                      {"rows": rows[:MAX_ROWS], "total": {"label": L("Each month", "Cada mes"),
                                                          "value": span(rng.get("low"), rng.get("high"), cur)}},
                      envelope, result)]
    return []


def _performance(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    if "end_value" not in result:
        return []
    cur = result.get("currency")
    parts = result.get("decomposition") or {}
    twr = result.get("twr") or {}
    rows = [
        {"label": L("Value at the start", "Valor al inicio"), "value": money(result.get("start_value"), cur)},
        {"label": L("Money added", "Aportaciones"), "value": money(parts.get("contributions"), cur)},
        {"label": L("Money taken out", "Retiros"), "value": money(parts.get("withdrawals"), cur)},
        {"label": L("Gain or loss", "Ganancia o pérdida"), "value": money(parts.get("total_gain"), cur)},
        {"label": L("Return, time-weighted", "Rendimiento ponderado por tiempo"), "value": ratio(twr.get("period"))},
        {"label": L("Your annual return", "Tu rendimiento anual"), "value": ratio(result.get("xirr_annual"))},
    ]
    return [_spec(task, "ticket", L("How the investments did", "Cómo les fue a las inversiones"),
                  {"rows": rows, "total": {"label": L("Value at the end", "Valor al final"),
                                           "value": money(result.get("end_value"), cur)}}, envelope, result)]


_BAND_CAPTION = L("Simulated range: 1 in 10 outcomes ends below the low end, 1 in 10 above the high end",
                  "Rango simulado: 1 de cada 10 resultados queda abajo del mínimo y 1 de cada 10 arriba del máximo")


def _band(percentiles: Any, at: Any) -> dict | None:
    if not isinstance(percentiles, Mapping) or not (isinstance(at, str) and _ISO.match(at)):
        return None
    values = {k: _raw(percentiles.get(p)) for k, p in (("low", "p10"), ("mid", "p50"), ("high", "p90"))}
    if any(v is None for v in values.values()):
        return None
    return {"x": at, **values}


def _project(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    cur = result.get("currency")
    out: list[dict] = []
    end = (result.get("horizon") or {}).get("end_date")
    band = _band(result.get("terminal_wealth_percentiles"), end)
    if band:
        out.append(_spec(task, "series", L("Where it could end up", "A dónde podría llegar"),
                         {"unit": {"t": "money", "cur": str(cur or "")}, "points": [], "band": band},
                         envelope, result, _BAND_CAPTION))
    rows = [{"label": L("Ends with money left", "Termina con dinero"),
             "value": percent(result.get("positive_terminal_wealth_probability_percent"))},
            {"label": L("Every withdrawal paid", "Se cubren todos los retiros"),
             "value": percent(result.get("all_withdrawals_fulfilled_probability_percent"))}]
    goals = result.get("hard_goal_funded_probability_percent") or {}
    rows += [{"label": _title_name(str(goal).replace("_", " ")), "value": percent(p)}
             for goal, p in list(goals.items())[:MAX_ROWS - 2]]
    out.append(_spec(task, "ticket", L("Chances across the simulations", "Probabilidades en las simulaciones"),
                     {"rows": rows, "total": None}, envelope, result))
    return out


def _income(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    cur = result.get("currency")
    strategies = result.get("strategies")
    if not isinstance(strategies, Mapping) or not strategies:
        return []
    names = [k for k in strategies if isinstance(strategies[k], Mapping)][:MAX_OPTIONS]
    if len(names) >= 2:
        pick = lambda key, sub=None: [  # noqa: E731
            (strategies[n].get(key) or {}).get(sub) if sub else strategies[n].get(key) for n in names]
        p50 = [_dec(v) for v in pick("terminal_wealth_percentiles", "p50")]
        best = max(range(len(names)), key=lambda i: p50[i] if p50[i] is not None else Decimal("-Infinity"))
        options = [{"label": _method(n), "best": i == best} for i, n in enumerate(names)]
        metrics = [
            {"label": L("Typical ending wealth", "Patrimonio final típico"),
             "values": [money(v, cur) for v in pick("terminal_wealth_percentiles", "p50")], "better": "higher"},
            {"label": L("A bad-luck ending", "Final con mala suerte"),
             "values": [money(v, cur) for v in pick("terminal_wealth_percentiles", "p10")], "better": "higher"},
            {"label": L("Typical ending after selling everything", "Final típico tras vender todo"),
             "values": [money(v, cur) for v in pick("terminal_wealth_after_liquidation_tax_percentiles", "p50")],
             "better": "higher"},
            {"label": L("Taxes paid", "Impuestos pagados"),
             "values": [money(v, cur) for v in pick("mean_taxes_paid")], "better": "lower"},
            {"label": L("Chance of falling short", "Probabilidad de quedarse corto"),
             "values": [percent(v) for v in pick("probability_of_any_income_deficit_percent")], "better": "lower"},
        ]
        return [_spec(task, "comparison", L("Two ways to draw income", "Dos formas de sacar ingreso"),
                      {"options": options, "metrics": metrics}, envelope, result)]
    only = strategies[names[0]]
    out: list[dict] = []
    band = _band(only.get("terminal_wealth_percentiles"), (result.get("horizon") or {}).get("end_date"))
    if band:
        out.append(_spec(task, "series", L("Where it could end up", "A dónde podría llegar"),
                         {"unit": {"t": "money", "cur": str(cur or "")}, "points": [], "band": band},
                         envelope, result, _BAND_CAPTION))
    risk = result.get("sequence_risk") or {}
    if isinstance(risk, Mapping) and risk:
        rows = [{"label": L("Bad years come first", "Los años malos llegan primero"),
                 "value": money(risk.get("median_terminal_wealth_with_low_returns_first"), cur)},
                {"label": L("Good years come first", "Los años buenos llegan primero"),
                 "value": money(risk.get("median_terminal_wealth_with_high_returns_first"), cur)}]
        out.append(_spec(task, "ticket", L("Typical ending wealth, by the order of returns",
                                           "Patrimonio final típico según el orden de los rendimientos"),
                         {"rows": rows, "total": None}, envelope, result))
    return out[:MAX_VIEWS]


def _stress(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    scenarios = [s for s in result.get("scenarios") or [] if isinstance(s, Mapping)]
    if not scenarios:
        return []
    title = L("If markets repeat these moves", "Si los mercados repiten estos movimientos")
    if 2 <= len(scenarios) <= MAX_OPTIONS:
        assets = list((result.get("weights") or {}).keys())[:MAX_METRICS - 1]
        options = [{"label": _name(s.get("name")), "best": False} for s in scenarios]
        metrics = [{"label": L("Your portfolio", "Tu portafolio"),
                    "values": [ratio(s.get("portfolio_return")) for s in scenarios], "better": "higher"}]
        metrics += [{"label": _name(a), "values": [ratio((s.get("asset_returns") or {}).get(a)) for s in scenarios],
                     "better": "higher"} for a in assets]
        return [_spec(task, "comparison", title, {"options": options, "metrics": metrics}, envelope, result)]
    rows = [{"label": _name(s.get("name")), "value": ratio(s.get("portfolio_return"))} for s in scenarios[:MAX_ROWS]]
    return [_spec(task, "ticket", title, {"rows": rows, "total": None}, envelope, result)]


def _debt_payoff(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    chosen = result.get("chosen")
    if not isinstance(chosen, Mapping):
        return []
    cur = result.get("currency")
    minimums = {str(m.get("id")): m for m in result.get("minimums_only") or [] if isinstance(m, Mapping)}
    rows = []
    for item in chosen.get("payoff") or []:
        if not isinstance(item, Mapping):
            continue
        row = {"label": DEBT.get(str(item.get("id")), _title_name(str(item.get("id")).replace("_", " "))),
               "date": when(item.get("date")), "months": months(item.get("months"))}
        alt = minimums.get(str(item.get("id")))
        if alt is not None:
            row["alt_date"] = when(alt.get("date"))
        rows.append(row)
    if not rows:
        return []
    total = {"date": when(chosen.get("date")), "months": months(chosen.get("months")),
             "interest": money(chosen.get("interest"), cur),
             "saved": money(result.get("interest_saved_by_avalanche"), cur)}
    data = {"rows": rows[:MAX_ROWS], "total": total, "method": _method(chosen.get("label") or "avalanche"),
            "alt_label": L("Minimums only", "Solo mínimos") if minimums else None}
    return [_spec(task, "payoff", L("When the debts are gone", "Cuándo quedan pagadas las deudas"), data,
                  envelope, result)]


def _tax(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    cur = result.get("currency")
    mode = result.get("mode")
    if mode == "lot_selection" and isinstance(result.get("methods"), Mapping):
        methods = result["methods"]
        ranking = [m for m in result.get("ranking_by_incremental_tax") or [] if m in methods]
        chosen = ranking[:MAX_OPTIONS]
        if "fifo" in methods and "fifo" not in chosen and len(chosen) == MAX_OPTIONS:
            chosen[-1] = "fifo"  # the default method is the baseline people compare against
        if len(chosen) < 2:
            return []
        lowest = result.get("lowest_tax_method")
        options = [{"label": _method(m), "best": m == lowest} for m in chosen]
        realized = lambda m, k: (methods[m].get("realized") or {}).get(k)  # noqa: E731
        metrics = [
            {"label": L("Extra tax", "Impuesto adicional"),
             "values": [money(methods[m].get("incremental_tax"), cur) for m in chosen], "better": "lower"},
            {"label": L("Short-term gain or loss", "Ganancia o pérdida a corto plazo"),
             "values": [money(realized(m, "short_term"), cur) for m in chosen]},
            {"label": L("Long-term gain or loss", "Ganancia o pérdida a largo plazo"),
             "values": [money(realized(m, "long_term"), cur) for m in chosen]},
        ]
        return [_spec(task, "comparison", L("Which shares to sell", "Qué acciones vender"),
                      {"options": options, "metrics": metrics}, envelope, result)]
    if mode in {"rebalance", "harvest"} and isinstance(result.get("included_scenario_gain_or_loss"), Mapping):
        gains = result["included_scenario_gain_or_loss"]
        after = ((result.get("netting") or {}).get("after_scenario") or {})
        estimate = result.get("incremental_tax_estimate")
        rows = [
            {"label": L("Short-term gain or loss", "Ganancia o pérdida a corto plazo"), "value": money(gains.get("short_term"), cur)},
            {"label": L("Long-term gain or loss", "Ganancia o pérdida a largo plazo"), "value": money(gains.get("long_term"), cur)},
            {"label": L("Loss used against income", "Pérdida aplicada al ingreso"),
             "value": money(after.get("ordinary_income_loss_deduction"), cur)},
            {"label": L("Short-term loss carried forward", "Pérdida a corto plazo para años siguientes"),
             "value": money(after.get("short_term_carryforward"), cur)},
            {"label": L("Long-term loss carried forward", "Pérdida a largo plazo para años siguientes"),
             "value": money(after.get("long_term_carryforward"), cur)},
        ]
        tax = estimate.get("incremental_tax") if isinstance(estimate, Mapping) else None
        return [_spec(task, "ticket", L("Tax effect of these sales", "Efecto fiscal de estas ventas"),
                      {"rows": rows, "total": {"label": L("Change in federal tax", "Cambio en el impuesto federal"),
                                               "value": money(tax, cur)}},
                      envelope, result, L("Estimate before filing", "Estimación antes de declarar"))]
    return []


def _first(item: Mapping, *keys: str) -> Any:
    for key in keys:
        if item.get(key) is not None:
            return item[key]
    return None


def _rebalance(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    """Trades and target mix; tolerant of the rebalance module's shape (guarded, never raises)."""
    cur = result.get("currency")
    out: list[dict] = []
    trades = [t for t in result.get("trades") or [] if isinstance(t, Mapping)]
    rows = []
    for trade in trades[:MAX_ROWS]:
        name = _name(_first(trade, "instrument_id", "symbol", "ticker", "name"))
        side = str(_first(trade, "side", "action", "direction") or "").lower()
        label = (L(f"Buy {name}", f"Comprar {name}") if side.startswith("buy")
                 else L(f"Sell {name}", f"Vender {name}") if side.startswith("sell") else name)
        rows.append({"label": label, "value": money(_first(trade, "amount", "value", "notional", "trade_value"),
                                                      trade.get("currency") or cur)})
    if rows:
        total = None
        tax = _first(result, "estimated_tax", "incremental_tax")
        if tax is not None:
            total = {"label": L("Estimated tax", "Impuesto estimado"), "value": money(tax, cur)}
        out.append(_spec(task, "ticket", L("Trades to rebalance", "Operaciones para rebalancear"),
                         {"rows": rows, "total": total}, envelope, result))
    weights = _first(result, "target_weights", "weights_after", "target")
    if isinstance(weights, Mapping) and weights:
        shares = sorted(((k, _dec(v), _raw(v)) for k, v in weights.items()),
                        key=lambda kv: kv[1] or Decimal(0), reverse=True)
        alloc = [{"label": _name(k), "share": raw} for k, v, raw in shares
                 if v is not None and Decimal(0) <= v <= Decimal(1)][:MAX_ROWS]
        if alloc:
            out.append(_spec(task, "allocation", L("The mix after rebalancing", "La mezcla después de rebalancear"),
                             {"rows": alloc, "total": None}, envelope, result))
    return out


def _asset_location(result: Mapping, envelope: Mapping, task: str) -> list[dict]:
    cur = result.get("currency")
    rows = []
    for item in [p for p in _first(result, "placements", "assignments", "locations") or [] if isinstance(p, Mapping)]:
        name = _name(_first(item, "instrument_id", "symbol", "ticker", "name"))
        account = _name(_first(item, "account_name", "account_id", "account"))
        rows.append({"label": f"{name} → {account}", "value": money(_first(item, "amount", "value"), item.get("currency") or cur)})
    if not rows:
        return []
    saved = _first(result, "annual_tax_saved", "tax_drag_saved", "estimated_savings")
    total = {"label": L("Tax saved each year", "Impuesto ahorrado al año"), "value": money(saved, cur)} if saved is not None else None
    return [_spec(task, "ticket", L("Where each holding lives", "Dónde vive cada inversión"),
                  {"rows": rows[:MAX_ROWS], "total": total}, envelope, result)]


def _situation(sit: Mapping, envelope: Mapping, task: str) -> list[dict]:
    out: list[dict] = []
    nw = sit.get("net_worth") or {}
    if isinstance(nw, Mapping) and nw.get("total") is not None:
        cur = nw.get("currency") or sit.get("currency")
        rows = [{"label": L("What you own", "Lo que tienes"), "value": money(nw.get("assets"), cur)},
                {"label": L("What you owe", "Lo que debes"), "value": money(nw.get("liabilities"), cur)}]
        out.append(_spec(task, "ticket", L("Where you stand today", "Dónde estás hoy"),
                         {"rows": rows, "total": {"label": L("Net worth", "Patrimonio neto"),
                                                  "value": money(nw.get("total") if nw.get("complete", True) else None, cur)}},
                         envelope, sit))
    flow = sit.get("cash_flow") or {}
    if isinstance(flow, Mapping) and (flow.get("income") is not None or flow.get("spending") is not None):
        cur = flow.get("currency") or sit.get("currency")
        rows = [{"label": L("Income", "Ingresos"), "value": money(flow.get("income"), cur)},
                {"label": L("Spending", "Gastos"), "value": money(flow.get("spending"), cur)},
                {"label": L("Debt payments", "Pagos de deudas"), "value": money(flow.get("debt_payments"), cur)}]
        out.append(_spec(task, "ticket", L("A typical month", "Un mes típico"),
                         {"rows": rows, "total": {"label": L("Left each month", "Queda cada mes"),
                                                  "value": money(flow.get("surplus") if flow.get("complete", True) else None, cur)}},
                         envelope, sit))
    return out


BUILDERS: dict[str, Callable[[Mapping, Mapping, str], list[dict]]] = {
    "spending": _spending, "dca": _dca, "performance": _performance, "project": _project, "income": _income,
    "stress": _stress, "debt_payoff": _debt_payoff, "tax": _tax, "rebalance": _rebalance,
    "asset_location": _asset_location,
}


def views_for(task: str, envelope: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """0-2 validated view specs for a run result; [] for anything unsuccessful or unknown.

    ``task`` is the wealth_run task, or ``situation`` for the saved picture (the
    envelope is then the situation itself, or ``{"situation": ...}``).
    """
    if not isinstance(task, str) or not isinstance(envelope, Mapping):
        return []
    try:
        if task == "situation":
            sit = envelope.get("situation", envelope)
            specs = _situation(sit, {}, "situation") if isinstance(sit, Mapping) else []
        else:
            builder = BUILDERS.get(task)
            result = envelope.get("result")
            if builder is None or not isinstance(result, Mapping):
                return []
            status = envelope.get("status")
            # A surplus with an unknown reserve target is still worth drawing: the unknown shows as unknown.
            if status not in _OK and not (task == "spending" and status == "needs_input"
                                          and isinstance(result.get("monthly"), Mapping)):
                return []
            specs = builder(result, envelope, task)
    except (AttributeError, TypeError, KeyError, ValueError, InvalidOperation):
        return []  # a view must never break a result
    valid = []
    for spec in specs:
        try:
            validate(spec)
        except ValueError:
            continue
        valid.append(spec)
    return valid[:MAX_VIEWS]


def summaries(specs: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """What the model sees: id, kind and an English title per view."""
    return [{"id": s["id"], "kind": s["kind"], "title": s["title"]["en"]} for s in specs]


def placed_ids(text: str, available: Mapping[str, Any] | None = None) -> list[str]:
    """Ids the answer placed on their own lines, in order, deduplicated, at most MAX_VIEWS."""
    found: list[str] = []
    for match in PLACEMENT.finditer(text or ""):
        view_id = match.group(1)
        if view_id in found or (available is not None and view_id not in available):
            continue
        found.append(view_id)
        if len(found) == MAX_VIEWS:
            break
    return found


# --------------------------------------------------------------------------- formatting for text channels

MINUS = "−"
_MONTHS = {"en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
           "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]}
_UNITS = {"months": L("{n} months", "{n} meses"), "month": L("{n} month", "{n} mes")}
UNKNOWN = L("— means not known yet", "— significa que aún no se sabe")


def text(value: Any, lang: str) -> str:
    lang = "es" if lang == "es" else "en"
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if "date" in value:
            return format_date(value["date"], lang)
        return str(value.get(lang) or value.get("en") or "")
    return ""


def format_date(iso: Any, lang: str) -> str:
    if not isinstance(iso, str) or not _ISO.match(iso):
        return "—"
    year, month = int(iso[:4]), int(iso[5:7])
    name = _MONTHS["es" if lang == "es" else "en"][month - 1]
    if len(iso) == 7:
        return f"{name} {year}"
    day = int(iso[8:10])
    return f"{day} {name} {year}" if lang == "es" else f"{name} {day}, {year}"


def _grouped(number: Decimal, places: int) -> str:
    return f"{abs(number):,.{places}f}"


def format_value(value: Mapping[str, Any], lang: str) -> str:
    kind = value.get("t")
    if kind == "text":
        return text(value.get("v"), lang)
    if kind == "date":
        return format_date(value.get("v"), lang)
    if kind == "range":
        low = format_value({"t": "money", "v": value.get("lo"), "cur": value.get("cur")}, lang)
        high = format_value({"t": "money", "v": value.get("hi"), "cur": value.get("cur")}, lang)
        return "—" if "—" in (low, high) else f"{low}–{high}"
    number = _dec(value.get("v"))
    if number is None:
        return "—"
    sign = MINUS if number < 0 else ""
    if kind == "money":
        places = 0 if abs(number) >= 100 or number == number.to_integral() else 2
        return f"{sign}${_grouped(number, places)}"
    if kind == "ratio":
        return f"{sign}{_grouped(number * 100, 1)}%"
    if kind == "percent":
        return f"{sign}{_grouped(number, 0 if number == number.to_integral() else 1)}%"
    if kind == "months":
        n = int(number)
        return _UNITS["month" if n == 1 else "months"][lang if lang == "es" else "en"].format(n=n)
    return f"{sign}{_grouped(number, 0 if number == number.to_integral() else 2)}"


# --------------------------------------------------------------------------- drawing (SVG and PNG share one display list)

INK, SOFT, MUTED, HAIRLINE, SURFACE, COBALT = "#2B2522", "#4A413C", "#6E625B", "#D9CEC6", "#FFFCF7", "#4358C7"
WIDTH, PAD = 640, 32


def _wrap(value: str, size: int, width: float, mono: bool = False) -> list[str]:
    """Greedy word wrap by an approximate glyph advance (no font metrics needed)."""
    limit = max(8, int(width / (size * (0.6 if mono else 0.54))))
    lines: list[str] = []
    for word in value.split():
        if lines and len(lines[-1]) + 1 + len(word) <= limit:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines or [""]


def _ops(spec: Mapping[str, Any], lang: str) -> tuple[list[tuple], int]:
    """Primitives for one view: ('text', x, y, str, size, weight, anchor, mono, color) and friends."""
    data, kind = spec["data"], spec["kind"]
    ops: list[tuple] = []
    y = PAD - 8
    for line in _wrap(text(spec["title"], lang), 20, WIDTH - 2 * PAD):
        y += 26
        ops.append(("text", PAD, y, line, 20, 600, "start", False, INK))
    y += 10
    if spec.get("caption"):
        for line in _wrap(text(spec["caption"], lang), 13, WIDTH - 2 * PAD):
            y += 18
            ops.append(("text", PAD, y, line, 13, 400, "start", False, MUTED))
    right = WIDTH - PAD
    unknown = False

    def value(v: Mapping) -> str:
        nonlocal unknown
        shown = format_value(v, lang)
        unknown = unknown or shown == "—"
        return shown

    def rule(at: float, weight: float = 1, color: str = HAIRLINE) -> None:
        ops.append(("line", PAD, at, right, at, weight, color))

    if kind == "ticket":
        y += 12
        for i, row in enumerate(data["rows"]):
            if i:
                rule(y)
            y += 30
            ops.append(("text", PAD + (16 if row.get("sub") else 0), y, text(row["label"], lang), 16, 400, "start", False, SOFT))
            ops.append(("text", right, y, value(row["value"]), 16, 400, "end", True, INK))
            y += 16
        if data["total"]:
            rule(y + 2, 1.5, INK)
            y += 34
            ops.append(("text", PAD, y, text(data["total"]["label"], lang), 16, 600, "start", False, INK))
            ops.append(("text", right, y, value(data["total"]["value"]), 16, 500, "end", True, INK))
            y += 16
    elif kind == "allocation":
        y += 12
        for row in data["rows"]:
            y += 28
            share = Decimal(str(row["share"]))
            shown = format_value({"t": "ratio", "v": row["share"]}, lang)
            if "value" in row:
                shown = f"{value(row['value'])}  {shown}"
            ops.append(("text", PAD, y, text(row["label"], lang), 16, 400, "start", False, SOFT))
            ops.append(("text", right, y, shown, 15, 400, "end", True, INK))
            y += 10
            rule(y + 3)
            ops.append(("rect", PAD, y, max(2.0, float(share) * (right - PAD)), 6, INK))
            y += 14
    elif kind == "series":
        unit = data["unit"]
        fmt = lambda raw: value({"t": unit["t"], "v": raw, **({"cur": unit.get("cur", "")} if unit["t"] == "money" else {})})  # noqa: E731
        points = data["points"]
        if points:
            ys = [float(Decimal(str(p["y"]))) for p in points]
            if "reference" in data:
                ys.append(float(Decimal(str(data["reference"]["y"]))))
            lo, hi = min(ys), max(ys)
            pad = (hi - lo) * 0.12 or abs(hi) * 0.1 or 1
            lo, hi = lo - pad, hi + pad
            top, bottom, left, plot_right = y + 40, y + 220, PAD, right - 110
            sx = lambda i: left + (plot_right - left) * (i / (len(points) - 1))  # noqa: E731
            sy = lambda v: bottom - (bottom - top) * ((v - lo) / (hi - lo))  # noqa: E731
            ops.append(("line", left, bottom, right, bottom, 1, HAIRLINE))
            # The extremes anchor the scale, unless the labelled last point already is one.
            for extreme in (max, min):
                raw = extreme(points, key=lambda p: Decimal(str(p["y"])))["y"]
                if Decimal(str(raw)) != Decimal(str(points[-1]["y"])):
                    ey = sy(float(Decimal(str(raw))))
                    ops.append(("text", plot_right + 12, ey + 4, fmt(raw), 12, 400, "start", True, MUTED))
            if "reference" in data:
                ry = sy(float(Decimal(str(data["reference"]["y"]))))
                ops.append(("line", left, ry, plot_right, ry, 1, HAIRLINE))
            coords = [(sx(i), sy(v)) for i, v in enumerate(ys[:len(points)])]
            ops.append(("poly", coords, 2, INK))
            last_x, last_y = coords[-1]
            ops.append(("circle", last_x, last_y, 4, COBALT if data.get("current") else INK))
            ops.append(("text", last_x + 12, last_y + 5, fmt(points[-1]["y"]), 15, 500, "start", True, INK))
            ops.append(("text", left, bottom + 22, format_date(points[0]["x"], lang), 12, 400, "start", True, MUTED))
            ops.append(("text", plot_right, bottom + 22, format_date(points[-1]["x"], lang), 12, 400, "end", True, MUTED))
            y = bottom + 30
            if "reference" in data:
                y += 22
                ops.append(("line", left, y - 4, left + 16, y - 4, 1, MUTED))
                ops.append(("text", left + 24, y, f"{text(data['reference']['label'], lang)}  {fmt(data['reference']['y'])}",
                            12, 400, "start", True, MUTED))
        elif "band" in data:
            band = data["band"]
            low, mid, high = (float(Decimal(str(band[k]))) for k in ("low", "mid", "high"))
            y += 34
            ops.append(("text", PAD, y, format_date(band["x"], lang), 13, 400, "start", True, MUTED))
            span_ = (high - low) or 1
            scale = lambda v: PAD + 40 + (right - PAD - 80) * ((v - low) / span_)  # noqa: E731
            y += 56
            ops.append(("text", scale(mid), y - 22, fmt(band["mid"]), 20, 600, "middle", True, INK))
            ops.append(("line", PAD, y, right, y, 1, HAIRLINE))
            ops.append(("rect", scale(low), y - 3, scale(high) - scale(low), 6, INK))
            ops.append(("circle", scale(mid), y, 6, INK))
            y += 28
            ops.append(("text", scale(low), y, fmt(band["low"]), 14, 400, "middle", True, SOFT))
            ops.append(("text", scale(high), y, fmt(band["high"]), 14, 400, "middle", True, SOFT))
            y += 8
    elif kind == "comparison":
        # Options run across the full width; each metric's label sits on its own line above its values,
        # so four options fit a 640-wide image without colliding.
        options = data["options"]
        col = (right - PAD) / len(options)
        wrapped = [_wrap(text(o["label"], lang), 14, col - 16) for o in options]
        top = y + 18
        for i, lines in enumerate(wrapped):
            for j, line in enumerate(lines):
                ops.append(("text", right - col * (len(options) - 1 - i), top + 18 * (j + 1), line, 14,
                            600 if options[i].get("best") else 400, "end", False, INK))
        y = top + 18 * max(len(lines) for lines in wrapped) + 10
        rule(y, 1.5, INK)
        for metric in data["metrics"]:
            y += 26
            ops.append(("text", PAD, y, text(metric["label"], lang), 13, 400, "start", False, MUTED))
            y += 26
            for i, v in enumerate(metric["values"]):
                weight = 600 if options[i].get("best") else 400
                ops.append(("text", right - col * (len(options) - 1 - i), y, value(v), 16, weight, "end", True, INK))
            y += 12
            rule(y)
    elif kind == "payoff":
        y += 12
        for i, row in enumerate(data["rows"]):
            if i:
                rule(y)
            y += 30
            ops.append(("text", PAD, y, text(row["label"], lang), 16, 400, "start", False, SOFT))
            ops.append(("text", right, y, value(row["date"]), 16, 500, "end", True, INK))
            y += 20
            detail = value(row["months"])
            if "alt_date" in row and data["alt_label"]:
                detail += f" · {text(data['alt_label'], lang)}: {value(row['alt_date'])}"
            ops.append(("text", right, y, detail, 12, 400, "end", True, MUTED))
            y += 10
        total = data["total"]
        rule(y + 2, 1.5, INK)
        y += 8
        if len(data["rows"]) > 1:  # with one debt, its date already is the debt-free date
            y += 26
            ops.append(("text", PAD, y, L("Debt-free", "Sin deudas")[lang if lang == "es" else "en"], 16, 600, "start", False, INK))
            ops.append(("text", right, y, value(total["date"]), 16, 600, "end", True, INK))
        y += 26
        ops.append(("text", PAD, y, L("Interest paid", "Intereses pagados")[lang if lang == "es" else "en"], 14, 400, "start", False, SOFT))
        ops.append(("text", right, y, value(total["interest"]), 14, 400, "end", True, INK))
        y += 6
    source = spec["source"]
    y += 30
    origin = text(source["label"], lang).upper()
    if source.get("as_of"):
        origin += " · " + format_date(source["as_of"], lang).upper()
    ops.append(("text", PAD, y, origin, 11, 400, "start", True, MUTED))
    if unknown:
        y += 20
        ops.append(("text", PAD, y, text(UNKNOWN, lang), 12, 400, "start", False, MUTED))
    return ops, int(y + PAD)


def render_svg(spec: Mapping[str, Any], lang: str = "en") -> str:
    """A standalone Dot-styled SVG of one view (text is escaped; no scripts, no external refs)."""
    validate(spec)
    ops, height = _ops(spec, lang)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" '
             f'role="img" aria-label="{_xml(text(spec["title"], lang), {chr(34): "&quot;"})}">',
             f'<rect width="{WIDTH}" height="{height}" fill="{SURFACE}"/>']
    for op in ops:
        if op[0] == "text":
            _, x, y, value, size, weight, anchor, mono, color = op
            family = "Roboto Mono, Menlo, monospace" if mono else "Inter, Helvetica, Arial, sans-serif"
            numeric = ' font-variant-numeric="tabular-nums"' if mono else ""
            parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
                         f'text-anchor="{anchor}" fill="{color}"{numeric}>{_xml(value)}</text>')
        elif op[0] == "line":
            _, x1, y1, x2, y2, width, color = op
            parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{width}"/>')
        elif op[0] == "rect":
            _, x, y, w, h, color = op
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="2" fill="{color}"/>')
        elif op[0] == "poly":
            _, coords, width, color = op
            points = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="{width}" '
                         'stroke-linejoin="round" stroke-linecap="round"/>')
        elif op[0] == "circle":
            _, cx, cy, r, color = op
            parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{color}" stroke="{SURFACE}" stroke-width="2"/>')
    parts.append("</svg>")
    return "".join(parts)


_SANS = ("/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial Unicode.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf")
_SANS_BOLD = ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf")
_MONO = ("/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
         "/usr/share/fonts/dejavu/DejaVuSansMono.ttf")


def png_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


def render_png(spec: Mapping[str, Any], lang: str = "en", scale: int = 2) -> bytes | None:
    """The same drawing as a PNG, when Pillow is installed; None otherwise (serve the SVG instead)."""
    validate(spec)
    try:
        from io import BytesIO

        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    ops, height = _ops(spec, lang)
    fonts: dict[tuple, Any] = {}

    def font(size: int, weight: int, mono: bool):
        key = (size, weight >= 600, mono)
        if key not in fonts:
            paths = _MONO if mono else (_SANS_BOLD if weight >= 600 else _SANS)
            loaded = None
            for path in paths:
                try:
                    loaded = ImageFont.truetype(path, size * scale)
                    break
                except OSError:
                    continue
            fonts[key] = (loaded or ImageFont.load_default(size=size * scale), loaded is not None)
        return fonts[key]

    image = Image.new("RGB", (WIDTH * scale, height * scale), SURFACE)
    draw = ImageDraw.Draw(image)
    s = lambda v: v * scale  # noqa: E731
    for op in ops:
        if op[0] == "text":
            _, x, y, value, size, weight, anchor, mono, color = op
            face, unicode_ok = font(size, weight, mono)
            if not unicode_ok:  # Pillow's bundled face lacks these glyphs
                value = value.replace(MINUS, "-").replace("—", "-").replace("–", "-").replace("→", ">").replace("●", "*")
            draw.text((s(x), s(y)), value, font=face, fill=color, anchor={"start": "ls", "end": "rs", "middle": "ms"}[anchor])
        elif op[0] == "line":
            _, x1, y1, x2, y2, width, color = op
            draw.line([(s(x1), s(y1)), (s(x2), s(y2))], fill=color, width=max(1, round(width * scale)))
        elif op[0] == "rect":
            _, x, y, w, h, color = op
            draw.rounded_rectangle([s(x), s(y), s(x + w), s(y + h)], radius=2 * scale, fill=color)
        elif op[0] == "poly":
            _, coords, width, color = op
            draw.line([(s(x), s(y)) for x, y in coords], fill=color, width=round(width * scale), joint="curve")
        elif op[0] == "circle":
            _, cx, cy, r, color = op
            draw.ellipse([s(cx - r - 2), s(cy - r - 2), s(cx + r + 2), s(cy + r + 2)], fill=SURFACE)
            draw.ellipse([s(cx - r), s(cy - r), s(cx + r), s(cy + r)], fill=color)
    buffer = BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
