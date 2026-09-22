"""Estate and beneficiary register: what happens to each account at death.

:func:`register` reads the canonical picture (``wealth.situation.build``) and
the facts that describe the estate (``estate.designation.<slug>`` joined to
``cash.<id>``, ``investment.<id>``, ``insurance.<id>``, ``property.<id>`` or a
statement's ``account.<id>``; ``estate.will``, ``estate.guardianship``,
``estate.family``) and returns, for the person's own death:

* ``rows``: one per account, policy or property, with the mechanism that moves
  it (a beneficiary designation that skips the juicio sucesorio or probate, a
  trust, survivorship on a US joint account, the will, or intestate
  succession), who receives it and an estimated amount per heir;
* ``heirs``: those amounts summed per person;
* ``gaps``: problems ranked by the amount at risk (no beneficiary, shares that
  do not add to 100%, a minor named directly without a guardian or trust, a
  predeceased or ex-spouse beneficiary, an old designation or one that
  predates a marriage or a child, US-situs assets over US$60,000 for a
  non-resident alien via :mod:`wealth.estate`, AFORE beneficiaries missing, no
  will, a will older than a marriage or child, ERISA spousal consent);
* ``questions``: what is unknown and worth asking (unknown is never "none");
* ``completeness``: a 0-100 score with its parts.

Designations live in their own ``estate.designation.<slug>`` facts so that
naming a beneficiary never re-dates a balance; beneficiaries saved inline on
an account by older writes are still read, and the designation fact wins.

Amounts are ranges when the facts that decide them are unknown: with an
unknown marital regime (sociedad conyugal keeps half of the gananciales out
of the estate) and, in a Mexican intestate succession with descendants, with
unknown spouse's assets (CCF Arts. 1624-1625: the spouse takes a child's share
only if they lack property, or what equals it); with no descendants, with
unknown surviving parents or siblings (CCF Arts. 1626-1629, UPC 2-102(2)); and
for an AFORE, when survivors could draw an IMSS pension that the balance
funds instead of a lump sum (LSS Arts. 64 and 193).

Everything is an estimate of how the rules usually work, never legal advice
or drafting: a notario (Mexico) or estate attorney (US) confirms it.  The
caveats and statutes live in ``assumptions`` and ``sources`` only.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from .situation.model import D, humanize, institution_key, num, same_institution
from .situation.schema import SchemaError, designation_key, validate

LIC_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LIC.pdf"
LMV_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LMV.pdf"
LSAR_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LSAR.pdf"
LSS_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LSS.pdf"
LFT_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LFT.pdf"
CCF_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/CCF.pdf"
LSCS_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LSCS.pdf"
MES_TESTAMENTO_URL = "https://colegiodenotarios.org.mx/septiembre-mes-testamento"
SECURE_URL = "https://www.law.cornell.edu/uscode/text/26/401"
IRS_RMD_URL = "https://www.irs.gov/retirement-plans/retirement-plan-and-ira-required-minimum-distributions-faqs"
ERISA_URL = "https://www.law.cornell.edu/uscode/text/29/1055"
UPC_URL = "https://www.uniformlaws.org/committees/community-home?CommunityKey=a539920d-c477-44b8-84fe-b0d7b1a4cca8"
EGELHOFF_URL = "https://supreme.justia.com/cases/federal/us/532/141/"

CHECKED_ON = "2026-09-22"

SOURCES: dict[str, dict[str, Any]] = {
    "lic_56": {"title": "Ley de Instituciones de Crédito, Art. 56 (beneficiarios de depósitos y custodia de valores; "
                        "sin tope desde la reforma DOF 2009-03-23)", "url": LIC_URL, "status": "verified"},
    "lmv_201": {"title": "Ley del Mercado de Valores, Art. 201 (beneficiarios en los contratos de las casas de bolsa "
                         "con su clientela; sin beneficiarios, legislación común; reforma DOF 2014-01-10)",
                "url": LMV_URL, "status": "verified"},
    "lss_193": {"title": "Ley del Seguro Social, Art. 193 (beneficiarios de la cuenta individual AFORE: reciben lo que "
                         "puede entregarse en una sola exhibición; reforma DOF 2020-12-16), Art. 64 (el saldo integra "
                         "el monto constitutivo de la pensión de los beneficiarios; el excedente puede retirarse) and "
                         "LFT Art. 501 (orden a falta de designación)", "url": LSS_URL,
                "status": "verified", "also": LFT_URL},
    "lsar": {"title": "Ley de los Sistemas de Ahorro para el Retiro (cuentas individuales; reclamaciones ante CONSAR)",
             "url": LSAR_URL, "status": "verified"},
    "lscs": {"title": "Ley sobre el Contrato de Seguro, Arts. 163-165 (beneficiarios del seguro de vida)",
             "url": LSCS_URL, "status": "needs_verification"},
    "ccf_intestate": {"title": "Código Civil Federal, Arts. 1599-1637 (sucesión legítima: descendientes, cónyuge o "
                               "concubino, ascendientes, colaterales, Beneficencia Pública; 1626 cónyuge y ascendientes "
                               "por mitad, 1627 cónyuge dos tercios y hermanos un tercio, 1635 concubinato) and Art. 425 "
                               "(patria potestad: administración de los bienes del menor); each state has its own "
                               "civil code", "url": CCF_URL, "status": "verified"},
    "mes_testamento": {"title": "Colegio Nacional del Notariado Mexicano / SEGOB, Septiembre Mes del Testamento",
                       "url": MES_TESTAMENTO_URL, "status": "verified"},
    "secure": {"title": "SECURE Act (Pub. L. 116-94, Sec. 401): 10-year rule for designated beneficiaries, IRC "
                        "401(a)(9)(H); eligible designated beneficiaries excepted", "url": SECURE_URL,
               "status": "verified"},
    "irs_rmd": {"title": "IRS, Retirement plan and IRA required minimum distributions FAQs", "url": IRS_RMD_URL,
                "status": "verified"},
    "erisa": {"title": "ERISA Sec. 205 (29 U.S.C. 1055) and IRC 401(a)(11): the spouse is the 401(k) beneficiary "
                       "unless they consent in writing, witnessed by a notary or plan representative",
              "url": ERISA_URL, "status": "verified"},
    "egelhoff": {"title": "Egelhoff v. Egelhoff, 532 U.S. 141 (2001): ERISA preempts state revocation-on-divorce "
                          "for plan designations", "url": EGELHOFF_URL, "status": "verified"},
    "upc": {"title": "Uniform Probate Code 2-102/2-103 (intestate shares: 2-102(2) spouse with a parent takes the "
                     "first $300,000 plus three-fourths of the balance; many states differ) and 2-804 (revocation "
                     "on divorce)", "url": UPC_URL, "status": "verified"},
}

DESIGNATION_REVIEW_YEARS = 5
"""A designation or will older than this is due for review (the same 5-year rule as the estate checklist)."""
US_SITUS_THRESHOLD_USD = Decimal(60000)
UPC_SPOUSE_FIRST_USD = Decimal(300000)
"""UPC 2-102(2): with no descendant and a surviving parent, the spouse takes the first $300,000 plus 3/4 of the rest."""
ADULT_AGE = 18
SHARE_TOLERANCE = Decimal("0.005")

# Weights of the completeness score; parts that do not apply are left out and the rest rescaled.
SCORE_WEIGHTS = {"designations": 60, "will": 30, "guardian": 10}

GAP_CODES = ("no_beneficiary", "afore_beneficiaries", "shares_not_100", "minor_direct", "beneficiary_predeceased",
             "beneficiary_ex_spouse", "designation_old", "designation_before_event", "erisa_spousal_consent",
             "us_situs_nra", "no_will", "will_before_event", "will_old", "no_guardian")
_MARITAL_PRODUCTS = frozenset({"bank", "broker", "investment", "ppr", "property"})
REVIEW_GAPS = frozenset({"designation_old", "designation_before_event"})
FACT_PREFIXES = ("cash.", "investment.", "insurance.", "property.", "estate.", "client.profile", "account.")

_US_PLANS = {"401k", "403b", "457b", "ira", "roth_ira", "sep_ira", "simple_ira"}
_ERISA_PLANS = {"401k", "403b"}  # governmental 457(b) plans are outside ERISA; church plans may be too
_MX_BROKERS = ("gbm", "actinver", "monex", "vector", "kuspit", "bursanet", "finamex", "intercam", "valmex",
               "casa de bolsa", "hey inversion", "flink", "cetesdirecto", "fintual")


# ------------------------------------------------------------------ small helpers


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _age(birth: date | None, birth_year: Any, today: date) -> int | None:
    if birth:
        return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    if isinstance(birth_year, int) and not isinstance(birth_year, bool):
        return today.year - birth_year
    return None


def _fold(text: Any) -> str:
    return " ".join(str(text or "").lower().replace(".", " ").split())


def _short(amount: Decimal | None, currency: str | None) -> str:
    """$217k, $1.2M: the way a nudge says an amount."""
    if amount is None:
        return "?"
    value = abs(amount)
    if value >= 1_000_000:
        text = f"${value / 1_000_000:.1f}M".replace(".0M", "M")
    elif value >= 1_000:
        text = f"${value / 1_000:.0f}k"
    else:
        text = f"${value:,.0f}"
    return text if currency in (None, "MXN", "USD") else f"{text} {currency}"


def _money(value: Decimal | None) -> Any:
    return num(value) if value is not None else None


class _Converter:
    """The picture's own FX rows (``situation.fx``), both directions; unknown stays unknown."""

    def __init__(self, sit: Mapping[str, Any]):
        self.to = sit.get("currency")
        self.rates: dict[tuple[str, str], Decimal] = {}
        for row in sit.get("fx") or []:
            pair, rate = str(row.get("pair") or ""), D(row.get("rate"))
            if "/" in pair and rate:
                base, quote = pair.split("/", 1)
                self.rates[(base, quote)] = rate

    def __call__(self, amount: Decimal | None, currency: str | None, to: str | None = None) -> Decimal | None:
        to = to or self.to
        if amount is None or not currency or not to:
            return None
        if currency == to:
            return amount
        if (currency, to) in self.rates:
            return amount * self.rates[(currency, to)]
        if (to, currency) in self.rates:
            return amount / self.rates[(to, currency)]
        return None


