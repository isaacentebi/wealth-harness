"""Protection: life and disability needs, health cover, an estate checklist and a life-event router.

All outputs are ranges and checklists with a status, never product picks,
legal drafting or a single "10x income" multiple.  Unknown inputs stay
unknown and are listed; they are never counted as zero.

* :func:`life_need` — needs minus resources (Life Happens' needs approach):
  income replacement for the years dependants need support, plus debts, plus
  education goals and final expenses, minus liquid assets and existing cover.
  Shown only when there are dependants.
* :func:`disability_gap` — the monthly income a long-term disability policy
  typically replaces (40-65% of pre-tax pay) against existing benefits.
* :func:`health_review` — Mexico: gastos médicos mayores (policy, deductible
  against the reserve, coaseguro); US: out-of-pocket maximum and the HSA link.
* :func:`estate_checklist` — testamento/will, beneficiaries, poder notarial,
  marital regime and US-situs exposure via :mod:`wealth.estate`; it refers to
  a notario or attorney for anything legal.
* :func:`life_event` — the ordered reviews after marriage, a birth, divorce,
  a death, a job change or layoff, a move, an inheritance, a home purchase or
  retirement, in English and Mexican Spanish.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from . import estate as estate_module
from .guardrails import CHECKED_ON, _country, _num

LIFE_HAPPENS_URL = "https://lifehappens.org/life-insurance-101/how-much-life-insurance-do-i-need/"
LIFE_HAPPENS_DI_URL = "https://lifehappens.org/disability-insurance-101/how-much-does-disability-insurance-cost/"
IRS_RP_2025_19_URL = "https://www.irs.gov/pub/irs-drop/rp-25-19.pdf"
SAT_DEDUCTIONS_URL = "https://www.sat.gob.mx/minisitio/DeduccionesPersonales/index.html"
MES_TESTAMENTO_URL = "https://colegiodenotarios.org.mx/septiembre-mes-testamento"


def _p(value: Any, status: str, title: str, url: str | None = None, note: str | None = None) -> dict:
    row = {"value": value, "status": status, "checked_on": CHECKED_ON, "source": {"title": title}}
    if url:
        row["source"]["url"] = url
    if note:
        row["note"] = note
    return row


PARAMETERS: dict[str, dict] = {
    "life_method": _p("needs minus resources", "verified",
                      "Life Happens, How much life insurance do I need? (Calculation 2: expenses minus resources)",
                      LIFE_HAPPENS_URL),
    "life_replacement_ratio": _p([0.6, 0.8], "policy",
                                 "Wealth assumption: the household needs 60-80% of the income it loses",
                                 note="A planning convention, not a sourced figure; pass life.replacement_ratio to override."),
    "support_until_age": _p([22, 25], "policy", "Wealth assumption: support until the youngest dependant is 22-25"),
    "support_years_unknown_ages": _p([10, 20], "policy", "Wealth assumption when dependant ages are unknown"),
    "disability_replacement": _p([0.40, 0.65], "verified",
                                 "Life Happens: long-term disability insurance typically replaces 40-65% of pre-tax earnings",
                                 LIFE_HAPPENS_DI_URL),
    "hsa_limit_2026": _p({"self": 4400, "family": 8750, "catch_up_55": 1000}, "verified",
                         "IRS Rev. Proc. 2025-19 (2026 HSA limits)", IRS_RP_2025_19_URL,
                         note="Figures confirmed via Thomson Reuters and IRS-derived summaries; the PDF did not render."),
    "hdhp_min_deductible_2026": _p({"self": 1700, "family": 3400}, "verified", "IRS Rev. Proc. 2025-19", IRS_RP_2025_19_URL),
    "hdhp_oop_max_2026": _p({"self": 8500, "family": 17000}, "verified", "IRS Rev. Proc. 2025-19", IRS_RP_2025_19_URL),
    "mx_gmm_premium_deductible": _p("LISR Art. 151 fr. VI", "needs_verification",
                                    "SAT, Deducciones personales (gastos médicos mayores premiums)", SAT_DEDUCTIONS_URL,
                                    note="Cited in docs/notes/scope.md 1.10; fraction not re-read on the SAT page."),
    "mes_del_testamento": _p("September, notary fees up to 50% off", "verified",
                             "Colegio Nacional del Notariado Mexicano / SEGOB, Septiembre Mes del Testamento (24th edition, 2026)",
                             MES_TESTAMENTO_URL),
    "will_review_years": _p(5, "policy", "Wealth: review estate documents every 3-5 years and after any life event "
                                         "(docs/notes/scope.md 1.11)"),
}


def _src(key: str) -> dict:
    row = PARAMETERS[key]
    return {**row["source"], "checked_on": row["checked_on"], "status": row["status"]}


def _range(low: float | None, high: float | None) -> list[float] | None:
    """A rounded, non-negative ``[low, high]`` that is always ordered."""
    if low is None or high is None:
        return None
    return sorted([round(max(0.0, low), -2), round(max(0.0, high), -2)])


def _pair(value: Any, field: str) -> tuple[float, float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), float(value)
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(_num(v) is not None for v in value):
        low, high = float(value[0]), float(value[1])
        if low > high:
            raise ValueError(f"{field} must be [low, high]")
        return low, high
    raise ValueError(f"{field} must be a number or [low, high]")


def _policies(policies: Any, kinds: tuple[str, ...]) -> list[dict] | None:
    """Policies of these kinds; ``None`` when no policy list was given (unknown, not none)."""
    if policies is None:
        return None
    if not isinstance(policies, list):
        raise ValueError("policies must be a list of {kind, ...}")
    out = []
    for index, row in enumerate(policies):
        if not isinstance(row, Mapping) or not isinstance(row.get("kind"), str):
            raise ValueError(f"policies[{index}] must be an object with kind")
        if row["kind"].lower() in kinds:
            out.append(dict(row))
    return out


# ------------------------------------------------------------------ life


def life_need(situation: Mapping[str, Any], policies: Any = None, life: Mapping[str, Any] | None = None) -> dict:
    """Needs-minus-resources life insurance range; ``applies`` is False without dependants."""
    sit = situation or {}
    life = dict(life or {})
    profile = sit.get("profile") or {}
    currency = sit.get("currency")
    dependents = profile.get("dependents")
    if isinstance(dependents, str):  # "2", "0" from a form; anything else is not a count
        dependents = int(dependents.strip()) if dependents.strip().isdigit() else None
    elif isinstance(dependents, (list, tuple)):
        dependents = len(dependents)
    elif isinstance(dependents, bool):
        dependents = int(dependents)
    elif dependents is not None and (_num(dependents) is None or float(dependents) < 0):
        raise ValueError("profile.dependents must be a count of people")
    if dependents is None:
        return {"applies": None, "missing": ["client.profile.dependents"],
                "why": "Life cover matters when someone depends on your income; I don't know yet whether anyone does."}
    if dependents == 0:
        return {"applies": False, "missing": [],
                "why": "No one depends on your income, so there is no life-insurance need to size."}
    missing = []
    assumptions = ["Income replacement is not discounted: returns on the payout and inflation are assumed to "
                   "roughly offset."]
    monthly = _num((sit.get("income") or {}).get("monthly"))
    if monthly is not None and monthly < 0:
        # A loss-making month is not income to replace; replacing it would lower the need (and invert the range).
        assumptions.append("Monthly income is negative, so no income replacement is counted; pass a typical "
                           "positive income to size it.")
        monthly = 0.0
    annual = monthly * 12 if monthly is not None else None
    if annual is None:
        missing.append("income")
    if life.get("years") is not None:
        years = _pair(life["years"], "life.years")
        years_basis = "stated"
    else:
        ages = [a for a in (profile.get("dependent_ages") or []) if _num(a) is not None]
        if ages:
            youngest = min(float(a) for a in ages)
            lo_age, hi_age = PARAMETERS["support_until_age"]["value"]
            years = (max(0.0, lo_age - youngest), max(0.0, hi_age - youngest))
            years_basis = f"until the youngest ({youngest:g}) is {lo_age}-{hi_age}"
        else:
            years = tuple(PARAMETERS["support_years_unknown_ages"]["value"])
            years_basis = "dependant ages unknown: 10-20 years assumed"
            missing.append("client.profile.dependent_ages")
    ratio = _pair(life["replacement_ratio"], "life.replacement_ratio") if life.get("replacement_ratio") is not None \
        else tuple(PARAMETERS["life_replacement_ratio"]["value"])
    if min(years) < 0 or min(ratio) < 0:
        raise ValueError("life.years and life.replacement_ratio must not be negative")
    replacement = (annual * ratio[0] * years[0], annual * ratio[1] * years[1]) if annual is not None else None

    debts, unknown_debts = 0.0, []
    for row in sit.get("liabilities") or []:
        value = _num(row.get("value"))
        if value is None:
            unknown_debts.append(row.get("name") or row.get("id"))
        else:
            debts += value
    if unknown_debts:
        missing += [f"debt in {currency}: {d}" for d in unknown_debts]
    education = 0.0
    for goal in sit.get("goals") or []:
        if goal.get("action") == "education" and goal.get("status") == "active":
            target = _num(goal.get("target_amount"))
            if target is None or (goal.get("currency") and goal.get("currency") != currency):
                missing.append(f"goals.{goal.get('id')}.target_amount in {currency}")
            else:
                education += target
    if _num(life.get("education")) is not None:
        education += float(life["education"])
    final = _num(life.get("final_expenses"))
    if final is None:
        assumptions.append("Final expenses (funeral, medical, estate costs) are not included; pass life.final_expenses.")
        final = 0.0  # excluded and said so, not assumed to be zero cost
    liquid = _num((sit.get("net_worth") or {}).get("liquid"))
    if liquid is None:
        missing.append("liquid assets")
    elif liquid < 0:  # negative liquid wealth is debt, already counted under debts; it is not a resource
        liquid = 0.0
    life_policies = _policies(policies, ("life", "vida"))
    cover = None
    if life_policies is None:
        missing.append("policies (existing life cover)")
    else:
        cover = 0.0
        for row in life_policies:
            amount = _num(row.get("cover_amount"))
            if amount is None or (row.get("currency") and row.get("currency") != currency):
                missing.append(f"life policy {row.get('insurer') or ''} cover_amount in {currency}".strip())
                cover = None
                break
            cover += amount
    gross = None if replacement is None else (replacement[0] + debts + education + final,
                                              replacement[1] + debts + education + final)
    complete = gross is not None and liquid is not None and cover is not None and not unknown_debts
    # Unknown resources are never subtracted as zero: without them only the gross need is shown.
    need = (gross[0] - liquid - cover, gross[1] - liquid - cover) \
        if gross is not None and liquid is not None and cover is not None else None
    return {
        "applies": True, "currency": currency, "dependents": dependents, "method": PARAMETERS["life_method"]["value"],
        "components": {
            "income_replacement": _range(*replacement) if replacement else None,
            "annual_income": annual, "income_is_net": (sit.get("income") or {}).get("net"),
            "years": list(years), "years_basis": years_basis, "replacement_ratio": list(ratio),
            "debts": round(debts, 2), "education": round(education, 2), "final_expenses": final or None,
            "liquid_assets": liquid, "existing_cover": cover,
        },
        "need_range": _range(*need) if need else None,
        "gross_need_range": _range(*gross) if gross else None,
        "resources_known": {"liquid_assets": liquid, "existing_cover": cover},
        "complete": complete, "missing": missing, "assumptions": assumptions,
        "not_a_multiple": "A range from your own figures, not a multiple of income.",
        "refer": {"en": "Price term cover through a licensed insurance agent; compare at least three quotes.",
                  "es": "Cotiza un seguro de vida temporal con un agente autorizado; compara al menos tres opciones."},
        "sources": [_src("life_method"), _src("life_replacement_ratio")],
    }


# ------------------------------------------------------------------ disability


def disability_gap(situation: Mapping[str, Any], policies: Any = None, disability: Mapping[str, Any] | None = None) -> dict:
    sit = situation or {}
    disability = dict(disability or {})
    currency = sit.get("currency")
    monthly = _num(disability.get("gross_monthly_income"))
    net_used = False
    if monthly is None:
        monthly = _num((sit.get("income") or {}).get("monthly"))
        net_used = bool((sit.get("income") or {}).get("net"))
    missing = []
    if monthly is None:
        return {"status": "unknown", "missing": ["income"], "currency": currency,
                "why": "I need your income to size the gap."}
    low, high = PARAMETERS["disability_replacement"]["value"]
    target = (monthly * low, monthly * high)
    existing = None
    di = _policies(policies, ("disability", "invalidez", "incapacidad"))
    other = _num(disability.get("other_monthly_benefit"))
    if di is None and other is None:
        missing.append("policies (disability) or disability.other_monthly_benefit (employer, IMSS, SSDI)")
    else:
        existing = sum(_num(p.get("monthly_benefit")) or 0.0 for p in (di or [])) + (other or 0.0)
        if any(_num(p.get("monthly_benefit")) is None for p in (di or [])):
            missing.append("disability policy monthly_benefit")
            existing = None
    essential = _num((sit.get("spending") or {}).get("essential")) or _num((sit.get("spending") or {}).get("monthly"))
    return {
        "status": "ready" if existing is not None else "partial", "currency": currency, "monthly_income": monthly,
        "income_is_net": net_used,
        "typical_benefit_range": [round(target[0], -1), round(target[1], -1)],
        "existing_monthly_benefit": existing,
        "gap_range": None if existing is None else [round(max(0.0, target[0] - existing), -1),
                                                    round(max(0.0, target[1] - existing), -1)],
        "essential_spending": essential,
        "essential_covered_by_existing": None if existing is None or essential is None else existing >= essential,
        "reserve_months": (sit.get("reserve") or {}).get("months"),
        "note": ("Applied to take-home pay, so the range is understated; give gross_monthly_income for a better figure."
                 if net_used else None),
        "why": {"en": "A long illness or injury is more likely during a working life than an early death, and "
                      "policies usually start paying after a 3-6 month wait that the reserve has to cover.",
                "es": "Una enfermedad o lesión larga es más probable en la vida laboral que una muerte temprana, y las "
                      "pólizas suelen pagar tras una espera de 3 a 6 meses que cubre el fondo de emergencia."},
        "missing": missing, "sources": [_src("disability_replacement")],
    }


# ------------------------------------------------------------------ health


def health_review(situation: Mapping[str, Any], policies: Any = None, country: str | None = None,
                  health: Mapping[str, Any] | None = None) -> dict:
    sit = situation or {}
    health = dict(health or {})
    country = country or _country(sit)
    currency = sit.get("currency")
    reserve = _num((sit.get("reserve") or {}).get("amount"))
    if country == "MX":
        gmm = _policies(policies, ("gmm", "gastos_medicos_mayores", "health", "major_medical"))
        if gmm is None:
            return {"jurisdiction": "MX", "status": "unknown", "missing": ["policies (gastos médicos mayores)"],
                    "why": {"en": "I don't know whether you have major-medical cover.",
                            "es": "No sé si tienes seguro de gastos médicos mayores."}}
        if not gmm:
            return {"jurisdiction": "MX", "status": "gap", "has_policy": False, "missing": [],
                    "why": {"en": "No gastos médicos mayores policy on record. A hospital stay without it can take "
                                  "years of savings; IMSS/ISSSTE access, if you have it, is the fallback.",
                            "es": "No hay póliza de gastos médicos mayores registrada. Una hospitalización sin ella "
                                  "puede costar años de ahorro; el IMSS o ISSSTE, si tienes acceso, es el respaldo."},
                    "tax_note": {"en": "Premiums are a personal deduction in the annual return.",
                                 "es": "Las primas son deducción personal en la declaración anual.",
                                 "source": _src("mx_gmm_premium_deductible")},
                    "refer": {"en": "Compare policies with a licensed agent.",
                              "es": "Compara pólizas con un agente autorizado."}}
        rows = []
        for policy in gmm:
            deductible = _num(policy.get("deductible"))
            coins = _num(policy.get("coinsurance"))
            cap = _num(policy.get("coinsurance_cap"))
            worst = deductible + cap if deductible is not None and cap is not None else None
            rows.append({
                "insurer": policy.get("insurer"), "deductible": deductible, "coinsurance": coins,
                "coinsurance_cap": cap, "worst_case_out_of_pocket": worst,
                "reserve_covers_deductible": None if deductible is None or reserve is None else reserve >= deductible,
                "reserve_covers_worst_case": None if worst is None or reserve is None else reserve >= worst,
                "sum_insured": _num(policy.get("sum_insured")),
                "missing": [f for f, v in (("deductible", deductible), ("coinsurance", coins),
                                           ("coinsurance_cap", cap)) if v is None],
                "coinsurance_note": None if coins is None or cap is not None else
                "Coaseguro without a cap (tope) has no ceiling on your share.",
            })
        return {"jurisdiction": "MX", "status": "ready", "has_policy": True, "policies": rows, "reserve": reserve,
                "currency": currency, "missing": [m for r in rows for m in r["missing"]],
                "tax_note": {"en": "Premiums are a personal deduction in the annual return.",
                             "es": "Las primas son deducción personal en la declaración anual.",
                             "source": _src("mx_gmm_premium_deductible")}}
    if country == "US":
        plans = _policies(policies, ("health", "hdhp", "medical"))
        if plans is None:
            return {"jurisdiction": "US", "status": "unknown", "missing": ["policies (health plan)"],
                    "why": {"en": "I don't know your health plan's deductible or out-of-pocket maximum.",
                            "es": "No conozco el deducible ni el máximo de gastos de tu plan de salud."}}
        if not plans:
            return {"jurisdiction": "US", "status": "gap", "has_policy": False, "missing": [],
                    "why": {"en": "No health plan on record; an uninsured hospital stay is one of the most common "
                                  "causes of US medical debt.",
                            "es": "No hay plan de salud registrado; una hospitalización sin seguro es de las causas "
                                  "más comunes de deuda médica en EE.UU."}}
        rows = []
        birth_year = (sit.get("profile") or {}).get("birth_year")
        year = int((sit.get("as_of") or datetime.now(timezone.utc).date().isoformat())[:4])
        for plan in plans:
            coverage = plan.get("coverage") if plan.get("coverage") in ("self", "family") else None
            deductible = _num(plan.get("deductible"))
            oop = _num(plan.get("out_of_pocket_max"))
            hsa = None
            if year == 2026 and coverage and deductible is not None:
                minimum = PARAMETERS["hdhp_min_deductible_2026"]["value"][coverage]
                eligible = plan.get("hdhp") is True and deductible >= minimum
                limits = PARAMETERS["hsa_limit_2026"]["value"]
                catch_up = bool(birth_year and year - birth_year >= 55)
                hsa = {"eligible": eligible if plan.get("hdhp") is not None else None,
                       "min_deductible": minimum,
                       "annual_limit": limits[coverage] + (limits["catch_up_55"] if catch_up else 0) if eligible else None,
                       "catch_up_included": catch_up if eligible else None,
                       "note": "An HSA pairs with a qualifying high-deductible plan: pre-tax in, tax-free out for "
                               "medical costs, and it can be invested.",
                       "source": _src("hsa_limit_2026")}
            elif year != 2026:
                hsa = {"eligible": None, "note": f"HSA limits for {year} are not loaded; only 2026 is verified."}
            rows.append({"name": plan.get("name"), "coverage": coverage, "deductible": deductible,
                         "out_of_pocket_max": oop,
                         "reserve_covers_out_of_pocket_max": None if oop is None or reserve is None else reserve >= oop,
                         "hsa": hsa,
                         "missing": [f for f, v in (("coverage", coverage), ("deductible", deductible),
                                                    ("out_of_pocket_max", oop)) if v is None]})
        return {"jurisdiction": "US", "status": "ready", "has_policy": True, "plans": rows, "reserve": reserve,
                "currency": currency, "missing": [m for r in rows for m in r["missing"]]}
    return {"jurisdiction": None, "status": "unknown", "missing": ["client.profile.residence"],
            "why": {"en": "Health cover depends on where you live.", "es": "La cobertura médica depende de dónde vives."}}


# ------------------------------------------------------------------ estate checklist

_YES = {"yes": "done", "true": "done", "done": "done", "no": "missing", "false": "missing", "missing": "missing",
        "unknown": "unknown", "n/a": "not_applicable", "not_applicable": "not_applicable"}


def _status(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "done" if value else "missing"
    return _YES.get(str(value).lower(), "unknown")


_ESTATE_ITEMS = {
    # item: (English noun phrase, Spanish noun phrase, plural?) for the status sentence
    "testamento": ("a will (testamento)", "testamento", False),
    "beneficiarios_afore": ("beneficiaries registered at your AFORE", "beneficiarios registrados en tu AFORE", True),
    "beneficiarios_seguros": ("beneficiaries on your insurance policies", "beneficiarios en tus pólizas de seguro", True),
    "beneficiarios_cuentas": ("beneficiaries on your bank and brokerage accounts",
                              "beneficiarios en tus cuentas de banco y casa de bolsa", True),
    "poder_notarial": ("a poder notarial", "poder notarial", False),
    "voluntad_anticipada": ("a documento de voluntad anticipada", "documento de voluntad anticipada", False),
    "regimen_matrimonial": ("your marital regime on record", "tu régimen matrimonial registrado", False),
    "will": ("a will", "testamento", False),
    "beneficiaries_retirement": ("beneficiaries on your 401(k)/IRA", "beneficiarios en tu 401(k)/IRA", True),
    "beneficiaries_insurance": ("beneficiaries on your life insurance", "beneficiarios en tu seguro de vida", True),
    "transfer_on_death": ("TOD/POD designations on your accounts", "designaciones TOD/POD en tus cuentas", True),
    "power_of_attorney": ("a durable power of attorney", "poder notarial duradero", False),
    "healthcare_proxy": ("a healthcare proxy", "representante médico", False),
    "revocable_trust": ("a revocable trust", "fideicomiso revocable", False),
}
_REGISTER_ITEMS = {"testamento", "beneficiarios_afore", "beneficiarios_seguros", "beneficiarios_cuentas",
                   "regimen_matrimonial", "will", "beneficiaries_retirement", "beneficiaries_insurance",
                   "transfer_on_death"}


def _status_text(status: str, en: str, es: str, plural: bool) -> dict:
    """What we know about one item, said plainly: unknown is never "you don't have"."""
    if status == "unknown":
        return {"en": f"I don't know whether you have {en}.", "es": f"No sé si tienes {es}."}
    if status == "missing":
        return {"en": f"You told me you don't have {en}.", "es": f"Me dijiste que no tienes {es}."}
    if status == "review":
        return {"en": f"{en[0].upper() + en[1:]} {'are' if plural else 'is'} due for review.",
                "es": f"Conviene revisar tu {es}." if not plural else f"Conviene revisar tus {es}."}
    if status == "done":
        return {"en": f"You have {en}.", "es": f"Ya tienes {es}."}
    return {"en": "Does not apply.", "es": "No aplica."}


def estate_checklist(situation: Mapping[str, Any], estate: Mapping[str, Any] | None = None,
                     country: str | None = None, as_of: date | None = None) -> dict:
    """A document checklist with a status per item; never legal drafting."""
    sit = situation or {}
    estate = dict(estate or {})
    country = country or _country(sit)
    today = as_of or date.fromisoformat(sit.get("as_of") or datetime.now(timezone.utc).date().isoformat())
    items: list[dict] = []
    beneficiaries = estate.get("beneficiaries") if isinstance(estate.get("beneficiaries"), Mapping) else {}
    will_status = _status(estate.get("will"))
    will_date = estate.get("will_date")
    if will_status == "done" and will_date:
        age = (today - date.fromisoformat(str(will_date)[:10])).days / 365.25
        if age > PARAMETERS["will_review_years"]["value"]:
            will_status = "review"
    married = estate.get("married")
    regime = estate.get("marital_regime")

    def add(code: str, status: str, en: str, es: str, refer: str | None = None, **extra: Any) -> None:
        items.append({"item": code, "status": status, "en": en, "es": es, "refer": refer, **extra})

    if country == "MX":
        september = today.month == 9
        next_sep = date(today.year + (1 if today.month > 9 else 0), 9, 1)
        add("testamento", will_status,
            "A testamento público abierto before a notario names who inherits and a guardian for minors; without "
            "one, heirs go through an intestate succession that takes longer and costs more. "
            + ("It is Mes del Testamento now: notaries offer up to 50% off this September."
               if september else f"Mes del Testamento runs every September (next: {next_sep:%B %Y}), with notary fees "
                                 "up to 50% off."),
            "Un testamento público abierto ante notario dice quién hereda y quién sería tutor de menores; sin él, "
            "los herederos pasan por una sucesión intestamentaria más lenta y cara. "
            + ("Estamos en el Mes del Testamento: los notarios dan hasta 50% de descuento este septiembre."
               if september else "El Mes del Testamento es cada septiembre, con hasta 50% de descuento en honorarios."),
            refer="notario", source=_src("mes_del_testamento"))
        add("beneficiarios_afore", _status(beneficiaries.get("afore")),
            "Beneficiaries registered at your AFORE (update them in the AFORE app or branch).",
            "Beneficiarios registrados en tu AFORE (se actualizan en la app o sucursal de la AFORE).")
        add("beneficiarios_seguros", _status(beneficiaries.get("insurance")),
            "Beneficiaries named on each life and accident policy.",
            "Beneficiarios designados en cada póliza de vida y accidentes.")
        add("beneficiarios_cuentas", _status(beneficiaries.get("accounts")),
            "Beneficiaries on bank and casa de bolsa accounts; they receive those balances directly.",
            "Beneficiarios en cuentas de banco y casa de bolsa; reciben esos saldos directamente.")
        add("poder_notarial", _status(estate.get("power_of_attorney")),
            "A poder notarial lets someone you trust act for you if you cannot.",
            "Un poder notarial permite que alguien de confianza actúe por ti si no puedes.", refer="notario")
        add("voluntad_anticipada", _status(estate.get("advance_directive")),
            "A documento de voluntad anticipada records your medical wishes (where your state recognises it).",
            "Un documento de voluntad anticipada registra tus decisiones médicas (donde tu estado lo reconoce).",
            refer="notario")
        if married is True:
            regime_status = "unknown" if regime not in ("sociedad_conyugal", "separacion_de_bienes") else "done"
            explain = {
                "sociedad_conyugal": ("Under sociedad conyugal, property acquired during the marriage is shared; your "
                                      "will can only leave your half.",
                                      "En sociedad conyugal los bienes adquiridos en el matrimonio son de ambos; tu "
                                      "testamento solo dispone de tu mitad."),
                "separacion_de_bienes": ("Under separación de bienes each spouse owns their own property; without a will "
                                         "or beneficiaries your spouse is not automatically your only heir.",
                                         "En separación de bienes cada quien es dueño de lo suyo; sin testamento ni "
                                         "beneficiarios tu cónyuge no hereda automáticamente todo."),
            }.get(regime, ("Which regime you married under (sociedad conyugal or separación de bienes) decides what your "
                           "will can leave; it is on the acta de matrimonio.",
                           "El régimen con el que te casaste (sociedad conyugal o separación de bienes) decide de qué "
                           "puede disponer tu testamento; aparece en el acta de matrimonio."))
            add("regimen_matrimonial", regime_status, explain[0], explain[1], refer="notario", regime=regime)
        elif married is None:
            add("regimen_matrimonial", "unknown", "Married or not decides whether the marital regime matters.",
                "Estar casado o no decide si importa el régimen matrimonial.")
    elif country == "US":
        add("will", will_status, "A will names heirs and a guardian for minor children.",
            "Un testamento nombra herederos y tutor de hijos menores.", refer="estate attorney")
        add("beneficiaries_retirement", _status(beneficiaries.get("retirement")),
            "Beneficiaries on 401(k)/IRA accounts override the will.",
            "Los beneficiarios del 401(k)/IRA prevalecen sobre el testamento.")
        add("beneficiaries_insurance", _status(beneficiaries.get("insurance")),
            "Beneficiaries on life insurance.", "Beneficiarios del seguro de vida.")
        add("transfer_on_death", _status(beneficiaries.get("accounts")),
            "Transfer-on-death or payable-on-death designations on bank and brokerage accounts avoid probate.",
            "Las designaciones TOD/POD en cuentas de banco e inversión evitan el proceso sucesorio (probate).")
        add("power_of_attorney", _status(estate.get("power_of_attorney")),
            "A durable financial power of attorney.", "Un poder notarial financiero duradero.", refer="estate attorney")
        add("healthcare_proxy", _status(estate.get("advance_directive")),
            "A healthcare proxy and living will.", "Un representante médico y testamento vital.", refer="estate attorney")
        add("revocable_trust", _status(estate.get("revocable_trust")),
            "A revocable trust is worth discussing if you own real estate in several states or want privacy.",
            "Un fideicomiso revocable vale la pena si tienes inmuebles en varios estados o buscas privacidad.",
            refer="estate attorney")
    else:
        add("jurisdiction", "unknown", "Estate documents depend on where you live.",
            "Los documentos sucesorios dependen de dónde vives.")

    # US-situs estate exposure (non-US persons, including Mexico residents)
    situs = None
    request = estate.get("us_situs")
    if isinstance(request, Mapping):
        report = estate_module.us_estate_exposure(dict(request))
        exposure = (report.get("result") or {}).get("exposure")
        situs = {"status": report["status"], "exposure": exposure, "missing": report.get("missing"),
                 "task": "estate"}
        items.append({"item": "us_situs_estate", "status": "review" if report["status"] != "needs_input" else "unknown",
                      "en": "US-situs assets (US shares and US-domiciled ETFs, including via the SIC) can owe US estate "
                            "tax above US$60,000 for a non-resident; see the exposure figures.",
                      "es": "Los activos con situs en EE.UU. (acciones y ETF domiciliados en EE.UU., también vía SIC) "
                            "pueden pagar impuesto sucesorio de EE.UU. arriba de US$60,000 para un no residente.",
                      "refer": "estate attorney", "exposure": exposure})
    elif country == "MX":
        domicile = ((sit.get("holdings") or {}).get("domicile") or {})
        if domicile.get("US"):
            items.append({"item": "us_situs_estate", "status": "review",
                          "en": "You hold US-domiciled securities; run the estate exposure (task estate) to see the "
                                "US estate-tax range.",
                          "es": "Tienes valores domiciliados en EE.UU.; revisa la exposición al impuesto sucesorio de "
                                "EE.UU. (tarea estate).", "refer": None, "task": "estate"})
    for item in items:
        what = _ESTATE_ITEMS.get(item["item"])
        if what is None:
            continue
        item["status_text"] = _status_text(item["status"], *what)
        if item["item"] in _REGISTER_ITEMS:
            item["task"] = "estate_register"  # who would receive each account, the intestate split and the gaps
    order = {"missing": 0, "review": 1, "unknown": 2, "done": 3, "not_applicable": 4}
    items.sort(key=lambda i: order.get(i["status"], 5))
    return {"jurisdiction": country, "items": items, "us_situs": situs,
            "summary": {s: sum(1 for i in items if i["status"] == s) for s in order},
            "not_legal_advice": {"en": "A checklist, not legal drafting: a notario (Mexico) or estate attorney (US) "
                                       "prepares and signs the documents.",
                                 "es": "Una lista de revisión, no redacción legal: un notario (México) o un abogado "
                                       "(EE.UU.) prepara y firma los documentos."}}


# ------------------------------------------------------------------ protection review


def protection_review(situation: Mapping[str, Any], inputs: Mapping[str, Any] | None = None) -> dict:
    inputs = dict(inputs or {})
    sit = situation or {}
    country = inputs.get("jurisdiction") if inputs.get("jurisdiction") in ("MX", "US") else _country(sit)
    policies = inputs.get("policies")
    life = life_need(sit, policies, inputs.get("life"))
    disability = disability_gap(sit, policies, inputs.get("disability"))
    health = health_review(sit, policies, country, inputs.get("health"))
    estate = estate_checklist(sit, inputs.get("estate"), country)
    missing = sorted(set(life.get("missing") or []) | set(disability.get("missing") or [])
                     | set(health.get("missing") or []))
    gaps = []
    if life.get("applies") and life.get("need_range") and life["need_range"][1] > 0:
        gaps.append("life")
    if disability.get("gap_range") and disability["gap_range"][1] > 0:
        gaps.append("disability")
    if health.get("status") == "gap":
        gaps.append("health")
    if estate["summary"].get("missing"):
        gaps.append("estate")
    return {"jurisdiction": country, "life": life, "disability": disability, "health": health, "estate": estate,
            "gaps": gaps, "missing": missing, "trade_call": None,
            "refer": {"en": "Insurance is bought through a licensed agent; legal documents through a notario or attorney.",
                      "es": "Los seguros se contratan con un agente autorizado; los documentos legales con un notario "
                            "o abogado."}}


# ------------------------------------------------------------------ life events

EVENTS = ("marriage", "birth_or_adoption", "divorce", "death_in_family", "job_change", "layoff", "relocation",
          "inheritance", "home_purchase", "retirement")
_ALIASES = {"birth": "birth_or_adoption", "adoption": "birth_or_adoption", "death": "death_in_family",
            "job_loss": "layoff", "move": "relocation", "home": "home_purchase", "wedding": "marriage"}


def _s(area: str, en: str, es: str, facts: list[str] | None = None, tasks: list[str] | None = None,
       where: str = "all", refer: str | None = None) -> dict:
    return {"area": area, "en": en, "es": es, "facts": facts or [], "tasks": tasks or [], "jurisdiction": where,
            "refer": refer}


def _n(days: int, en: str, es: str) -> dict:
    return {"offset_days": days, "en": en, "es": es}


_PLAYBOOK: dict[str, dict] = {
    "marriage": {"steps": [
        _s("profile", "Update the household: marital status and whether money is shared or kept apart.",
           "Actualiza el hogar: estado civil y si el dinero se comparte o se lleva por separado.", ["client.profile"]),
        _s("estate", "Confirm the marital regime (sociedad conyugal or separación de bienes) on the acta de matrimonio.",
           "Confirma el régimen matrimonial (sociedad conyugal o separación de bienes) en el acta de matrimonio.",
           ["client.profile"], ["protection_review"], "MX", "notario"),
        _s("cash_flow", "Combine or coordinate the budget: both incomes, shared spending and who pays what.",
           "Junta o coordina el presupuesto: ambos ingresos, gastos compartidos y quién paga qué.",
           ["income.<id>", "spending.monthly"], ["spending", "plan"]),
        _s("reserve", "Reset the emergency reserve to the household's essential spending.",
           "Ajusta el fondo de emergencia al gasto esencial del hogar.", ["reserve"], ["plan"]),
        _s("beneficiaries", "Name each other as beneficiaries where intended (AFORE/401k, insurance, accounts).",
           "Designa beneficiarios donde corresponda (AFORE/401k, seguros, cuentas).", [], ["protection_review"]),
        _s("insurance", "Size life and disability cover now that someone depends on your income.",
           "Dimensiona seguro de vida e invalidez ahora que alguien depende de tu ingreso.", ["client.profile"],
           ["protection_review"]),
        _s("tax", "Choose a filing status (married filing jointly or separately) and update the W-4.",
           "Elige cómo declarar (conjunta o separada) y actualiza el W-4.", ["tax.profile"], ["tax"], "US", "CPA"),
        _s("will", "Make or update wills for both of you.", "Hagan o actualicen el testamento de ambos.", [],
           ["protection_review"], "all", "notario / estate attorney"),
        _s("policy", "Redraft the investment policy for the joint goals.",
           "Rehaz la política de inversión con las metas conjuntas.", ["goals"], ["policy_draft"]),
    ], "nudges": [_n(30, "Beneficiary designations updated?", "¿Ya actualizaron beneficiarios?"),
                  _n(90, "First joint budget review.", "Primera revisión del presupuesto conjunto.")]},
    "birth_or_adoption": {"steps": [
        _s("health", "Add the child to the health plan (US plans usually allow it within 30 days; GMM policies have "
                     "newborn clauses).",
           "Da de alta al bebé en el seguro médico (en EE.UU. suele haber 30 días; las pólizas de GMM tienen cláusulas "
           "de recién nacido).", [], ["protection_review"]),
        _s("profile", "Update dependants and their ages.", "Actualiza dependientes y sus edades.", ["client.profile"]),
        _s("insurance", "Size life and disability cover for the new dependant.",
           "Dimensiona seguro de vida e invalidez para el nuevo dependiente.", [], ["protection_review"]),
        _s("will", "Name a guardian (tutor) in a will.", "Nombra un tutor en el testamento.", [], ["protection_review"],
           "all", "notario / estate attorney"),
        _s("beneficiaries", "Review beneficiaries (AFORE/401k, insurance, accounts).",
           "Revisa beneficiarios (AFORE/401k, seguros, cuentas).", [], ["protection_review"]),
        _s("cash_flow", "Add childcare and baby costs to spending and the reserve.",
           "Suma guardería y gastos del bebé al gasto y al fondo de emergencia.", ["spending.monthly", "reserve"],
           ["spending", "plan"]),
        _s("education", "Open an education goal (a 529 plan in the US).",
           "Abre una meta de educación (en EE.UU., un plan 529).", ["goals"], ["plan", "project"]),
        _s("tax", "Claim the child as a dependant (Child Tax Credit).",
           "Declara al hijo como dependiente (Child Tax Credit).", ["tax.profile"], ["tax"], "US", "CPA"),
    ], "nudges": [_n(14, "Is the baby on the health plan?", "¿Ya está el bebé en el seguro médico?"),
                  _n(60, "Guardian named in a will?", "¿Ya nombraste tutor en un testamento?"),
                  _n(365, "Education goal check.", "Revisión de la meta de educación.")]},
    "divorce": {"steps": [
        _s("legal", "Work with your own lawyer first; sign nothing about money until they have reviewed it.",
           "Primero tu propio abogado; no firmes nada sobre dinero sin su revisión.", [], [], "all", "abogado / attorney"),
        _s("inventory", "List every account, property and debt, with statements.",
           "Haz la lista de cuentas, bienes y deudas, con estados de cuenta.", ["account.<id>", "liability.<id>"],
           ["ledger"]),
        _s("estate", "The marital regime decides how property is divided.",
           "El régimen matrimonial decide cómo se reparten los bienes.", [], [], "MX", "abogado"),
        _s("retirement", "A retirement-plan split needs a QDRO; an IRA transfer must follow the decree.",
           "Dividir un plan de retiro requiere una QDRO; un traspaso de IRA debe seguir la sentencia.", [], [], "US",
           "attorney"),
        _s("cash_flow", "Rebuild the budget on one income and refill the reserve.",
           "Rehaz el presupuesto con un solo ingreso y rellena el fondo de emergencia.",
           ["income.<id>", "spending.monthly", "reserve"], ["spending", "plan"]),
        _s("credit", "Close or separate joint cards and loans.", "Cierra o separa tarjetas y créditos conjuntos.",
           ["liability.<id>"], ["debt_payoff"]),
        _s("beneficiaries", "Change beneficiaries once the decree allows it.",
           "Cambia beneficiarios cuando la sentencia lo permita.", [], ["protection_review"]),
        _s("will", "Write a new will.", "Haz un nuevo testamento.", [], ["protection_review"], "all",
           "notario / estate attorney"),
        _s("policy", "Redraft the investment policy for your own goals.",
           "Rehaz la política de inversión con tus propias metas.", ["goals"], ["policy_draft"]),
    ], "nudges": [_n(30, "Accounts and debts listed?", "¿Ya tienes la lista de cuentas y deudas?"),
                  _n(180, "Beneficiaries and will updated after the decree?",
                     "¿Beneficiarios y testamento actualizados tras la sentencia?")]},
    "death_in_family": {"steps": [
        _s("pause", "Make no large or irreversible money decisions in the first weeks.",
           "No tomes decisiones grandes o irreversibles en las primeras semanas.", []),
        _s("documents", "Get several certified copies of the death certificate (acta de defunción).",
           "Pide varias copias certificadas del acta de defunción.", []),
        _s("claims", "Claim life insurance and notify banks and brokers.",
           "Reclama los seguros de vida y avisa a bancos y casas de bolsa.", [], ["protection_review"]),
        _s("pension", "Ask the AFORE and IMSS about survivor benefits (pensión de viudez u orfandad).",
           "Pregunta en la AFORE y el IMSS por pensión de viudez u orfandad.", [], [], "MX"),
        _s("pension", "Ask Social Security about survivor benefits.",
           "Pregunta al Seguro Social por beneficios de sobreviviente.", [], [], "US"),
        _s("estate", "Open the succession: with a will before a notario; without one, an intestate succession.",
           "Inicia la sucesión: con testamento ante notario; sin él, una sucesión intestamentaria.", [], [], "MX",
           "notario"),
        _s("estate", "Probate and, if the estate is large, the estate return (due 9 months after death).",
           "Proceso sucesorio (probate) y, si el patrimonio es grande, la declaración de sucesión (9 meses).", [],
           [], "US", "estate attorney"),
        _s("us_situs", "US shares held by a non-resident can owe US estate tax; check the exposure.",
           "Acciones de EE.UU. de un no residente pueden causar impuesto sucesorio de EE.UU.; revisa la exposición.",
           [], ["estate"], "MX", "estate attorney"),
        _s("profile", "Update the household, income and beneficiaries.", "Actualiza hogar, ingresos y beneficiarios.",
           ["client.profile", "income.<id>"], ["protection_review"]),
    ], "nudges": [_n(30, "Insurance claims filed?", "¿Ya reclamaste los seguros?"),
                  _n(90, "Succession started?", "¿Ya inició la sucesión?"),
                  _n(270, "Estate return deadline if one applies (US).",
                     "Vence la declaración de sucesión si aplica (EE.UU.).")]},
    "job_change": {"steps": [
        _s("income", "Update income, pay dates and any signing bonus.",
           "Actualiza ingreso, fechas de pago y bono de contratación.", ["income.<id>"], ["calendar"]),
        _s("retirement", "Decide on the old 401(k): leave it, move it to the new plan or an IRA by direct rollover.",
           "Decide sobre el 401(k) anterior: dejarlo, pasarlo al nuevo plan o a una IRA por traspaso directo.", [],
           [], "US"),
        _s("retirement", "Your AFORE continues; check there is only one and that the new employer reports to it.",
           "Tu AFORE sigue; revisa que solo tengas una y que el nuevo patrón aporte a ella.", [], [], "MX"),
        _s("tax", "Check the finiquito for correct ISR withholding.", "Revisa la retención de ISR del finiquito.",
           [], ["mx_calendar"], "MX", "contador"),
        _s("benefits", "Group life, disability and medical cover end with the old job; check the gap.",
           "El seguro de vida, invalidez y gastos médicos del trabajo anterior termina; revisa el hueco.", [],
           ["protection_review"]),
        _s("equity", "Note any unvested equity you leave behind or receive.",
           "Anota acciones o bonos diferidos que dejas o recibes.", ["income.<id>"]),
        _s("plan", "Re-split the monthly surplus among reserve, debts and goals.",
           "Reparte de nuevo el excedente mensual entre fondo, deudas y metas.", ["spending.monthly"], ["plan"]),
    ], "nudges": [_n(30, "Benefits enrolled at the new job?", "¿Ya te inscribiste en las prestaciones nuevas?"),
                  _n(60, "Old retirement account decided?", "¿Ya decidiste qué hacer con el plan de retiro anterior?")]},
    "layoff": {"steps": [
        _s("runway", "Work out how many months essential spending the reserve covers; cut to essentials now.",
           "Calcula cuántos meses de gasto esencial cubre tu fondo; recorta a lo esencial desde ya.",
           ["spending.monthly", "reserve"], ["spending", "plan"]),
        _s("severance", "Check the finiquito or liquidación and its ISR; a dismissal dispute goes to PROFEDET.",
           "Revisa el finiquito o liquidación y su ISR; una disputa por despido va a PROFEDET.", [], [], "MX",
           "contador / PROFEDET"),
        _s("unemployment", "File for state unemployment benefits right away.",
           "Solicita el seguro de desempleo de tu estado de inmediato.", [], [], "US"),
        _s("health", "Keep health cover: COBRA or a marketplace plan (the election window is limited).",
           "Mantén tu seguro médico: COBRA o un plan del marketplace (el plazo para elegir es limitado).", [],
           ["protection_review"], "US"),
        _s("health", "Employer GMM ends; ask about converting to an individual policy without new waiting periods.",
           "El GMM de la empresa termina; pregunta por convertirlo a individual sin nuevos periodos de espera.", [],
           ["protection_review"], "MX"),
        _s("retirement", "Avoid cashing out retirement accounts; taxes and penalties take a large share.",
           "Evita retirar tus cuentas de retiro; impuestos y penalizaciones se llevan una parte grande.", [], []),
        _s("investing", "Pause new investing until income returns; long-term holdings are not the first source.",
           "Pausa nuevas inversiones hasta que vuelva el ingreso; lo invertido a largo plazo no es la primera fuente.",
           ["goals"], ["plan"]),
    ], "nudges": [_n(7, "Health cover arranged?", "¿Ya resolviste el seguro médico?"),
                  _n(30, "Budget on reduced income checked?", "¿Ya revisaste el presupuesto con menos ingreso?"),
                  _n(90, "Runway check.", "Revisión de cuántos meses te quedan de fondo.")]},
    "relocation": {"steps": [
        _s("tax_residence", "Confirm your tax residence after the move and from which date.",
           "Confirma tu residencia fiscal tras la mudanza y desde qué fecha.", ["client.profile"], [], "all",
           "contador / CPA"),
        _s("accounts", "Tell brokers and banks; some restrict non-residents.",
           "Avisa a bancos y casas de bolsa; algunos restringen a no residentes.", ["account.<id>"]),
        _s("tax", "Holdings can be taxed differently in the new country (Mexican funds for a US person, US-situs "
                  "assets for a Mexican resident).",
           "Tus inversiones pueden tributar distinto en el nuevo país (fondos mexicanos para una persona de EE.UU., "
           "activos con situs en EE.UU. para un residente en México).", [],
           ["mx_foreign", "estate", "asset_location"]),
        _s("health", "Arrange health cover in the new country before the old one lapses.",
           "Contrata cobertura médica en el nuevo país antes de que venza la anterior.", [], ["protection_review"]),
        _s("currency", "Set the reporting currency and the reserve's currency.",
           "Define la moneda de referencia y la del fondo de emergencia.", ["client.profile", "reserve"]),
        _s("will", "Check the will is valid where you now live and where your assets are.",
           "Revisa que tu testamento valga donde vives y donde están tus bienes.", [], ["protection_review"], "all",
           "notario / estate attorney"),
        _s("policy", "Redraft the investment policy (vehicles change with residence).",
           "Rehaz la política de inversión (los vehículos cambian con la residencia).", [], ["policy_draft"]),
    ], "nudges": [_n(30, "Tax residence confirmed?", "¿Confirmaste tu residencia fiscal?"),
                  _n(120, "Accounts and policy updated?", "¿Cuentas y política actualizadas?")]},
    "inheritance": {"steps": [
        _s("pause", "Keep it somewhere safe and liquid while you decide; there is no rush.",
           "Mantenlo en un lugar seguro y líquido mientras decides; no hay prisa.", ["cash.<id>"]),
        _s("insurance_limit", "Bank deposits are insured up to 400,000 UDIs per person per bank; spread a large sum.",
           "Los depósitos bancarios están protegidos hasta 400 mil UDIs por persona y por banco; reparte una suma "
           "grande.", [], [], "MX"),
        _s("tax", "Inheritances are exempt from ISR but must be reported in the annual return above the "
                  "disclosure threshold.",
           "Las herencias están exentas de ISR pero se informan en la declaración anual arriba del umbral.", [], [],
           "MX", "contador"),
        _s("tax", "Inherited assets usually get a stepped-up basis; inherited retirement accounts have withdrawal "
                  "deadlines.",
           "Los bienes heredados suelen recibir costo base ajustado; las cuentas de retiro heredadas tienen plazos "
           "de retiro.", [], ["tax"], "US", "CPA"),
        _s("us_situs", "US shares in the estate may owe US estate tax.",
           "Acciones de EE.UU. en la herencia pueden causar impuesto sucesorio de EE.UU.", [], ["estate"], "MX"),
        _s("plan", "Order the uses: reserve, expensive debt, near goals, then long-term investing.",
           "Ordena los usos: fondo de emergencia, deudas caras, metas cercanas y después inversión a largo plazo.",
           ["goals", "reserve"], ["plan", "debt_payoff"]),
        _s("policy", "Redraft the investment policy with the new capital.",
           "Rehaz la política de inversión con el nuevo capital.", [], ["policy_draft"]),
        _s("will", "Update your own will and beneficiaries.", "Actualiza tu testamento y beneficiarios.", [],
           ["protection_review"], "all", "notario / estate attorney"),
    ], "nudges": [_n(30, "Still parked safely? No decisions needed yet.", "¿Sigue en lugar seguro? Aún no hay que decidir."),
                  _n(90, "Plan the uses.", "Planea los usos."),
                  _n(180, "Policy and will updated?", "¿Política y testamento actualizados?")]},
    "home_purchase": {"steps": [
        _s("reserve", "Keep the emergency reserve separate from the down payment and closing costs.",
           "Mantén el fondo de emergencia aparte del enganche y los gastos de escrituración.", ["reserve", "goals"],
           ["plan"]),
        _s("mortgage", "Record the mortgage: balance, rate, payment and term.",
           "Registra la hipoteca: saldo, tasa, pago y plazo.", ["liability.<id>"], ["debt_payoff"]),
        _s("costs", "Add predial, maintenance and home insurance to spending (predial is due early in the year).",
           "Suma predial, mantenimiento y seguro de la casa al gasto (el predial se paga a inicio de año).",
           ["spending.monthly"], ["mx_calendar"], "MX"),
        _s("costs", "Add property tax, homeowner's insurance and maintenance to spending.",
           "Suma impuesto predial, seguro de vivienda y mantenimiento al gasto.", ["spending.monthly"], ["spending"],
           "US"),
        _s("insurance", "A mortgage raises the life-insurance need; check cover.",
           "Una hipoteca aumenta la necesidad de seguro de vida; revisa la cobertura.", [], ["protection_review"]),
        _s("will", "Update the will for the property.", "Actualiza el testamento para incluir el inmueble.", [],
           ["protection_review"], "all", "notario / estate attorney"),
    ], "nudges": [_n(30, "Mortgage recorded and spending updated?", "¿Hipoteca registrada y gasto actualizado?"),
                  _n(365, "Home insurance renewal.", "Renovación del seguro de la casa.")]},
    "retirement": {"steps": [
        _s("income", "Map income sources and dates: pension, AFORE, Social Security, withdrawals.",
           "Mapea fuentes de ingreso y fechas: pensión, AFORE, Seguro Social, retiros.", ["income.<id>"],
           ["income", "ladder", "calendar"]),
        _s("pension", "Confirm your IMSS regime (Ley 73 or Ley 97) and pension estimate before applying.",
           "Confirma tu régimen IMSS (Ley 73 o Ley 97) y la estimación de pensión antes de tramitarla.", [], [], "MX"),
        _s("pension", "Decide when to claim Social Security; each year of delay raises the benefit.",
           "Decide cuándo pedir el Seguro Social; cada año de espera aumenta el beneficio.", [], [], "US"),
        _s("health", "Enrol in Medicare around 65; missing the window can mean lasting penalties.",
           "Inscríbete en Medicare cerca de los 65; perder el plazo puede traer penalizaciones permanentes.", [],
           ["protection_review"], "US"),
        _s("health", "Check your GMM policy's age limits and renewal terms.",
           "Revisa los límites de edad y la renovación de tu póliza de GMM.", [], ["protection_review"], "MX"),
        _s("reserve", "Hold one to two years of spending outside risk assets.",
           "Ten de uno a dos años de gasto fuera de activos de riesgo.", ["reserve"], ["plan", "ladder"]),
        _s("policy", "Redraft the investment policy for withdrawals.",
           "Rehaz la política de inversión para la etapa de retiros.", ["goals"], ["policy_draft"]),
        _s("estate", "Review the will and beneficiaries.", "Revisa testamento y beneficiarios.", [],
           ["protection_review"], "all", "notario / estate attorney"),
    ], "nudges": [_n(30, "Income plan confirmed?", "¿Plan de ingresos confirmado?"),
                  _n(365, "Annual withdrawal review.", "Revisión anual de retiros.")]},
}


def life_event(kind: str, when: str | date | None = None, details: Mapping[str, Any] | None = None,
               situation: Mapping[str, Any] | None = None) -> dict:
    """The ordered checklist of reviews after a life event, in English and Mexican Spanish."""
    key = _ALIASES.get(str(kind or "").lower(), str(kind or "").lower())
    if key not in _PLAYBOOK:
        raise ValueError(f"kind must be one of {', '.join(EVENTS)}")
    details = dict(details or {})
    if when is None:
        start = date.fromisoformat((situation or {}).get("as_of") or datetime.now(timezone.utc).date().isoformat())
    elif isinstance(when, date):
        start = when
    else:
        try:
            start = date.fromisoformat(str(when)[:10])
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
    country = details.get("jurisdiction") if details.get("jurisdiction") in ("MX", "US") else _country(situation)
    steps = []
    for step in _PLAYBOOK[key]["steps"]:
        if country and step["jurisdiction"] not in ("all", country):
            continue
        steps.append({"order": len(steps) + 1, **step})
    nudges = [{"date": (start + timedelta(days=n["offset_days"])).isoformat(), "en": n["en"], "es": n["es"]}
              for n in _PLAYBOOK[key]["nudges"]]
    facts = sorted({f for s in steps for f in s["facts"]})
    tasks = list(dict.fromkeys(t for s in steps for t in s["tasks"]))
    return {"kind": key, "date": start.isoformat(), "jurisdiction": country,
            "jurisdiction_note": None if country else "Residence unknown: steps for Mexico and the US are both listed.",
            "steps": steps, "facts_to_update": facts, "tasks_to_run": tasks, "nudges": nudges,
            "details": details or None, "trade_call": None}


# ------------------------------------------------------------------ service tasks

TASKS = ("protection_review", "life_event")


def _saved_estate(saved: Mapping[str, Any]) -> dict:
    """The checklist's ``estate`` answers from the saved estate.* facts (what the person told us)."""
    out: dict[str, Any] = {}
    will = saved.get("estate.will")
    if isinstance(will, Mapping) and isinstance(will.get("exists"), bool):
        out["will"] = will["exists"]
        if will.get("date"):
            out["will_date"] = will["date"]
    family = saved.get("estate.family")
    if isinstance(family, Mapping):
        status = family.get("marital_status")
        if status in ("married", "free_union"):
            out["married"] = True
        elif status in ("single", "divorced", "widowed"):
            out["married"] = False
        if family.get("marital_regime"):
            out["marital_regime"] = family["marital_regime"]
    return out


