"""Institution, account-type and instrument recognition for US and Mexican statements.

Classification only records what the text signals.  An instrument without a
recognisable signal keeps no asset class: unknown is not guessed.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from .common import fold


INSTITUTIONS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (key, name, re.compile(pattern, re.IGNORECASE))
    for key, name, pattern in (
        ("gbm", "GBM", r"\bGBM\b|grupo burs[aá]til mexicano"),
        ("actinver", "Actinver", r"\bactinver\b"),
        ("banorte", "Banorte", r"\bbanorte\b"),
        ("bbva", "BBVA México", r"\bbbva\b|\bbancomer\b"),
        ("santander", "Santander", r"\bsantander\b"),
        ("cetesdirecto", "Cetesdirecto", r"\bcetes\s?directo\b"),
        ("nu", "Nu México", r"\bnu\s+m[eé]xico\b|\bnubank\b|\bnu\s+bank\b"),
        ("hey", "Hey Banco", r"\bhey,?\s+banco\b"),
        ("kuspit", "Kuspit", r"\bkuspit\b"),
        ("schwab", "Charles Schwab", r"\bschwab\b"),
        ("fidelity", "Fidelity", r"\bfidelity\b"),
        ("vanguard", "Vanguard", r"\bthe\s+vanguard\s+group\b|\bvanguard\s+(?:brokerage|statement|account)\b"),
        ("ibkr", "Interactive Brokers", r"\binteractive\s+brokers\b|\bibkr\b"),
        ("merrill", "Merrill", r"\bmerrill\b"),
        ("etrade", "E*TRADE", r"\be\*?trade\b"),
        ("morganstanley", "Morgan Stanley", r"\bmorgan\s+stanley\b"),
        ("robinhood", "Robinhood", r"\brobinhood\b"),
        ("chase", "Chase", r"\bjpmorgan\s+chase\b|\bchase\s+bank\b"),
        ("wellsfargo", "Wells Fargo", r"\bwells\s+fargo\b"),
        ("bofa", "Bank of America", r"\bbank\s+of\s+america\b"),
        ("alpaca", "Alpaca", r"\balpaca\b"),
        ("cuenca", "Cuenca", r"\bcuenca\b"),
    )
)
# What each institution is: a broker's cash belongs to its brokerage account (a statement or sync of that
# account covers it); a bank's is a bank balance.  Every INSTITUTIONS key is here, connectors included.
INSTITUTION_KINDS: dict[str, str] = {
    "gbm": "broker", "actinver": "broker", "kuspit": "broker", "cetesdirecto": "broker", "schwab": "broker",
    "fidelity": "broker", "vanguard": "broker", "ibkr": "broker", "merrill": "broker", "etrade": "broker",
    "morganstanley": "broker", "robinhood": "broker", "alpaca": "broker",
    "banorte": "bank", "bbva": "bank", "santander": "bank", "nu": "bank", "hey": "bank", "chase": "bank",
    "wellsfargo": "bank", "bofa": "bank", "cuenca": "bank",
}
BROKERS = frozenset(key for key, kind in INSTITUTION_KINDS.items() if kind == "broker")

CASH_LABEL = re.compile(
    r"(?i)^\s*(?:total\s+)?(cash(?:\s*(?:&|and)\s*cash\s*(?:investments|equivalents))?|cash\s+balance|"
    r"cash\s+sweep|sweep(?:\s+account)?|money\s+market(?:\s+fund)?|core\s+(?:cash|position)|"
    r"efectivo(?:\s+disponible)?|saldo\s+en\s+efectivo|saldo\s+efectivo|liquidez|"
    r"efectivo\s+en\s+(?:pesos|d[oó]lares)|saldo\s+disponible\s+en\s+efectivo)\b"
)
CASH_TICKERS = frozenset({
    "SPAXX", "FDRXX", "FZFXX", "SPRXX", "FCASH", "CORE", "SWVXX", "SNVXX", "SNOXX", "VMFXX", "VMRXX",
    "VUSXX", "CASH", "USD", "MXN", "EFECTIVO", "BONDDIA",
})
_ETF = re.compile(r"(?i)\b(etf|ishares|ishrs|spdr|vanguard\s+\w+.*\b(index|etf)|index\s+fund|naftrac|trac)\b")
_FUND = re.compile(r"(?i)\b(fund|fondo|sociedad\s+de\s+inversi[oó]n|siid|sirv|mutual)\b")
_FUND_SECTION = re.compile(r"(?i)\b(sociedades?\s+de\s+inversi[oó]n|fondos?\s+de\s+inversi[oó]n|mutual\s+funds?)\b")
_FIBRA_TICKERS = frozenset({
    "FUNO", "FIBRAMQ", "FIBRAPL", "FMTY", "FIHO", "FINN", "TERRA", "TERRAFINA", "DANHOS", "FSHOP",
    "FIBRAHD", "FNOVA", "FPLUS", "EDUCA", "STORAGE", "FIBRAUP", "FIBRAHOTEL", "NEXT", "FIBRAMTY",
})
_GOV_MX = re.compile(
    r"(?i)\b(?:(?P<cetes>cetes)|(?P<bondes>bondes\s?[dfg]?|bonde[dfg])|(?P<udibono>udibonos?)|"
    r"(?P<bonom>bonos?\s?m\b|bono\s+tasa\s+fija)|(?P<bpag>bpag\s?\d{2}|bpa\s?182|ipab))"
)
_GOV_TV = re.compile(r"^\s*(BI|LD|LF|LG|M|S|IS|IM|IQ|2U)\s+(?:[A-Z]+\s+)?(\d{6})\b")
_PAGARE = re.compile(r"(?i)\b(pagar[eé]s?|prlv|cede[sd]?|certificado\s+de\s+dep[oó]sito)\b")
_REPORTO = re.compile(r"(?i)\breportos?\b")
_BOND = re.compile(r"(?i)\b(bond|treasury|t-bill|t-note|notes?\s+due|debenture|municipal|corporate\s+bond|cd\b|certificate\s+of\s+deposit)\b")
_SIC = re.compile(r"(?i)\b(sic|sistema\s+internacional\s+de\s+cotizaciones|mercado\s+global|global\s+market)\b")
_MATURITY = re.compile(r"\b(\d{2})(\d{2})(\d{2})\b")


def detect_institution(text: str) -> tuple[str | None, str | None]:
    head = text[:6000]
    best: tuple[int, str, str] | None = None
    for key, name, pattern in INSTITUTIONS:
        match = pattern.search(head)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), key, name)
    return (best[1], best[2]) if best else (None, None)


def account_type(text: str) -> tuple[str | None, str]:
    """Return a canonical account type and confidence from a label or header."""
    value = fold(text)
    rules = (
        (r"\broth\b", "roth_ira"), (r"\b(ira|individual retirement)\b", "ira"), (r"\b401 ?k\b|\b403 ?b\b", "401k"),
        (r"\bhsa\b|health savings", "hsa"), (r"\b529\b", "529"),
        (r"\bppr\b|plan personal de retiro", "ppr"), (r"\bafore\b", "afore"),
        (r"\bcheques\b|\bchecking\b|cuenta de cheques|\blibreton\b|\bcuenta digital\b|\bcuenta express\b", "checking"),
        (r"\bhysa\b|high yield savings|\bsavings\b|\bahorro\b|\bcajitas?\b", "savings"),
        (r"credit card|tarjeta de cr[eé]dito|\bcredito\b", "credit_card"),
        (r"\bmortgage\b|hipoteca|hipotecario", "mortgage"),
        (r"brokerage|individual|joint|margin|casa de bolsa|inversi[oó]n|inversiones|cartera|portafolio|contrato|\bcash account\b|taxable", "brokerage"),
        (r"\bdebito\b|\bdebit\b|n[oó]mina|\bvista\b|dep[oó]sito", "checking"),
    )
    for pattern, kind in rules:
        if re.search(pattern, value):
            return kind, "medium"
    return None, "low"


def _maturity(text: str) -> str | None:
    for match in _MATURITY.finditer(text):
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(2000 + year, month, day).isoformat()
        except ValueError:
            continue
    return None


_UCITS_IE = frozenset({
    "CSPX", "VUAA", "VUSA", "VUAG", "IWDA", "SWDA", "EIMI", "VWRA", "VWRL", "VWCE", "IUSA", "CNDX", "EQQQ", "IB01",
    "IBTA", "IBTM", "IDTL", "DTLA", "SXR8", "XDWD", "ISAC", "SPPW", "AGGU", "IGLA", "IUIT", "INRG", "IEMA", "VFEA",
    "VHYL", "IGLN", "SGLN", "CSP1", "SSAC", "IUSQ", "EUNL",
})
_US_ETFS = frozenset({
    "VOO", "IVV", "SPY", "QQQ", "QQQM", "VTI", "VT", "VEA", "VWO", "VXUS", "BND", "AGG", "TLT", "GLD", "IAU", "SCHD",
    "VNQ", "VIG", "VYM", "IEFA", "IEMG", "EEM", "EFA", "XLK", "XLF", "XLE", "XLV", "SMH", "SOXX", "ARKK", "DIA",
    "IWM", "VGT", "SHY", "IEF", "LQD", "HYG", "TIP", "SHV", "SGOV", "BIL", "ACWI", "URTH", "EWW", "VUG", "VTV",
})
_US_STOCKS = frozenset({
    "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "NVDA", "TSLA", "NFLX", "JPM", "V", "MA", "KO", "PEP", "DIS",
    "WMT", "JNJ", "PG", "XOM", "CVX", "BAC", "INTC", "AMD", "ORCL", "CRM", "ADBE", "COST", "MCD", "NKE", "BA",
    "UBER", "PYPL", "AVGO", "LLY", "UNH", "HD", "ABNB", "BRKB", "C", "GS", "MS", "T", "VZ", "PFE", "MRK", "ABBV",
})
_BMV_DOMESTIC = frozenset({
    "WALMEX", "FEMSA", "AMX", "GFNORTE", "GMEXICO", "CEMEX", "BIMBO", "KOF", "AC", "ALSEA", "ASUR", "GAP", "OMA",
    "BOLSA", "ELEKTRA", "GCARSO", "GRUMA", "KIMBER", "LIVEPOL", "MEGA", "ORBIA", "PINFRA", "Q", "RA", "TLEVISA",
    "VESTA", "LAB", "PEOLES", "PE&OLES", "CHDRAUI", "GENTERA", "BBAJIO", "GFINBUR", "CUERVO", "SITES", "NAFTRAC",
    "MEXTRAC", "M10TRAC", "GCC", "VOLAR", "ALFA", "ALPEK", "AGUA", "HERDEZ", "LACOMER", "SORIANA", "GSANBOR",
    "SIMEC", "TRAXION", "NEMAK", "ACTINVR", "SPORT", "FRAGUA", "BAFAR", "AUTLAN", "CMOCTEZ", "ICH", "POCHTEC",
})
_ISIN = re.compile(r"\b([A-Z]{2})[A-Z0-9]{9}\d\b")


def listing(symbol: str | None, description: str | None, section: str = "", *, market: str | None = None) -> dict[str, Any]:
    """Venue facts for a Mexican-context listing: SIC foreign listing versus BMV domestic series.

    SIC rows ("AAPL *", "VOO *", "CSPX N") keep MXN as the listing currency and
    record the home ticker and, when determinable (ISIN prefix or a known
    UCITS/US list), the issuer domicile.  Known BMV names, FIBRAs and Mexican
    government paper are domestic.  Anything else stays unclassified.
    """
    sym = (symbol or "").strip().upper()
    parts = sym.split()
    home = re.sub(r"\*+$", "", parts[0]) if parts else ""
    series = " ".join(parts[1:]) or ("*" if sym.endswith("*") else "")
    isin = _ISIN.search(description or "")
    in_sic_section = bool(_SIC.search(section or "")) or bool(re.search(r"(?i)\bSIC\b", description or ""))
    domestic = home in _BMV_DOMESTIC or home in _FIBRA_TICKERS or home.startswith("FIBRA")
    if domestic and not (isin and isin.group(1) != "MX"):
        kind = ({} if home in _FIBRA_TICKERS or home.startswith("FIBRA") else
                {"asset_class": "fund"} if home.endswith("TRAC") else {"asset_class": "equity"})
        return {"venue": "bmv", "listing_currency": "MXN", "issuer_domicile": "MX", **kind,
                **({"series": series} if series else {})}
    foreign_known = home in _UCITS_IE or home in _US_ETFS or home in _US_STOCKS
    if not (in_sic_section or (market == "mx" and foreign_known and series in {"*", "N", ""})):
        return {}
    result: dict[str, Any] = {"venue": "sic", "listing_currency": "MXN", "underlying_symbol": home}
    if series:
        result["series"] = series
    if isin:
        result["issuer_domicile"] = isin.group(1)
    elif home in _UCITS_IE:
        result["issuer_domicile"] = "IE"
    elif home in _US_ETFS or home in _US_STOCKS:
        result["issuer_domicile"] = "US"
    if home in _UCITS_IE or home in _US_ETFS:
        result["asset_class"] = "fund"
    elif home in _US_STOCKS:
        result["asset_class"] = "equity"
    return result


def classify_instrument(symbol: str | None, description: str | None, section: str = "") -> dict[str, Any]:
    """Recognise cash, Mexican government paper, pagarés, reportos, FIBRAs, SIC listings, funds, bonds."""
    sym = (symbol or "").strip()
    desc = (description or "").strip()
    text = f"{sym} {desc}"
    head = re.split(r"[\s*]+", sym.upper())[0] if sym else ""
    result: dict[str, Any] = {}
    if head.rstrip("*") in CASH_TICKERS or CASH_LABEL.match(desc) or CASH_LABEL.match(sym):
        return {"asset_class": "cash"}
    gov = _GOV_MX.search(text)
    tv = _GOV_TV.match(text.upper())
    if gov or tv:
        kind = next((name for name, value in (gov.groupdict().items() if gov else ()) if value), None)
        if kind is None and tv:
            kind = {"BI": "cetes", "LD": "bondes", "LF": "bondes", "LG": "bondes", "M": "bonom",
                    "S": "udibono", "IS": "bpag", "IM": "bpag", "IQ": "bpag", "2U": "udibono"}[tv.group(1)]
        result.update(asset_class="fixed_income", instrument_type=kind, issuer="Gobierno Federal (MX)", country="MX")
        maturity = _maturity(tv.group(2) if tv else text)
        if maturity:
            result["maturity"] = maturity
        if kind == "udibono":
            result["denomination"] = "UDI"
        return result
    if _PAGARE.search(text):
        return {"asset_class": "fixed_income", "instrument_type": "pagare", "liquid": False,
                **({"maturity": m} if (m := _maturity(text)) else {})}
    if _REPORTO.search(text) or _REPORTO.search(section):
        return {"asset_class": "fixed_income", "instrument_type": "reporto"}
    if head in _FIBRA_TICKERS or head.startswith("FIBRA") or re.search(r"(?i)\bfibra\b", desc):
        return {"asset_class": "real_estate", "instrument_type": "fibra", "country": "MX"}
    if _ETF.search(text) or head.rstrip("*") in _US_ETFS or head.rstrip("*") in _UCITS_IE:
        result["asset_class"] = "fund"
    elif _FUND.search(desc) or _FUND_SECTION.search(section or ""):
        result["asset_class"] = "fund"  # a row under "Sociedades de inversión" is a fund whatever its ticker
    elif _BOND.search(desc):
        result["asset_class"] = "fixed_income"
    return result