def _facts(snapshot: Mapping[str, Any]) -> dict[str, dict]:
    """Active facts by key, stale or not: a designation does not expire with the balance it sits on."""
    out: dict[str, dict] = {}
    for fact in (snapshot or {}).get("facts") or []:
        if not isinstance(fact, dict) or not isinstance(fact.get("key"), str):
            continue
        if fact.get("status", "active") != "active" or (fact.get("source") or {}).get("kind") == "pattern":
            continue
        if fact.get("confidence") == "inferred":
            continue  # an interpretation is not a designation
        out[fact["key"]] = fact
    return out


# ------------------------------------------------------------------ people


class _Family:
    def __init__(self, facts: dict[str, dict], sit: Mapping[str, Any], today: date):
        raw = (facts.get("estate.family") or {}).get("value")
        self.raw = raw if isinstance(raw, dict) else {}
        self.known = bool(self.raw)
        self.today = today
        status = self.raw.get("marital_status")
        self.status = status
        self.married = status in ("married", "free_union") if status else None
        self.spouse = self.raw.get("spouse") if isinstance(self.raw.get("spouse"), str) else None
        self.regime = self.raw.get("marital_regime")
        # True: sociedad conyugal / community property; False: separate; None: unknown.
        self.common = {"sociedad_conyugal": True, "community_property": True, "separacion_de_bienes": False,
                       "separate_property": False}.get(self.regime)
        if status == "free_union":
            self.common = False
        assets = self.raw.get("spouse_assets")
        self.spouse_assets = assets if isinstance(assets, dict) else None
        self.marriage = _date(self.raw.get("marriage_date"))
        self.divorce = _date(self.raw.get("divorce_date"))
        self.ex = {_fold(n) for n in self.raw.get("ex_spouses") or [] if isinstance(n, str)}
        self.deceased = {_fold(n) for n in self.raw.get("deceased") or [] if isinstance(n, str)}
        self.parents = self.raw.get("parents_living") if isinstance(self.raw.get("parents_living"), int) else None
        siblings = self.raw.get("siblings_living")
        self.siblings = siblings if isinstance(siblings, int) and not isinstance(siblings, bool) else None
        # Concubinato (CCF Art. 1635) has no marital regime: nothing is shared by law.
        self.free_union = status == "free_union"
        self.children: list[dict] = []
        for index, child in enumerate(self.raw.get("children") or []):
            if isinstance(child, dict):
                born = _date(child.get("birth_date"))
                name = child.get("name") if isinstance(child.get("name"), str) and child["name"].strip() else None
                # A child saved without a name ({"minor": true} or a birth year) still counts as an heir.
                self.children.append({"name": name or f"Hijo/a {index + 1}", "named": name is not None,
                                      "born": born, "age": _age(born, child.get("birth_year"), today),
                                      "minor": child.get("minor"),
                                      "born_year": born.year if born else child.get("birth_year")})
        profile = sit.get("profile") or {}
        ages = [a for a in profile.get("dependent_ages") or [] if isinstance(a, int)]
        self.children_known = bool(self.children) or "children" in self.raw
        self.minor_children = [c for c in self.children
                               if (c["age"] is not None and c["age"] < ADULT_AGE)
                               or (c["age"] is None and c["minor"] is True)]
        # Dependants under 18 from the profile count as minors when the family names no children.
        self.minors = len(self.minor_children) if self.children_known else sum(1 for a in ages if a < ADULT_AGE)
        self.minors_known = self.children_known or bool(ages) or profile.get("dependents") == 0

    def events(self) -> list[tuple[date, str, str]]:
        """Dated life events a designation or will should postdate: (date, code, name)."""
        out = []
        if self.marriage:
            out.append((self.marriage, "marriage", self.spouse or ""))
        if self.divorce:
            out.append((self.divorce, "divorce", ""))
        for child in self.children:
            if child["born"]:
                out.append((child["born"], "child", child["name"]))
            elif isinstance(child["born_year"], int):
                # A year alone: January 1, so a designation made that same year is never called older.
                out.append((date(child["born_year"], 1, 1), "child", child["name"]))
        return sorted(out)

    def child_age(self, name: str) -> int | None:
        for child in self.children:
            if _fold(child["name"]) == _fold(name):
                if child["age"] is None and child["minor"] is True:
                    return 0  # said to be a minor, age not given
                return child["age"]
        return None


# ------------------------------------------------------------------ rows


_US_CUSTODIANS = frozenset({"schwab", "ibkr", "fidelity", "vanguard"})  # situation.model institution aliases


def _country(value: Mapping[str, Any], row: Mapping[str, Any], residence: str | None) -> tuple[str | None, str | None]:
    """Where the account is and why we think so."""
    stated = value.get("country")
    if isinstance(stated, str) and len(stated) == 2:
        return stated.upper(), None
    plan = value.get("plan_type")
    kind = _fold(value.get("kind") or row.get("type"))
    if plan in ("afore", "ppr") or "afore" in kind or "ppr" in kind:
        return "MX", None
    if plan in _US_PLANS or any(w in kind for w in ("401", "ira", "403b")):
        return "US", None
    currency = value.get("currency") or row.get("currency")
    if currency == "MXN":
        return "MX", "assumed from the MXN currency"
    if residence:
        return residence, f"assumed from residence ({residence})"
    if currency == "USD":
        return "US", "assumed from the USD currency"
    return None, None