def run_task(task: str, inputs: Mapping[str, Any], situation: Mapping[str, Any],
             saved_estate: Mapping[str, Any] | None = None) -> dict:
    if task not in TASKS:
        raise ValueError(f"protection tasks are {', '.join(TASKS)}")
    inputs = dict(inputs)
    if task == "protection_review":
        allowed = {"policies", "life", "disability", "health", "estate", "jurisdiction"}
        unknown = sorted(set(inputs) - allowed)
        if unknown:
            raise ValueError(f"protection_review inputs: unknown {unknown}; expected {{policies?, life?, disability?, "
                             "health?, estate?, jurisdiction?, facts?, as_of?}")
        saved_estate = saved_estate or {}
        stated = _saved_estate(saved_estate)
        if stated:
            inputs["estate"] = {**stated, **dict(inputs.get("estate") or {})}
        result = protection_review(situation, inputs)
        profile = situation.get("profile") or {}
        family_known = bool(saved_estate) or bool(profile.get("dependents")) or bool(profile.get("dependent_ages"))
        result["estate"]["next_step"] = (
            {"task": "estate_register",
             "en": "Family or estate facts are saved: run wealth_run(task=estate_register) with the client_id for who "
                   "would receive each account, the intestate split and the gaps, before answering about the estate.",
             "es": "Hay datos de familia o sucesión guardados: corre estate_register para ver quién recibiría cada "
                   "cuenta, el reparto intestamentario y los huecos."}
            if family_known else
            {"task": "estate_register",
             "en": "Ask who is in the family (spouse, marital regime, children) and whether there is a will, save it "
                   "(estate.family, estate.will), then run estate_register for who would receive each account.",
             "es": "Pregunta por la familia (cónyuge, régimen, hijos) y si hay testamento, guárdalo y corre "
                   "estate_register."})
        status = "partial" if result["missing"] else "ready"
        sources = [_src(k) for k in ("life_method", "disability_replacement", "hsa_limit_2026", "mes_del_testamento",
                                     "mx_gmm_premium_deductible")]
        return {"status": status, "result": result, "missing": result["missing"], "warnings": [],
                "sources": sources,
                "assumptions": ["Life need uses a 60-80% income-replacement range and support until the youngest "
                                "dependant is 22-25 unless stated; these are Wealth's planning conventions.",
                                "No product is recommended; coverage is bought through a licensed agent."]}
    allowed = {"kind", "date", "details", "jurisdiction"}
    unknown = sorted(set(inputs) - allowed)
    if unknown or "kind" not in inputs:
        raise ValueError("life_event inputs: " + (f"unknown {unknown}" if unknown else "missing kind")
                         + f"; expected {{kind: {'|'.join(EVENTS)}, date?, details?, jurisdiction?}}")
    details = dict(inputs.get("details") or {})
    if inputs.get("jurisdiction"):
        details["jurisdiction"] = inputs["jurisdiction"]
    result = life_event(inputs["kind"], inputs.get("date"), details, situation)
    return {"status": "ready", "result": result, "missing": [], "warnings": [], "sources": [],
            "assumptions": ["A checklist of reviews, not legal or tax advice; referrals are named on each step."]}


__all__ = ["EVENTS", "PARAMETERS", "TASKS", "disability_gap", "estate_checklist", "health_review", "life_event",
           "life_need", "protection_review", "run_task"]