def _product(key: str, value: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """bank | broker | afore | ppr | us_retirement | insurance | property | investment."""
    head = key.split(".", 1)[0]
    if head == "insurance":
        return "insurance"
    if head == "property":
        return "property"
    plan = value.get("plan_type")
    kind = _fold(value.get("kind") or row.get("type") or row.get("kind"))
    if plan == "afore" or "afore" in kind:
        return "afore"
    if plan == "ppr" or "ppr" in kind:
        return "ppr"
    if plan in _US_PLANS or any(w in kind for w in ("401", "ira", "403b", "457")):
        return "us_retirement"
    if head == "cash" or kind in ("bank", "checking", "savings", "deposit", "cash"):
        return "bank"
    institution = _fold(value.get("institution") or row.get("institution"))
    if kind in ("brokerage", "taxable", "fund") or any(b in institution for b in _MX_BROKERS):
        return "broker"
    return "investment"


def _label(value: Mapping[str, Any], row: Mapping[str, Any], key: str) -> str:
    for name in (value.get("institution"), row.get("institution"), value.get("insurer"), value.get("name"),
                 row.get("label"), row.get("name")):
        if isinstance(name, str) and name.strip():
            return name.strip()
    return humanize(key.split(".", 1)[-1])


def _accounts(sit: Mapping[str, Any], facts: dict[str, dict], convert: _Converter) -> tuple[list[dict], list[str]]:
    """One entry per thing that passes at death: stated accounts (valued by their statement when one covers
    them), statement accounts nobody described, policies and property."""
    notes: list[str] = []
    statements = {a["key"]: a for a in sit.get("accounts") or [] if a.get("key")}
    used_statements: set[str] = set()
    out: list[dict] = []
    for row in (*(sit.get("cash") or []), *(sit.get("investments") or [])):
        key = row.get("key")
        if not isinstance(key, str) or not key.startswith(("cash.", "investment.")):
            continue
        fact = facts.get(key) or {}
        value = fact.get("value") if isinstance(fact.get("value"), dict) else {}
        amount = D(row.get("value"))
        covered = [statements[k] for k in row.get("covered_by") or [] if k in statements]
        if covered:
            used_statements.update(a["key"] for a in covered)
            parts = [D(a.get("value")) for a in covered]
            amount = sum(parts, Decimal(0)) if None not in parts else None
        out.append({"key": key, "value": value, "row": row, "amount": amount,
                    "statement_keys": [a["key"] for a in covered]})
    # Stale stated balances are out of the picture's rows, yet their designations still count.
    listed = {e["key"] for e in out}
    for key, fact in facts.items():
        if key.startswith(("cash.", "investment.")) and key.count(".") == 1 and key not in listed \
                and isinstance(fact.get("value"), dict):
            out.append({"key": key, "value": fact["value"], "row": {}, "amount": None, "statement_keys": [],
                        "stale": True,
                        "estimate": convert(D(fact["value"].get("amount")), fact["value"].get("currency"))})
    for account in sit.get("accounts") or []:
        if account.get("key") in used_statements or account.get("duplicate_of") or account.get("superseded_by"):
            continue
        if (account.get("type") or "").lower() in ("credit_card", "loan", "mortgage", "line_of_credit"):
            continue
        out.append({"key": account["key"], "value": {}, "row": account, "amount": D(account.get("value")),
                    "statement_keys": [account["key"]], "statement_only": True})
    for key, fact in sorted(facts.items()):
        value = fact.get("value")
        if not isinstance(value, dict) or key.count(".") != 1:
            continue
        if key.startswith("insurance."):
            if value.get("kind") not in ("life", "accident"):
                continue  # disability cover pays the living, not heirs
            amount = convert(D(value.get("coverage")), value.get("currency"))
            if value.get("coverage") is not None and amount is None:
                notes.append(f"{key}: no exchange rate for {value.get('currency')}; its amount is unknown here.")
            out.append({"key": key, "value": value, "row": {}, "amount": amount, "statement_keys": []})
        elif key.startswith("property."):
            amount = convert(D(value.get("value")), value.get("currency"))
            if value.get("value") is not None and amount is None:
                notes.append(f"{key}: no exchange rate for {value.get('currency')}; its value is unknown here.")
            out.append({"key": key, "value": value, "row": {}, "amount": amount, "statement_keys": []})
    # Designations are their own facts; they override any legacy inline fields on the account.
    designations = {}
    for key, fact in sorted(facts.items()):
        value = fact.get("value")
        if key.startswith("estate.designation.") and isinstance(value, dict) and isinstance(value.get("account"), str):
            designations[value["account"]] = (key, {k: v for k, v in value.items() if k not in ("account", "note")})
    for entry in out:
        found = designations.pop(entry["key"], None)
        if found:
            entry["designation_key"] = found[0]
            entry["value"] = {**entry["value"], **found[1]}
    for account, (key, value) in list(designations.items()):
        # "investment.gbm" said in conversation, while GBM's statement is saved as account.gbm-7832: the one
        # statement account at that institution is the account the person named.
        named = account.split(".", 1)[-1].replace("-", " ").replace("_", " ")
        matches = [e for e in out if e.get("statement_only") and not e.get("designation_key")
                   and same_institution(named, (e.get("row") or {}).get("institution"))]
        if len(matches) == 1:
            designations.pop(account)
            matches[0]["designation_key"] = key
            matches[0]["value"] = {**matches[0]["value"], **value}
    for account, (key, value) in designations.items():
        notes.append(f"{key} names {account}, which is not in the picture (forgotten, or a typo); it is listed "
                     "without an amount.")
        out.append({"key": account, "value": value, "row": {}, "amount": None, "statement_keys": [],
                    "designation_key": key, "orphan": True})
    return out, notes


# ------------------------------------------------------------------ succession rules


def _intestate(country: str | None, family: _Family, *, lft: bool = False,
               asks: list[str] | None = None) -> tuple[list[dict], str | None]:
    """Heirs by law as ``[{name, relationship, share}]`` and the rule applied, or ([], None) when unknown.

    In Mexico a spouse with descendants gets ``share: None`` and ``art_1624``: the share depends on the
    spouse's own property and is set per scenario (see :func:`_spouse_fraction`).  ``lft`` is the LFT
    Art. 501 order for an AFORE without designation (spouse and children alike, approximated).

    Unknown parents or siblings are never "none": the heirs they could displace get a ``share_range``
    (low, high) and the field to ask is appended to ``asks``.  A US spouse next to a parent gets
    ``upc: "spouse"`` (UPC 2-102(2): the first $300,000 plus three-fourths of the balance), settled on the
    whole intestate mass in :func:`_settle_heirs`."""
    asks = asks if asks is not None else []
    living_children = [c for c in family.children if _fold(c["name"]) not in family.deceased]
    spouse = family.married
    spouse_name = family.spouse or ("cónyuge" if country == "MX" else "spouse")
    if not family.known or spouse is None or not family.children_known:
        return [], None
    parents, siblings = family.parents, family.siblings
    heirs: list[dict] = []

    def ask(field: str) -> None:
        if field not in asks:
            asks.append(field)

    if country == "US":
        if spouse and living_children:
            heirs = [{"name": spouse_name, "relationship": "spouse", "share": Decimal(1)}]
            rule = "UPC 2-102(1): a spouse takes all when every descendant is also the spouse's (assumed)"
        elif spouse:
            if parents == 0:
                heirs = [{"name": spouse_name, "relationship": "spouse", "share": Decimal(1)}]
                rule = "UPC 2-102(1)(A): the spouse takes all, with no descendant or parent surviving"
            else:
                heirs = [{"name": spouse_name, "relationship": "spouse", "share": None, "upc": "spouse",
                          "parents_unknown": parents is None},
                         {"name": "parents", "relationship": "parent", "share": None, "upc": "parent",
                          "parents_unknown": parents is None}]
                rule = ("UPC 2-102(2): the spouse takes the first $300,000 plus three-fourths of the balance; the "
                        "parents the rest")
                if parents is None:
                    ask("estate.family.parents_living")
                    rule += " (whether a parent survives is unknown: with none, the spouse takes all)"
        elif living_children:
            share = Decimal(1) / len(living_children)
            heirs = [{"name": c["name"], "relationship": "child", "share": share} for c in living_children]
            rule = "UPC 2-103(a)(1): descendants equally"
        elif parents:
            heirs = [{"name": "parents", "relationship": "parent", "share": Decimal(1)}]
            rule = "UPC 2-103(a)(2): parents"
        elif parents == 0 and siblings:
            heirs = [{"name": "siblings", "relationship": "sibling", "share": Decimal(1)}]
            rule = "UPC 2-103(a)(3): descendants of the parents (siblings, by representation)"
        else:
            if parents is None or siblings is None:
                ask("estate.family.parents_living" if parents is None else "estate.family.siblings_living")
            return [], None
        return heirs, rule
    # Mexico (Código Civil Federal; state codes follow the same order)
    if living_children and spouse and not lft:
        heirs = [{"name": c["name"], "relationship": "child", "share": None, "art_1624": "child"}
                 for c in living_children]
        heirs.append({"name": spouse_name, "relationship": "spouse", "share": None, "art_1624": "spouse"})
        rule = ("CCF Arts. 1607, 1624-1625: children equally; the spouse or concubino takes a child's share only "
                "if they lack property of their own, or what brings theirs up to a child's share")
    elif living_children:
        count = len(living_children) + (1 if spouse else 0)
        share = Decimal(1) / count
        heirs = [{"name": c["name"], "relationship": "child", "share": share} for c in living_children]
        if spouse:
            heirs.append({"name": spouse_name, "relationship": "spouse", "share": share})
        rule = "LFT Art. 501 (approximated as equal parts)" if spouse else "CCF Art. 1607: children equally"
    elif spouse and lft:
        # LFT Art. 501: dependent parents concur with the spouse; siblings take nothing from an AFORE.
        heirs = [{"name": spouse_name, "relationship": "spouse", "share": Decimal("0.5") if parents else Decimal(1)}]
        if parents:
            heirs.append({"name": "padres", "relationship": "parent", "share": Decimal("0.5")})
        rule = "LFT Art. 501 (dependent parents concur with the spouse; approximated)"
    elif spouse and parents:
        heirs = [{"name": spouse_name, "relationship": "spouse", "share": Decimal("0.5")},
                 {"name": "padres", "relationship": "parent", "share": Decimal("0.5")}]
        rule = "CCF Arts. 1626, 1628: half to the spouse, half to the parents, whatever the spouse owns"
    elif spouse and parents == 0 and siblings:
        heirs = [{"name": spouse_name, "relationship": "spouse", "share": Decimal(2) / 3},
                 {"name": "hermanos", "relationship": "sibling", "share": Decimal(1) / 3}]
        rule = "CCF Art. 1627: two thirds to the spouse, one third to the siblings"
    elif spouse and parents == 0 and siblings == 0:
        heirs = [{"name": spouse_name, "relationship": "spouse", "share": Decimal(1)}]
        rule = "CCF Art. 1629: the spouse, with no descendants, ascendants or siblings"
    elif spouse and parents == 0:
        ask("estate.family.siblings_living")
        heirs = [{"name": spouse_name, "relationship": "spouse", "share": None,
                  "share_range": (Decimal(2) / 3, Decimal(1))},
                 {"name": "hermanos", "relationship": "sibling", "share": None,
                  "share_range": (Decimal(0), Decimal(1) / 3)}]
        rule = ("CCF Arts. 1627, 1629: two thirds to the spouse and one third to the siblings, or all to the "
                "spouse with no siblings (whether siblings survive is unknown)")
    elif spouse:
        # Parents unknown: half to them (Art. 1626), or, with none, siblings a third (1627) or nothing (1629).
        ask("estate.family.parents_living")
        heirs = [{"name": spouse_name, "relationship": "spouse", "share": None,
                  "share_range": (Decimal("0.5"), Decimal(1))},
                 {"name": "padres", "relationship": "parent", "share": None,
                  "share_range": (Decimal(0), Decimal("0.5"))}]
        if siblings != 0:
            if siblings is None:
                ask("estate.family.siblings_living")
            heirs.append({"name": "hermanos", "relationship": "sibling", "share": None,
                          "share_range": (Decimal(0), Decimal(1) / 3)})
        rule = ("CCF Arts. 1626-1629: half to the spouse and half to the parents; with no parents, two thirds "
                "to the spouse and a third to the siblings; with neither, all to the spouse (who survives is "
                "unknown)")
    elif lft:
        return [], None
    elif parents:
        heirs = [{"name": "padres", "relationship": "parent", "share": Decimal(1)}]
        rule = "CCF Art. 1615: the parents equally"
    elif parents == 0 and siblings:
        heirs = [{"name": "hermanos", "relationship": "sibling", "share": Decimal(1)}]
        rule = "CCF Arts. 1630-1631: the siblings equally (half-siblings half a share)"
    else:
        if parents is None or siblings is None:
            ask("estate.family.parents_living" if parents is None else "estate.family.siblings_living")
        return [], None  # known to be neither: collaterals or the Beneficencia Pública, not modeled
    return heirs, rule


def _pension_survivors(family: _Family) -> bool | None:
    """Whether someone could draw an IMSS survivors' pension (LSS Arts. 84 fr. III-IX, 130-137): a spouse or
    concubine, a child under 16 (25 if studying) or a dependent parent.  None when the family is unknown."""
    if not family.known or family.married is None:
        return None
    if family.married and not (family.spouse and _fold(family.spouse) in family.deceased):
        return True
    ages = [c["age"] for c in family.children if _fold(c["name"]) not in family.deceased]
    if any(a is None or a < 25 for a in ages):
        return True
    if family.parents:
        return True
    if family.children_known and family.parents == 0:
        return False
    return None


def _spouse_fraction(mass: Decimal, children: int, spouse_assets: Decimal | None) -> Decimal:
    """CCF Arts. 1624-1625: the part of an intestate mass the spouse takes next to ``children`` descendants.

    A child's portion is p = (mass - x) / children; the spouse gets x = p - assets when that is positive
    (x = p, a child's share, when they own nothing).  Solving: x = (mass - children * assets) / (children + 1).
    ``spouse_assets`` None means unlimited (they own at least a child's portion): nothing."""
    if spouse_assets is None or mass <= 0:
        return Decimal(0) if spouse_assets is None else Decimal(1) / (children + 1)
    x = (mass - children * spouse_assets) / (children + 1)
    return min(max(x, Decimal(0)), mass / (children + 1)) / mass


def _will_heirs(will: Mapping[str, Any]) -> tuple[list[dict], list[str], str | None]:
    """The will's heirs with their stated shares; (heirs, heirs whose share is unknown, warning).

    A stated share is kept as stated.  An heir without one is never given an equal part: the rest
    (100% less the stated shares) is split among the unknown ones only as a range, from nothing to all
    of that rest; when the stated shares already reach or pass 100% it is simply unknown."""
    heirs = [h for h in will.get("heirs") or [] if isinstance(h, dict) and isinstance(h.get("name"), str)]
    if not heirs:
        return [], [], None
    shares = [D(h.get("share")) for h in heirs]
    out = [{"name": h["name"], "relationship": h.get("relationship"), "share": s} for h, s in zip(heirs, shares)]
    unknown = [h["name"] for h, s in zip(heirs, shares) if s is None]
    if not unknown:
        return out, [], None
    stated = sum((s for s in shares if s is not None), Decimal(0))
    remainder = Decimal(1) - stated
    for heir in out:
        if heir["share"] is None and remainder > 0:
            heir["share_range"] = (Decimal(0), remainder)
    names = ", ".join(unknown)
    if remainder > 0:
        warning = (f"The will's share for {names} is not recorded: the stated shares are kept and the remaining "
                   f"{num(remainder * 100, 2)}% is shown as a range for them (from nothing up to all of it), not "
                   "split equally. Record each heir's share from the will.")
    else:
        warning = (f"The will's share for {names} is not recorded and the stated shares already total "
                   f"{num(stated * 100, 2)}%: their share is unknown. Record each heir's share from the will.")
    return out, unknown, warning


def _mechanism_label(mechanism: str, product: str, country: str | None, value: Mapping[str, Any]) -> dict:
    """What moves it, in both languages, with the rule it follows."""
    if mechanism == "beneficiary":
        if product == "afore":
            return {"en": "AFORE beneficiaries (LSS Art. 193)", "es": "Beneficiarios de la AFORE (LSS Art. 193)"}
        if product == "insurance":
            return {"en": "Policy beneficiaries", "es": "Beneficiarios de la póliza"}
        if product == "us_retirement":
            return {"en": "Plan beneficiaries", "es": "Beneficiarios del plan"}
        if country == "MX" and product == "bank":
            return {"en": "Bank beneficiaries (LIC Art. 56)", "es": "Beneficiarios bancarios (LIC Art. 56)"}
        if country == "MX" and product == "broker":
            # LMV Art. 201 governs casas de bolsa only; funds, PPRs and others follow their own contract or law.
            return {"en": "Brokerage beneficiaries (LMV Art. 201)",
                    "es": "Beneficiarios de la casa de bolsa (LMV Art. 201)"}
        if country == "US" and product == "bank":
            return {"en": "Payable on death (POD)", "es": "Pago al fallecimiento (POD)"}
        if country == "US" and product == "property":
            return {"en": "Transfer-on-death deed", "es": "Escritura TOD"}
        if country == "US":
            return {"en": "Transfer on death (TOD)", "es": "Transferencia al fallecimiento (TOD)"}
        return {"en": "Beneficiary designation", "es": "Designación de beneficiarios"}
    return {
        "trust": {"en": "Trust terms", "es": "Términos del fideicomiso"},
        "survivorship": {"en": "Joint owner by survivorship", "es": "Cotitular por supervivencia"},
        "legal_beneficiaries": {"en": "Legal beneficiaries (LFT Art. 501)",
                                "es": "Beneficiarios legales (LFT Art. 501)"},
        "plan_default": {"en": "Plan default (spouse, then estate)",
                         "es": "Regla del plan (cónyuge, luego sucesión)"},
        "will": {"en": "Will (juicio sucesorio or probate)" if country != "US" else "Will (probate)",
                 "es": "Testamento (juicio sucesorio)" if country != "US" else "Testamento (probate)"},
        "intestate": {"en": "Intestate succession", "es": "Sucesión intestamentaria"},
        "unknown": {"en": "Unknown: beneficiaries not recorded", "es": "Desconocido: beneficiarios sin registrar"},
    }[mechanism]


# ------------------------------------------------------------------ the register


def register(sit: Mapping[str, Any], snapshot: Mapping[str, Any], inputs: Mapping[str, Any] | None = None,
             as_of: date | str | None = None) -> dict:
    """The register as a service envelope (status, result, missing, warnings, sources, assumptions)."""
    inputs = dict(inputs or {})
    today = _date(as_of) or _date(sit.get("as_of")) or datetime.now(timezone.utc).date()
    review_years = inputs.get("review_years", DESIGNATION_REVIEW_YEARS)
    if isinstance(review_years, bool) or not isinstance(review_years, int) or not 1 <= review_years <= 30:
        raise ValueError("review_years must be a whole number of years (1-30)")
    facts = _facts(snapshot)
    warnings: list[str] = []
    for key in list(facts):
        if key.startswith(("insurance.", "property.", "estate.", "cash.", "investment.")):
            try:
                validate(key, facts[key].get("value"))
            except SchemaError as exc:
                warnings.append(f"{exc}; it was left out of the register.")
                facts.pop(key)
    profile = sit.get("profile") or {}
    residence = (profile.get("residence") or {}).get("country")
    currency = sit.get("currency")
    convert = _Converter(sit)
    family = _Family(facts, sit, today)
    will_fact = (facts.get("estate.will") or {}).get("value")
    will = will_fact if isinstance(will_fact, dict) else None
    guard_fact = (facts.get("estate.guardianship") or {}).get("value")
    guardianship = guard_fact if isinstance(guard_fact, dict) else {}
    guardian = guardianship.get("guardian") if isinstance(guardianship.get("guardian"), str) else None
    guardian_named = bool(guardian) or bool(will and will.get("guardian_named"))
    accounts, notes = _accounts(sit, facts, convert)
    warnings += notes
    assumptions: list[str] = [
        "An estimate of how these rules usually apply at the person's own death, not legal advice: a notario "
        "(Mexico) or estate attorney (US) confirms it. Each Mexican state has its own civil code and each US "
        "state its own probate law.",
        "Amounts are today's balances in the reporting currency, before taxes, debts and costs of the estate.",
    ]
    if family.regime in ("sociedad_conyugal", "community_property"):
        assumptions.append("Under sociedad conyugal or community property the spouse already owns half of what was "
                           "acquired during the marriage; the table shows whole balances, so the estate is smaller.")
    rows: list[dict] = []
    gaps: list[dict] = []
    questions: list[dict] = []
    events = family.events()
    # Regime scenarios: True counts only the person's half of marital property.
    regimes = [True, False] if family.married and family.common is None else [bool(family.married and family.common)]
    if family.free_union:
        assumptions.append("Concubinato (free union) has no marital regime: nothing is shared by law, so balances "
                           "count whole. A concubino inherits like a spouse only if the couple lived together as "
                           "if married for the five years before the death, or had a child together, both free of "
                           "marriage (CCF Art. 1635); with more than one concubino, none inherits.")
    if family.married and family.common is None:
        questions.append({"code": "marital_regime_unknown", "field": "estate.family.marital_regime"})
        assumptions.append("The marital regime is unknown: amounts are a range from sociedad conyugal (half of what "
                           "was acquired in the marriage is already the spouse's and is not in the estate) to "
                           "separación de bienes (all of it is).")
    elif family.married and family.common:
        assumptions.append("Sociedad conyugal (or community property): only the person's half of what was acquired "
                           "in the marriage is in the estate; accounts marked marital_property false count whole. "
                           "AFORE and life insurance follow their own laws and are not halved.")

    def gap(code: str, row: dict | None, amount: Decimal | None, en: str, es: str, **extra: Any) -> None:
        estimate = (row or {}).get("_est") if amount is None else None
        gaps.append({"code": code, "key": row["key"] if row else None, "label": row["label"] if row else None,
                     "amount_at_risk": _money(amount), "currency": currency, "en": en, "es": es,
                     **({"amount_estimate": _money(estimate), "amount_basis": "last_stated_balance"}
                        if estimate is not None else {}),
                     **{k: v for k, v in extra.items() if v is not None}})

    will_heirs, unknown_shares, share_warning = _will_heirs(will) if will and will.get("exists") else ([], [], None)
    if share_warning:
        warnings.append(share_warning)
        raw_heirs = [h for h in will.get("heirs") or [] if isinstance(h, dict) and isinstance(h.get("name"), str)]
        for index, heir in enumerate(raw_heirs):
            if heir["name"] in unknown_shares:
                questions.append({"code": "will_share_unknown", "field": f"estate.will.heirs[{index}].share",
                                  "name": heir["name"]})
    family_asks: list[str] = []
    pension_survivors = _pension_survivors(family)
    for entry in accounts:
        key, value, srow = entry["key"], entry["value"], entry["row"]
        country, country_note = _country(value, srow, residence)
        product = _product(key, value, srow)
        label = _label(value, srow, key)
        amount = entry["amount"]
        titling = value.get("titling") or "individual"
        co_owners = [n for n in value.get("co_owners") or [] if isinstance(n, str)]
        owner_share = D(value.get("owner_share"))
        if owner_share is None:
            owner_share = Decimal(1) / (1 + len(co_owners)) if titling in ("joint", "mancomunada") and co_owners \
                else (Decimal("0.5") if titling in ("joint", "mancomunada") else Decimal(1))
        # Under sociedad conyugal (or community property) half of what was acquired in the marriage is already
        # the spouse's: only the person's half is in the estate.  AFORE and life insurance follow their own laws.
        marital = bool(family.married) and product in _MARITAL_PRODUCTS and value.get("marital_property") is not False \
            and titling not in ("fideicomiso", "trust")
        held = amount * owner_share if amount is not None else None
        values = {flag: (amount * owner_share * (Decimal("0.5") if flag and marital else 1)
                         if amount is not None else None) for flag in regimes}
        known = [v for v in values.values() if v is not None]
        mine_lo, mine = (min(known), max(known)) if known else (None, None)
        beneficiaries = value.get("beneficiaries")
        row: dict[str, Any] = {"key": key, "label": label, "product": product, "country": country,
                               "titling": titling, "value": _money(amount),
                               "estate_value": _money(mine) if mine_lo == mine else None,
                               "estate_value_range": {"low": _money(mine_lo), "high": _money(mine)}
                               if mine_lo != mine else None,
                               "currency": currency, "owner_share": _money(owner_share) if owner_share != 1 else None,
                               "designation_date": value.get("designation_date"), "notes": [], "heirs": [],
                               "bypasses_court": None, "_values": values, "_hi": mine, "_marital": marital}
        if amount is None and entry.get("estimate") is not None:
            row["_est"] = entry["estimate"] * owner_share
        if entry.get("designation_key"):
            row["designation_key"] = entry["designation_key"]
        elif any(value.get(f) is not None for f in ("beneficiaries", "titling", "designation_date")):
            row["designation_key"] = "inline"  # saved on the account by an older write; still read
        if entry.get("orphan"):
            row["orphan"] = True
        if marital and family.common is not False:
            row["notes"].append({"en": ("Under sociedad conyugal half of this is already your spouse's and is not in "
                                        "your estate" if family.common else "If you married under sociedad conyugal, "
                                        "half of this is already your spouse's") + (
                                        "; the institution pays the beneficiaries, and your spouse can claim their "
                                        "half." if primary_named(value) else "."),
                                 "es": ("En sociedad conyugal la mitad de esto ya es de tu cónyuge y no entra en tu "
                                        "herencia" if family.common else "Si te casaste por sociedad conyugal, la "
                                        "mitad de esto ya es de tu cónyuge") + (
                                        "; la institución paga a los beneficiarios y tu cónyuge puede reclamar su "
                                        "mitad." if primary_named(value) else ".")})
        if entry.get("statement_only"):
            row["statement_only"] = True
        if entry.get("stale"):
            row["notes"].append({"en": "Balance past its review date: amount unknown here.",
                                 "es": "Saldo por reconfirmar: aquí el monto es desconocido."})
        if country_note:
            row["country_basis"] = country_note
        custodian = next((n for n in (srow.get("institution"), value.get("institution"), label)
                          if institution_key(n)[0] in _US_CUSTODIANS), None)
        if custodian and country != "US":
            # The succession law follows where the person lived; the paperwork follows where the account is.
            row["custody_country"] = "US"
            row["notes"].append({
                "en": f"{label} is held by a US custodian: even when Mexican law decides the heirs, it usually "
                      "releases the account only against US estate papers (probate or a small-estate affidavit), "
                      "unless a TOD beneficiary is on file.",
                "es": f"{label} está en un custodio de EE. UU.: aunque la ley mexicana decida los herederos, "
                      "normalmente sólo entrega la cuenta con trámites sucesorios de EE. UU. (probate o affidavit "
                      "de herencia menor), salvo que tenga beneficiario TOD registrado."})
        primary = [b for b in beneficiaries or [] if isinstance(b, dict) and not b.get("contingent")]
        backup = [b for b in beneficiaries or [] if isinstance(b, dict) and b.get("contingent")]
        heirs: list[dict] = []
        mechanism = "unknown"
        rule = None

        def alive(person: Mapping[str, Any]) -> bool:
            return not person.get("deceased") and _fold(person.get("name")) not in family.deceased

        if titling in ("fideicomiso", "trust"):
            mechanism = "trust"
            heirs = [{"name": b.get("name") or b.get("person"), "relationship": b.get("relationship"),
                      "share": D(b.get("share"))} for b in primary]
            if heirs and any(h["share"] is None for h in heirs):
                for h in heirs:
                    h["share"] = Decimal(1) / len(heirs)
            row["bypasses_court"] = True
        elif titling == "joint" and country != "MX":
            mechanism = "survivorship"
            names = co_owners or ["co-owner"]
            heirs = [{"name": n, "relationship": None, "share": Decimal(1) / len(names)} for n in names]
            row["bypasses_court"] = True
            row["notes"].append({"en": "Assumes joint tenancy with right of survivorship; tenants in common pass by "
                                       "will instead.",
                                 "es": "Supone cotitularidad con derecho de supervivencia; en copropiedad simple "
                                       "pasa por testamento."})
        elif primary:
            mechanism = "beneficiary"
            row["bypasses_court"] = True
            shares = [D(b.get("share")) for b in primary]
            given = [s for s in shares if s is not None]
            if given and len(given) == len(shares):
                total = sum(given, Decimal(0))
                if abs(total - 1) > SHARE_TOLERANCE:
                    gap("shares_not_100", {"key": key, "label": label}, mine,
                        f"{label}: beneficiary shares add to {total * 100:.0f}%, not 100%.",
                        f"{label}: los porcentajes de beneficiarios suman {total * 100:.0f}%, no 100%.",
                        total=_money(total))
            elif given:
                gap("shares_not_100", {"key": key, "label": label}, mine,
                    f"{label}: some beneficiaries have no percentage.",
                    f"{label}: a algunos beneficiarios les falta el porcentaje.")
                shares = [Decimal(1) / len(primary)] * len(primary)
            else:
                shares = [Decimal(1) / len(primary)] * len(primary)
                if len(primary) > 1:
                    row["notes"].append({"en": "No percentages recorded: equal shares assumed.",
                                         "es": "Sin porcentajes registrados: se suponen partes iguales."})
            orphaned = Decimal(0)
            for person, share in zip(primary, shares):
                name = person.get("name") or person.get("person")
                if not alive(person):
                    orphaned += share
                    gap("beneficiary_predeceased", {"key": key, "label": label},
                        mine * share if mine is not None else None,
                        f"{label}: {name} is named as beneficiary but has died.",
                        f"{label}: {name} está como beneficiario pero ya falleció.", person=name)
                    continue
                heirs.append({"name": name, "relationship": person.get("relationship"), "share": share,
                              "_person": person})
                if person.get("relationship") == "ex_spouse" or _fold(name) in family.ex:
                    gap("beneficiary_ex_spouse", {"key": key, "label": label},
                        mine * share if mine is not None else None,
                        f"{label}: {name}, an ex-spouse, is still a beneficiary.",
                        f"{label}: {name}, tu expareja, sigue como beneficiaria.", person=name,
                        note=("In the US many states revoke an ex-spouse designation on divorce, but ERISA plans "
                              "and policies may not; change it on the form.")
                        if country == "US" else None)
            if orphaned:
                living_backup = [b for b in backup if alive(b)]
                if living_backup:
                    part = orphaned / len(living_backup)
                    heirs += [{"name": b.get("name") or b.get("person"), "relationship": b.get("relationship"),
                               "share": part, "_person": b, "contingent": True} for b in living_backup]
                else:
                    heirs.append({"name": None, "relationship": "estate", "share": orphaned, "fallback": True})
        elif product == "afore" and country == "MX":
            mechanism = "legal_beneficiaries" if beneficiaries == [] else "unknown"
        elif product == "us_retirement" and (plan_is_erisa(value) or value.get("plan_type") == "457b"):
            # ERISA plans pay the spouse by law; a governmental 457(b) follows its plan document's default.
            mechanism = "plan_default" if beneficiaries == [] else "unknown"
        elif beneficiaries == [] or (product == "property" and country != "US"):
            # Nothing designated: the will moves it, or the law when there is none.
            if will is None:
                row["fallback"] = "will_or_intestate"
            else:
                mechanism = "will" if will.get("exists") else "intestate"
        else:
            row["fallback"] = "will_or_intestate"

        # Who receives it when no designation moves it.
        if mechanism == "will":
            row["bypasses_court"] = False
            heirs = [dict(h) for h in will_heirs]
            if not heirs:
                row["notes"].append({"en": "Heirs as the will names them (not recorded here).",
                                     "es": "Herederos según el testamento (no registrados aquí)."})
        elif mechanism == "intestate":
            row["bypasses_court"] = False
            heirs, rule = _intestate(country, family, asks=family_asks)
        elif mechanism == "legal_beneficiaries":
            heirs, rule = _intestate("MX", family, lft=True)
            rule = ("LSS Art. 193: with no designated beneficiaries, the LFT Art. 501 order (spouse or concubine "
                    "and children, then dependent parents); approximated")
        elif mechanism == "plan_default":
            heirs = ([{"name": family.spouse or "spouse", "relationship": "spouse", "share": Decimal(1)}]
                     if family.married else [{"name": None, "relationship": "estate", "share": Decimal(1)}])
            rule = "Most plans pay the spouse, then the estate, when no beneficiary is named (check the plan document)"
            if not plan_is_erisa(value):
                rule = ("A governmental 457(b) is outside ERISA: with no beneficiary named, the plan document's "
                        "default applies (usually the spouse, then the estate)")
        if rule:
            row["rule"] = rule
        row["mechanism"] = mechanism
        row["mechanism_label"] = _mechanism_label(mechanism, product, country, value) \
            if mechanism in ("beneficiary", "trust", "survivorship", "legal_beneficiaries", "plan_default", "will",
                             "intestate", "unknown") else None
        row["_heirs"] = heirs
        row["_pension"] = False
        if product == "afore" and country == "MX" and heirs and pension_survivors is not False:
            # LSS Arts. 64, 84, 127-137, 193: when survivors qualify for a pension (a spouse or concubine, children
            # under 16 or 25 if studying, dependent parents) the RCV balance funds the widow's and orphans'
            # pension; only what can be paid in one sum (vivienda, voluntary savings, any surplus) is handed over.
            row["_pension"] = True
            row["pension_possible"] = True
            row["lump_sum_range"] = {"low": 0, "high": _money(mine)} if mine is not None else None
            row["notes"].append({"en": "If your survivors qualify for an IMSS widow's or orphans' pension, the retiro, "
                                       "cesantía and vejez balance funds that pension instead of being paid out; the "
                                       "beneficiaries receive only what can be paid in one sum (vivienda, voluntary "
                                       "savings, any surplus). The amounts are a range from nothing to the whole "
                                       "balance (LSS Arts. 64 and 193).",
                                 "es": "Si tus deudos tienen derecho a pensión de viudez u orfandad del IMSS, el saldo "
                                       "de retiro, cesantía y vejez paga esa pensión en lugar de entregarse; los "
                                       "beneficiarios reciben solo lo que puede entregarse en una sola exhibición "
                                       "(vivienda, ahorro voluntario, excedente). Los montos van de cero al saldo "
                                       "completo (LSS Arts. 64 y 193)."})

        # Minors named directly
        for heir in heirs:
            person = heir.get("_person") or {}
            if not person or person.get("via_trust"):
                continue
            age = _age(None, person.get("birth_year"), today) if person.get("birth_year") else \
                family.child_age(heir["name"] or "")
            minor = person.get("minor") is True or (age is not None and age < ADULT_AGE)
            if minor and not guardian_named:
                # CCF Art. 425: a surviving parent with patria potestad represents the child and administers
                # what the child receives, so the money is not left without an adult.
                own_child = family.child_age(heir["name"] or "") is not None
                parent_alive = bool(family.married) and not (family.spouse and _fold(family.spouse) in family.deceased)
                softened = country == "MX" and own_child and parent_alive
                gap("minor_direct", {"key": key, "label": label},
                    mine * heir["share"] if mine is not None and heir["share"] is not None else None,
                    f"{label}: {heir['name']} is a minor named directly, with no guardian or trust on record.",
                    f"{label}: se nombró directamente a {heir['name']}, menor de edad, sin tutor ni fideicomiso "
                    "registrado.", person=heir["name"],
                    severity="low" if softened else None,
                    note=("While the other parent lives, they hold patria potestad and administer what the child "
                          "receives (CCF Art. 425, assumed to be your spouse or partner); a tutor or trust matters "
                          "if both parents are gone.") if softened else None)

        # No beneficiary, or unknown
        designable = product != "property" or country == "US"
        shown_en = shown_es = _short(held, currency)
        if held is None and row.get("_est") is not None:
            shown_en = f"last stated {_short(row['_est'], currency)}"
            shown_es = f"último saldo conocido {_short(row['_est'], currency)}"
        if designable and mechanism not in ("beneficiary", "trust", "survivorship"):
            if beneficiaries == [] and product == "afore":
                gap("afore_beneficiaries", row, mine,
                    f"Your AFORE ({label}, {shown_en}) has no designated beneficiaries.",
                    f"Tu AFORE ({label}, {shown_es}) no tiene beneficiarios designados.")
            elif beneficiaries == [] and product == "insurance":
                gap("no_beneficiary", row, mine,
                    f"Your {label} life policy ({shown_en}) has no beneficiaries.",
                    f"Tu seguro de vida {label} ({shown_es}) no tiene beneficiarios.")
            elif beneficiaries == []:
                gap("no_beneficiary", row, mine,
                    f"Your {label} account ({shown_en}) has no beneficiaries.",
                    f"Tu cuenta de {label} ({shown_es}) no tiene beneficiarios.")
            elif beneficiaries is None:
                questions.append({"code": "beneficiaries_unknown", "key": key, "label": label,
                                  "amount": _money(mine), "currency": currency,
                                  "field": f"{designation_key(key)}.beneficiaries",
                                  "afore": product == "afore" or None})
        # An old designation, or one older than a marriage or a child
        designated = _date(value.get("designation_date"))
        if mechanism == "beneficiary" and designated:
            later = [e for e in events if e[0] > designated]
            if later:
                when, event, who = later[0]
                gap("designation_before_event", row, mine,
                    f"{label}: beneficiaries were named in {designated.year}, before your "
                    + ("marriage" if event == "marriage" else "divorce" if event == "divorce"
                       else f"child {who}'s birth")
                    + f" ({when.year}).",
                    f"{label}: los beneficiarios son de {designated.year}, antes de "
                    + ("tu matrimonio" if event == "marriage" else "tu divorcio" if event == "divorce"
                       else f"que naciera {who}") + f" ({when.year}).", event=event)
            elif (today - designated).days / 365.25 > review_years:
                gap("designation_old", row, mine,
                    f"{label}: beneficiaries were last named in {designated.year}, over {review_years} years ago.",
                    f"{label}: los beneficiarios se nombraron en {designated.year}, hace más de {review_years} años.")
        # ERISA: a married participant's 401(k) goes to the spouse unless the spouse consented in writing
        if product == "us_retirement" and plan_is_erisa(value) and family.married and mechanism == "beneficiary":
            spouse_share = sum((h["share"] for h in heirs if h.get("relationship") == "spouse"
                                or (family.spouse and _fold(h["name"]) == _fold(family.spouse))), Decimal(0))
            if spouse_share < 1 and value.get("spousal_consent") is not True:
                gap("erisa_spousal_consent", row, mine * (1 - spouse_share) if mine is not None else None,
                    f"{label}: a 401(k) goes to your spouse unless they consent in writing to other beneficiaries; "
                    "no consent is on record.",
                    f"{label}: un 401(k) es para tu cónyuge salvo que firme su consentimiento para otros "
                    "beneficiarios; no hay consentimiento registrado.")
        # Notes that explain the mechanism (country specifics)
        if country == "MX" and product == "bank":
            row["notes"].append({"en": "LIC Art. 56 beneficiaries receive only this account's balance, in the stated "
                                       "percentages; with none, it goes by will or intestate succession.",
                                 "es": "Los beneficiarios del Art. 56 LIC reciben solo el saldo de esta cuenta en los "
                                       "porcentajes indicados; sin ellos, pasa por testamento o intestado."})
        if titling == "mancomunada":
            row["notes"].append({"en": "A cuenta mancomunada is not a beneficiary: the co-holder keeps their own part; "
                                       "yours goes to your beneficiaries or heirs.",
                                 "es": "Una cuenta mancomunada no es un beneficiario: el cotitular conserva su parte; "
                                       "la tuya va a tus beneficiarios o herederos."})
        if product == "afore":
            row["notes"].append({"en": "Designate in the AFORE app or branch; without it, the balance follows the LFT "
                                       "Art. 501 order.",
                                 "es": "Se designa en la app AforeMóvil o en sucursal; sin designación, el saldo "
                                       "sigue el orden del Art. 501 LFT."})
        if product == "us_retirement" and mechanism == "beneficiary" and \
                any(h.get("relationship") != "spouse" for h in heirs if h.get("name")):
            row["notes"].append({"en": "Non-spouse beneficiaries generally must empty an inherited IRA or 401(k) "
                                       "within 10 years (SECURE Act), unless an eligible designated beneficiary.",
                                 "es": "Beneficiarios que no son cónyuge suelen tener que vaciar la cuenta heredada "
                                       "en 10 años (SECURE Act), salvo excepciones."})
        rows.append(row)

    for field in family_asks:
        questions.append({"code": "parents_unknown" if field.endswith("parents_living") else "siblings_unknown",
                          "field": field})
    if family_asks:
        assumptions.append("Who survives among parents and siblings is unknown: with no descendants they can take "
                           "part of an intestate estate (CCF Arts. 1626-1627: half to the parents, or a third to "
                           "the siblings; UPC 2-102(2): a parent shares above the spouse's first $300,000), so the "
                           "spouse's share is a range, not all of it.")
    _settle_heirs(rows, family, regimes, convert, questions, assumptions)

    # Will
    marriage_or_child = [e for e in events if e[1] in ("marriage", "child")]
    passing_by_will = sum((r["_hi"] or Decimal(0) for r in rows
                           if r["mechanism"] in ("will", "intestate") or r.get("fallback")), Decimal(0))
    mx = residence == "MX" or any(r["country"] == "MX" for r in rows)
    september = today.month == 9
    mes_en = (" It is Mes del Testamento: notaries offer up to 50% off this September." if september else
              " Every September (Mes del Testamento) notaries offer up to 50% off.") if mx else ""
    mes_es = (" Es el Mes del Testamento: las notarías dan hasta 50% de descuento este septiembre." if september else
              " Cada septiembre (Mes del Testamento) las notarías dan hasta 50% de descuento.") if mx else ""
    will_state = "unknown"
    if will is None:
        questions.append({"code": "will_unknown", "field": "estate.will.exists"})
    elif not will.get("exists"):
        will_state = "missing"
        gap("no_will", None, passing_by_will,
            "No will: what has no beneficiary goes through intestate succession." + mes_en,
            "Sin testamento: lo que no tiene beneficiario pasa por un juicio intestamentario." + mes_es,
            mes_del_testamento=mx or None)
    else:
        will_state = "done"
        signed = _date(will.get("date"))
        if signed is None:
            questions.append({"code": "will_date_unknown", "field": "estate.will.date"})
        else:
            later = [e for e in marriage_or_child if e[0] > signed]
            if later:
                when, event, who = later[0]
                will_state = "review"
                gap("will_before_event", None, passing_by_will,
                    f"Your will ({signed.year}) predates your " + ("marriage" if event == "marriage" else
                                                                  f"child {who}'s birth") + f" ({when.year})." + mes_en,
                    f"Tu testamento ({signed.year}) es anterior a " + ("tu matrimonio" if event == "marriage" else
                                                                      f"que naciera {who}") + f" ({when.year})."
                    + mes_es, event=event)
            elif (today - signed).days / 365.25 > review_years:
                will_state = "review"
                gap("will_old", None, passing_by_will,
                    f"Your will is from {signed.year}, over {review_years} years ago." + mes_en,
                    f"Tu testamento es de {signed.year}, de hace más de {review_years} años." + mes_es)
    # Guardianship
    total_estate = sum((r["_hi"] or Decimal(0) for r in rows), Decimal(0))
    total_low = sum((min((v for v in r["_values"].values() if v is not None), default=Decimal(0)) for r in rows),
                    Decimal(0))
    guardian_state = "not_applicable"
    if family.minors:
        guardian_state = "done" if guardian_named else "missing"
        if not guardian_named:
            to_minors = sum((D(h["amount"] if h["amount"] is not None else (h.get("amount_range") or {}).get("high"))
                             or Decimal(0) for r in rows for h in r["heirs"]
                             if h["name"] and family.child_age(h["name"]) is not None
                             and family.child_age(h["name"]) < ADULT_AGE), Decimal(0))
            gap("no_guardian", None, to_minors or total_estate,
                "Minor children and no guardian (tutor) named: a judge would choose one.",
                "Hijos menores y ningún tutor designado: un juez elegiría a uno.")
    elif not family.minors_known:
        guardian_state = "unknown"
    # US-situs assets for a non-resident alien (wealth.estate)
    us_situs = _us_situs(sit, profile, inputs.get("us_situs"), convert, today)
    if us_situs.get("gap"):
        amount = convert(D(us_situs["us_situs_total_usd"]), "USD")
        gap("us_situs_nra", None, amount,
            f"US-situs assets of US${D(us_situs['us_situs_total_usd']):,.0f} are over the US$60,000 a non-resident "
            "can leave free of US estate tax; Form 706-NA would be needed.",
            f"Tienes US${D(us_situs['us_situs_total_usd']):,.0f} en activos con situs en EE.UU., arriba de los "
            "US$60,000 exentos para un no residente; se tendría que presentar el Formato 706-NA.",
            tax_range_usd=us_situs.get("estimated_tax_range_usd"), task="estate")
    if us_situs.get("missing"):
        questions.append({"code": "us_situs_status", "field": us_situs["missing"]})

    # Heirs summed
    totals: dict[str, dict] = {}
    for row in rows:
        for heir in row["heirs"]:
            name = heir["name"] or {"en": "Will or intestate heirs", "es": "Herederos por testamento o intestado"}
            key = name if isinstance(name, str) else "_fallback"
            entry = totals.setdefault(key, {"name": name, "low": None, "high": None, "unknown_parts": 0, "via": []})
            span = heir.get("amount_range") or ({"low": heir["amount"], "high": heir["amount"]}
                                                if heir["amount"] is not None else None)
            if span is None:
                entry["unknown_parts"] += 1
            else:
                entry["low"] = (entry["low"] or Decimal(0)) + D(span["low"])
                entry["high"] = (entry["high"] or Decimal(0)) + D(span["high"])
            if row["mechanism"] not in entry["via"]:
                entry["via"].append(row["mechanism"])
    heirs_out = sorted(({"name": v["name"], "amount": _money(v["high"]) if v["low"] == v["high"] else None,
                         "amount_range": {"low": _money(v["low"]), "high": _money(v["high"])}
                         if v["low"] != v["high"] else None,
                         "currency": currency, "complete": v["unknown_parts"] == 0, "via": v["via"]}
                        for v in totals.values()),
                       key=lambda h: -(D(h["amount"] if h["amount"] is not None
                                         else (h["amount_range"] or {}).get("high")) or Decimal(0)))
    unassigned = sum((r["_hi"] or Decimal(0) for r in rows if not r["heirs"]), Decimal(0))

    def at_risk(item: Mapping[str, Any]) -> Decimal:
        """Known amount, else the last stated balance, else unknown-high (ranked as the largest)."""
        for field in ("amount_at_risk", "amount_estimate"):
            if item.get(field) is not None:
                return D(item[field])
        return Decimal("Infinity")

    gaps.sort(key=lambda g: (g.get("severity") == "low", -at_risk(g), GAP_CODES.index(g["code"]), g["key"] or ""))
    for index, item in enumerate(gaps):
        item["rank"] = index + 1
    by_key: dict[str, list[str]] = {}
    for item in gaps:
        if item["key"]:
            by_key.setdefault(item["key"], []).append(item["code"])
    for row in rows:
        row["gaps"] = by_key.get(row["key"], [])
    score = _score(rows, will_state, guardian_state)
    missing = sorted({q["field"] if isinstance(q["field"], str) else ", ".join(q["field"]) for q in questions})
    result = {
        "as_of": today.isoformat(), "currency": currency, "jurisdiction": residence,
        "rows": rows, "heirs": heirs_out, "unassigned": _money(unassigned) if unassigned else None,
        "total": _money(total_estate) if total_low == total_estate else None,
        "total_range": {"low": _money(total_low), "high": _money(total_estate)} if total_low != total_estate else None,
        "gaps": gaps, "questions": questions, "completeness": score,
        "will": {"state": will_state, **({k: will.get(k) for k in ("exists", "date", "notaria", "jurisdiction", "kind")
                                          if will.get(k) is not None} if will else {})},
        "guardian": {"state": guardian_state, "name": guardian, "minors": family.minors or None},
        "us_situs": us_situs or None,
        "top_gap": gaps[0] if gaps else None,
        "review_years": review_years, "trade_call": None,
    }
    for row in rows:
        for internal in ("_values", "_hi", "_heirs", "_marital", "_est", "_pension"):
            row.pop(internal, None)
    sources = [dict(SOURCES[k], checked_on=CHECKED_ON) for k in (
        "lic_56", "lmv_201", "lss_193", "lsar", "lscs", "ccf_intestate", "mes_testamento", "secure", "irs_rmd",
        "erisa", "egelhoff", "upc")]
    if us_situs.get("sources"):
        sources += us_situs.pop("sources")
    assumptions += [
        "Beneficiary designations (LIC Art. 56 bank deposits, LMV Art. 201 brokerage, LSS Art. 193 AFORE, life "
        "policies, US TOD/POD and plan beneficiaries) pay outside the juicio sucesorio or probate and usually "
        "prevail over the will for that account.",
        "Intestate shares follow the Código Civil Federal order (descendants; the spouse next to them only up to "
        "a child's share, net of what they own, Arts. 1624-1625; spouse and parents half each, Art. 1626; then "
        "siblings a third next to the spouse, Art. 1627; then collaterals) or, in the US, the Uniform Probate Code "
        "(2-102(2): a spouse next to a parent takes the first $300,000 plus three-fourths of the rest). Gap amounts "
        "use the upper end of any range; a gap whose amount is unknown ranks by the last stated balance, or first "
        "when there is none.",
        "A designation older than the review period, or older than a marriage, divorce or child, is flagged for "
        f"review ({review_years} years).",
        "Stated accounts covered by a statement take the statement's value; statement accounts nobody described "
        "have unknown beneficiaries (unknown is never 'none').",
    ]
    status = "ready" if rows or will is not None else "needs_input"
    if status == "ready" and missing:
        status = "partial"
    return {"status": status, "result": result, "missing": missing if rows or will is not None
            else ["cash.<id> / investment.<id> / insurance.<id> / property.<id>", "estate.will"],
            "warnings": warnings, "sources": sources, "assumptions": assumptions}


def primary_named(value: Mapping[str, Any]) -> bool:
    return any(isinstance(b, dict) and not b.get("contingent") for b in value.get("beneficiaries") or [])


def _settle_heirs(rows: list[dict], family: _Family, regimes: list[bool], convert: _Converter,
                  questions: list[dict], assumptions: list[str]) -> None:
    """Amounts per heir across the scenarios that unknown facts leave open; a range when they differ."""
    # Scenarios: marital regime x the spouse's own property (unknown: nothing, or at least a child's portion).
    assets = family.spouse_assets
    stated = convert(D(assets.get("amount")), assets.get("currency")) if assets else None
    art_1624 = [r for r in rows if r["mechanism"] == "intestate" and any(h.get("art_1624") for h in r["_heirs"])]
    own_options: list[Decimal | None] = [stated] if stated is not None else [Decimal(0), None]
    children = next((sum(1 for h in r["_heirs"] if h.get("art_1624") == "child") for r in art_1624), 0)
    scenarios = []
    for flag in regimes:
        mass = sum((r["_values"][flag] or Decimal(0) for r in art_1624), Decimal(0))
        # The spouse's half of the gananciales is property of their own for Art. 1624.
        # (equal to the person's half of each marital account in this scenario).
        halves = sum((r["_values"][flag] or Decimal(0) for r in rows if r["_marital"]), Decimal(0)) \
            if flag else Decimal(0)
        for own in own_options:
            fraction = _spouse_fraction(mass, children, None if own is None else own + halves) if children else None
            scenarios.append((flag, fraction))
    # UPC 2-102(2): the spouse's part next to a parent depends on the whole intestate mass.
    upc_rows = [r for r in rows if r["mechanism"] == "intestate" and any(h.get("upc") for h in r["_heirs"])]
    upc_first = convert(UPC_SPOUSE_FIRST_USD, "USD")
    upc_fraction: dict[bool, Decimal | None] = {}
    for flag in regimes:
        mass = sum((r["_values"][flag] or Decimal(0) for r in upc_rows), Decimal(0))
        if not upc_rows or any(r["_values"][flag] is None for r in upc_rows):
            upc_fraction[flag] = None
        elif upc_first is None:
            upc_fraction[flag] = None  # no USD rate: somewhere from three-fourths to all
        elif mass <= 0:
            upc_fraction[flag] = Decimal(1)
        else:
            upc_fraction[flag] = (min(mass, upc_first) + max(mass - upc_first, Decimal(0)) * Decimal("0.75")) / mass
    open_question = any(len({f for regime, f in scenarios if regime == flag}) > 1 for flag in regimes)
    if art_1624 and stated is None and open_question:
        questions.append({"code": "spouse_assets_unknown", "field": "estate.family.spouse_assets"})
        assumptions.append("What the spouse owns is unknown: next to descendants their intestate share is a range "
                           "from nothing (they own at least a child's portion) to a child's share (they own nothing), "
                           "CCF Arts. 1624-1625.")
    for row in rows:
        for heir in row["_heirs"]:
            amounts, shares = [], []
            for flag, fraction in scenarios:
                share = heir["share"]
                if heir.get("art_1624") and fraction is not None:
                    share = fraction if heir["art_1624"] == "spouse" else (1 - fraction) / children
                options = [share]
                if heir.get("share_range"):
                    options = list(heir["share_range"])
                elif heir.get("upc"):
                    f = upc_fraction.get(flag)
                    spouse_side = heir["upc"] == "spouse"
                    if f is None:
                        options = [Decimal("0.75"), Decimal(1)] if spouse_side else [Decimal(0), Decimal("0.25")]
                    else:
                        options = [f if spouse_side else 1 - f]
                    if heir.get("parents_unknown"):
                        options.append(Decimal(1) if spouse_side else Decimal(0))
                value = row["_values"][flag]
                for option in options:
                    shares.append(option)
                    amounts.append(value * option if value is not None and option is not None else None)
                    if row.get("_pension") and value is not None:
                        amounts.append(Decimal(0))  # the balance may fund a survivors' pension instead
            out: dict[str, Any] = {"name": heir["name"], "relationship": heir.get("relationship")}
            known_shares = [s for s in shares if s is not None]
            if known_shares and min(known_shares) != max(known_shares):
                out["share"], out["share_range"] = None, {"low": num(min(known_shares), 4),
                                                          "high": num(max(known_shares), 4)}
            else:
                out["share"] = num(known_shares[0], 4) if known_shares else None
            known_amounts = [a for a in amounts if a is not None]
            if None in amounts or not known_amounts:
                out["amount"] = None
            elif min(known_amounts) != max(known_amounts):
                out["amount"], out["amount_range"] = None, {"low": _money(min(known_amounts)),
                                                            "high": _money(max(known_amounts))}
            else:
                out["amount"] = _money(known_amounts[0])
            if heir.get("contingent"):
                out["contingent"] = True
            if heir.get("fallback"):
                out["name"] = None
                out["via"] = "will_or_intestate"
            row["heirs"].append(out)


def plan_is_erisa(value: Mapping[str, Any]) -> bool:
    return value.get("plan_type") in _ERISA_PLANS


def _score(rows: list[dict], will_state: str, guardian_state: str) -> dict:
    """0-100: designations (by value), the will and, with minors, a guardian; unknown earns nothing."""
    designable = [r for r in rows if r["product"] != "property" or r["country"] == "US"]

    def ok(row: Mapping[str, Any]) -> Decimal:
        """1 for a working designation, 1/2 when it only needs a review, 0 otherwise."""
        if row["mechanism"] not in ("beneficiary", "trust", "survivorship"):
            return Decimal(0)
        if not row.get("gaps"):
            return Decimal(1)
        return Decimal("0.5") if set(row["gaps"]) <= REVIEW_GAPS else Decimal(0)

    values = [r["_hi"] for r in designable]
    by_value = bool(designable) and None not in values and sum(values, Decimal(0)) > 0
    parts: dict[str, dict] = {}
    if designable:
        if by_value:
            total = sum(values, Decimal(0))
            covered = sum((r["_hi"] * ok(r) for r in designable), Decimal(0))
        else:
            total, covered = Decimal(len(designable)), sum((ok(r) for r in designable), Decimal(0))
        parts["designations"] = {"weight": SCORE_WEIGHTS["designations"], "earned": covered / total,
                                 "basis": "value" if by_value else "count",
                                 "working": sum(1 for r in designable if ok(r) == 1),
                                 "to_review": sum(1 for r in designable if ok(r) == Decimal("0.5")),
                                 "of": len(designable)}
    parts["will"] = {"weight": SCORE_WEIGHTS["will"],
                     "earned": {"done": Decimal(1), "review": Decimal("0.5")}.get(will_state, Decimal(0)),
                     "state": will_state}
    if guardian_state in ("done", "missing", "unknown"):
        parts["guardian"] = {"weight": SCORE_WEIGHTS["guardian"],
                             "earned": Decimal(1) if guardian_state == "done" else Decimal(0),
                             "state": guardian_state}
    weight = sum(p["weight"] for p in parts.values())
    score = sum(p["weight"] * p["earned"] for p in parts.values()) / weight * 100 if weight else Decimal(0)
    for part in parts.values():
        part["earned"] = num(part["earned"], 3)
    return {"score": int(score.quantize(Decimal(1))), "parts": parts,
            "method": "designations 60 (share of value with a working designation, trust or survivorship; half "
                      "when it only needs a review), will 30 "
                      "(half when due for review), guardian 10 when there are minors; parts that do not apply are "
                      "left out and the rest rescaled; unknown earns nothing"}


def _us_situs(sit: Mapping[str, Any], profile: Mapping[str, Any], request: Any, convert: _Converter,
              today: date) -> dict:
    """US-situs exposure for a non-resident alien through wealth.estate (never for US persons)."""
    from . import estate as estate_module
    if isinstance(request, Mapping):
        report = estate_module.us_estate_exposure(dict(request))
    else:
        citizenship = [c for c in profile.get("citizenship") or [] if isinstance(c, str)]
        us_person = profile.get("us_person")
        residence = (profile.get("residence") or {}).get("country")
        domicile = ((sit.get("holdings") or {}).get("domicile") or {})
        us_value = D(domicile.get("US"))
        if not us_value:
            return {}
        if us_person is True or "US" in citizenship or residence == "US":
            return {}  # a US person or US resident: worldwide estate, run task estate instead
        if us_person is None and not citizenship:
            return {"missing": ["client.profile.us_person (US citizen or green-card holder?)"]}
        usd = convert(us_value, sit.get("currency"), "USD")
        if usd is None:
            return {"missing": [f"fx.USD/{sit.get('currency')}"]}
        report = estate_module.us_estate_exposure({
            "year": today.year, "decedent": {"us_citizen": False, "green_card": False, "us_domiciled": False},
            "assets": [{"id": "us_domiciled_holdings", "type": "us_stock", "value_usd": str(usd)}]})
    exposure = (report.get("result") or {}).get("exposure") or {}
    total = exposure.get("us_situs_total_usd")
    if total is None:
        return {"missing": report.get("missing") or []}
    return {"us_situs_total_usd": total, "gap": D(total) > US_SITUS_THRESHOLD_USD,
            "estimated_tax_range_usd": exposure.get("estimated_tax_range_usd"),
            "form_706na_likely_required": exposure.get("form_706na_likely_required"),
            "sources": report.get("sources") or []}


def summary(sit: Mapping[str, Any], snapshot: Mapping[str, Any], as_of: Any = None) -> dict | None:
    """The one-line You-page summary: score and top gap, or None when nothing is known to register."""
    try:
        report = register(sit, snapshot, {}, as_of)
    except (ValueError, TypeError, KeyError):
        return None
    result = report["result"]
    if not result["rows"]:
        return None
    top = result["gaps"][0] if result["gaps"] else None
    question = next((q for q in result["questions"] if q["code"] == "beneficiaries_unknown"), None)
    return {"score": result["completeness"]["score"], "gaps": len(result["gaps"]),
            "top_gap": {"en": top["en"], "es": top["es"], "amount": top["amount_at_risk"]} if top else None,
            "question": {"en": f"Who are the beneficiaries of {question['label']}?",
                         "es": f"¿Quiénes son los beneficiarios de {question['label']}?"} if question and not top
            else None,
            "currency": result["currency"]}


TASKS = ("estate_register",)


def run_task(task: str, inputs: Mapping[str, Any], sit: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict:
    if task not in TASKS:
        raise ValueError("estate_register.run_task supports only task='estate_register'")
    allowed = {"review_years", "us_situs"}
    unknown = sorted(set(inputs) - allowed)
    if unknown:
        raise ValueError(f"estate_register inputs: unknown {unknown}; expected {{client_id or facts, as_of?, "
                         "review_years?, us_situs?: estate-task inputs}")
    return register(sit, snapshot, inputs, sit.get("as_of"))


__all__ = ["GAP_CODES", "SOURCES", "TASKS", "register", "run_task", "summary"]
