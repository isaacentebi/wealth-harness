"""Read-only Cuenca connector (Mexican IFPE electronic-payment account, MXN).

:class:`CuencaConnector` reads balance entries (Cuenca's ledger of every
movement, each with its rolling balance), the deposits, transfers, card
transactions, commissions and bill payments they point to, the savings pockets
(*apartados*) and the latest monthly statement's metadata, and turns them into
the same :class:`wealth.ingest.IngestProposal` a statement upload produces.
Nothing is saved here; the person confirms the summary first.

Read-only by construction: every request passes
:meth:`wealth.connectors._rest.ReadOnlyClient.guard`, which allows only ``GET``
on the listed collections, and the default transport refuses any other method.
The SDK's transfer, card, wallet-transaction, API-key and login calls (all
``POST``/``PATCH``/``DELETE``) and JWT creation are never used.

Credentials
    ``WEALTH_CUENCA_API_KEY`` and ``WEALTH_CUENCA_API_SECRET``, or the OS
    keychain (service ``wealth-cuenca``, accounts ``api_key`` and
    ``api_secret``).  They are sent only as HTTP Basic auth (as the official SDK
    does), never in a URL, wrapped in :class:`~wealth.connectors._rest.Secret`
    and scrubbed from every error.  ``WEALTH_CUENCA_SANDBOX=1`` selects the
    sandbox host.

Cuenca sources (read 2026-09-21)
    * Official Python SDK, README (``CUENCA_API_KEY``/``CUENCA_API_SECRET``,
      sandbox, JWT, user login, balance in cents, queries newest first with
      automatic paging): https://github.com/cuenca-mx/cuenca-python
    * SDK HTTP client (hosts ``api.cuenca.com``/``sandbox.cuenca.com``, Basic auth,
      ``X-Cuenca-Api-Version: 2020-03-19``, JSON ``{"items", "next_page_uri"}``):
      https://github.com/cuenca-mx/cuenca-python/blob/main/cuenca/http/client.py
    * Resources: ``balance_entries`` (amount, descriptor, name, rolling_balance,
      type credit/debit, related_transaction_uri, funding_instrument_uri,
      wallet_id), ``deposits`` (network cash/internal/spei, tracking_key = clave
      de rastreo), ``transfers`` (account_number, recipient_name, network,
      tracking_key), ``card_transactions`` (type auth/capture/refund/void/...,
      network atm/visa, card_last4), ``commissions`` (type card_request,
      cash_deposit, outgoing_spei, card_shipping, account_fee), ``savings``
      (wallet balance, name, category), ``statements`` (year, month; PDF/XML
      download): https://github.com/cuenca-mx/cuenca-python/tree/main/cuenca/resources
    * Enums and query parameters (page_size max 100, created_after/created_before,
      wallet_id): https://github.com/cuenca-mx/cuenca-validations/tree/main/cuenca_validations/types
    * Recorded sandbox responses used as the fixture shapes:
      https://github.com/cuenca-mx/cuenca-python/tree/main/tests/resources/cassettes
    * Consumer terms (balances and commissions in MXN; apartados earn no yield;
      statements generated in the app): https://cuenca.com/faq and
      https://cuenca.com/comisiones

What could not be verified
    Cuenca publishes no public API reference or rate limit, and its site does
    not say whether an individual app user can obtain an API key (the API is
    offered to platforms).  The connector therefore works for anyone Cuenca has
    issued a key and secret to, backs off on HTTP 429/5xx using ``Retry-After``
    when sent, and everyone else keeps uploading the monthly statement.  Cuenca
    offers no investment product; apartados are separate wallets without yield.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ..ingest.common import envelope, out
from ..ingest.model import _summary, build_proposal, diff_proposals, proposal_digest
from ..ingest.redact import mask_account
from . import _rest
from ._rest import ConnectorError, ReadOnlyClient, Secret


NAME = "cuenca"
INSTITUTION = "Cuenca"
API_URL = "https://api.cuenca.com"
SANDBOX_URL = "https://sandbox.cuenca.com"
API_VERSION = "2020-03-19"
KEYCHAIN_SERVICE = "wealth-cuenca"
KEY_ENV, SECRET_ENV, SANDBOX_ENV = "WEALTH_CUENCA_API_KEY", "WEALTH_CUENCA_API_SECRET", "WEALTH_CUENCA_SANDBOX"
_ID = r"[A-Za-z0-9_\-]{2,64}"
# The only requests this connector can make (all GET).
ALLOWED_PATHS = (
    r"/balance_entries", r"/deposits", r"/transfers", r"/card_transactions", r"/commissions", r"/bill_payments",
    r"/savings", r"/statements",
    rf"/deposits/{_ID}", rf"/transfers/{_ID}", rf"/card_transactions/{_ID}", rf"/commissions/{_ID}",
    rf"/bill_payments/{_ID}",
)
PAGE_SIZE = 100
MAX_PAGES = 100
MAX_LOOKUPS = 50
DEFAULT_DAYS = 365
MIN_INTERVAL = 0.25  # no published limit; stay polite
TIMEOUT_SECONDS = 180.0
_MEXICO = ZoneInfo("America/Mexico_City")
_HINTS = {401: "Check the Cuenca API key and secret stored in the keychain.",
          403: "The Cuenca key is not allowed to read this account."}

default_transport: _rest.Transport = _rest.https_transport({"api.cuenca.com", "sandbox.cuenca.com"})
default_sleep = _rest.default_sleep
default_clock = _rest.default_clock


# -- credentials ------------------------------------------------------------

class CuencaKeys:
    """API key and secret; never printed."""

    __slots__ = ("api_key", "api_secret")

    def __init__(self, api_key: Secret, api_secret: Secret):
        self.api_key, self.api_secret = api_key, api_secret

    @property
    def source(self) -> str:
        return self.api_key.source if self.api_key.source == self.api_secret.source else "mixed"

    def values(self) -> tuple[str, str]:
        return self.api_key.reveal(), self.api_secret.reveal()

    def __repr__(self) -> str:
        return f"CuencaKeys(source={self.source!r}, api_key='****', api_secret='****')"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("CuencaKeys cannot be serialized")


def load_keys(environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None,
              platform: str | None = None) -> CuencaKeys | None:
    key = _rest.load_secret(KEY_ENV, KEYCHAIN_SERVICE, "api_key", label="Cuenca API key", environ=environ,
                            runner=runner, platform=platform)
    secret = _rest.load_secret(SECRET_ENV, KEYCHAIN_SERVICE, "api_secret", label="Cuenca API secret",
                               environ=environ, runner=runner, platform=platform)
    return CuencaKeys(key, secret) if key and secret else None


def sandbox_default(environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return (environ.get(SANDBOX_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


# -- fetch ------------------------------------------------------------------

def client(keys: CuencaKeys, *, sandbox: bool = False, transport: _rest.Transport | None = None,
           sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
           timeout: float = TIMEOUT_SECONDS) -> ReadOnlyClient:
    def headers() -> dict[str, str]:
        return {"Authorization": _rest.basic_auth(keys.api_key, keys.api_secret), "Accept": "application/json",
                "X-Cuenca-Api-Version": API_VERSION, "User-Agent": "wealth-harness/0.2 (read-only)"}

    def secrets() -> list[str]:
        key, secret = keys.values()
        return [key, secret, _rest.basic_auth(keys.api_key, keys.api_secret).split()[1]]

    return ReadOnlyClient(provider="Cuenca", base_url=SANDBOX_URL if sandbox else API_URL, headers=headers,
                          allowed_paths=ALLOWED_PATHS, transport=transport or default_transport, secrets=secrets,
                          sleep=sleep or default_sleep, clock=clock or default_clock, timeout=timeout,
                          min_interval=MIN_INTERVAL, hints=_HINTS)


def _pages(api: ReadOnlyClient, path: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every item of a collection, following ``next_page_uri`` (each hop re-checked by the guard)."""
    items: list[dict[str, Any]] = []
    page = api.get_json(path, {**params, "page_size": PAGE_SIZE})
    for _ in range(MAX_PAGES):
        if not isinstance(page, Mapping) or not isinstance(page.get("items"), list):
            raise ConnectorError(f"Cuenca returned {path} in an unexpected shape.")
        items.extend(page["items"])
        following = page.get("next_page_uri")
        if not following:
            return items
        page = api.get_json(str(following))
    raise ConnectorError(f"More than {MAX_PAGES * PAGE_SIZE} Cuenca {path.strip('/')}; sync a shorter period with since.")


def fetch_snapshot(api: ReadOnlyClient, *, since: date, today: date) -> dict[str, Any]:
    """Every read the proposal needs, as raw Cuenca JSON."""
    after = datetime.combine(since, datetime.min.time(), _MEXICO).astimezone(timezone.utc).replace(tzinfo=None)
    window = {"created_after": after.isoformat()}
    related_window = {"created_after": (after - timedelta(days=7)).isoformat()}
    latest = api.get_json("/balance_entries", {"limit": 1, "wallet_id": "default"})
    entries = {"default": _pages(api, "/balance_entries", {**window, "wallet_id": "default"})}
    savings = _pages(api, "/savings", {"active": "true"})
    latest_by_wallet = {"default": (latest.get("items") or [None])[0] if isinstance(latest, Mapping) else None}
    for saving in savings:
        wallet = str(saving.get("id") or "")
        if re.fullmatch(_ID, wallet):
            entries[wallet] = _pages(api, "/balance_entries", {**window, "wallet_id": wallet})
    related: dict[str, dict[str, Any]] = {}
    for collection in ("deposits", "transfers", "card_transactions", "commissions", "bill_payments"):
        for item in _pages(api, f"/{collection}", related_window):
            related[f"/{collection}/{item.get('id')}"] = item
    lookups = 0
    for wallet_entries in entries.values():
        for entry in wallet_entries:
            uri = str(entry.get("related_transaction_uri") or "")
            if uri in related or not re.fullmatch(r"/(deposits|transfers|card_transactions|commissions|bill_payments)/"
                                                  + _ID, uri):
                continue
            if lookups >= MAX_LOOKUPS:
                break
            lookups += 1
            related[uri] = api.get_json(uri)
    previous_month = today.replace(day=1) - timedelta(days=1)
    try:
        statements = api.get_json("/statements", {"year": previous_month.year, "month": previous_month.month})
    except ConnectorError as exc:
        if exc.status not in (400, 404, 422):
            raise
        statements = {"items": []}
    return {"entries": entries, "latest": latest_by_wallet, "savings": savings, "related": related,
            "statements": (statements.get("items") or []) if isinstance(statements, Mapping) else [],
            "since": since.isoformat(), "as_of": today.isoformat()}


# -- mapping ----------------------------------------------------------------

def _pesos(cents: Any) -> Decimal | None:
    if cents is None or isinstance(cents, bool):
        return None
    try:
        return Decimal(str(cents)) / 100
    except InvalidOperation:
        return None


def _day(value: Any) -> str | None:
    """Cuenca timestamps are UTC without an offset; the local (Mexico City) date is the one the person saw."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_MEXICO).date().isoformat()


def _signed(entry: Mapping[str, Any]) -> Decimal | None:
    amount = _pesos(entry.get("amount"))
    if amount is None:
        return None
    return -abs(amount) if str(entry.get("type") or "").lower() == "debit" else abs(amount)


_CLABE = re.compile(r"(?<!\d)\d{16,18}(?!\d)")
_INCOME = re.compile(r"(?i)\b(n[oó]mina|sueldo|salario|payroll|honorarios|aguinaldo|ptu|pensi[oó]n)\b")


def _text(*parts: Any) -> str:
    joined = " · ".join(" ".join(str(p).split()) for p in parts if p and " ".join(str(p).split()))
    return _CLABE.sub(lambda m: mask_account(m.group(0)) or "****", joined)


def _related_kind(uri: str) -> tuple[str, str]:
    match = re.fullmatch(r"/([a-z_]+)/(.+)", uri or "")
    return (match.group(1), match.group(2)) if match else ("", "")


def _map_entry(entry: Mapping[str, Any], related: Mapping[str, Any], wallet_names: Mapping[str, str],
               ) -> tuple[str, str, str, dict[str, Any]]:
    """(ingest type, external id base, Spanish description, extra fields) for one balance entry."""
    collection, ident = _related_kind(str(entry.get("related_transaction_uri") or ""))
    item = related.get(str(entry.get("related_transaction_uri") or "")) or {}
    credit = str(entry.get("type") or "").lower() == "credit"
    name, descriptor = entry.get("name"), entry.get("descriptor") or item.get("descriptor")
    extra: dict[str, Any] = {}
    tracking = str(item.get("tracking_key") or "").strip()
    if collection == "deposits":
        network = str(item.get("network") or "").lower()
        base = f"CUENCA-SPEI-{tracking}" if network == "spei" and re.fullmatch(r"[A-Za-z0-9]{1,40}", tracking) \
            else f"CUENCA-DP-{ident}"
        kind = "deposit" if network == "cash" else "income" if _INCOME.search(f"{name} {descriptor}") else "transfer"
        label = {"spei": "SPEI recibido", "internal": "Transferencia Cuenca recibida", "cash": "Depósito en efectivo"}
        extra["network"] = network or None
        if tracking:
            extra["tracking_key"] = tracking
        return kind, base, _text(label.get(network, "Depósito"), name, descriptor), extra
    if collection == "transfers":
        network = str(item.get("network") or "").lower()
        base = f"CUENCA-SPEI-{tracking}" if network == "spei" and re.fullmatch(r"[A-Za-z0-9]{1,40}", tracking) \
            else f"CUENCA-TR-{ident}"
        destination = mask_account(item.get("account_number")) if item.get("account_number") else None
        label = "SPEI enviado" if network == "spei" else "Transferencia Cuenca enviada"
        if credit:
            label = f"Devolución de {label.lower()}"
        extra["network"] = network or None
        if tracking:
            extra["tracking_key"] = tracking
        return "transfer", base, _text(label, name or item.get("recipient_name"), descriptor,
                                       f"a {destination}" if destination else None), extra
    if collection == "card_transactions":
        network = str(item.get("network") or "").lower()
        card_type = str(item.get("type") or "").lower()
        merchant = " ".join(str(name or descriptor or "Compra con tarjeta").split())
        extra["merchant"] = merchant
        if item.get("card_last4"):
            extra["card"] = f"****{str(item['card_last4'])[-4:]}"
        if network == "atm":
            return "withdrawal", f"CUENCA-CT-{ident}", _text("Retiro en cajero", merchant), extra
        if credit:
            label = {"refund": "Reembolso", "void": "Cancelación", "chargeback": "Contracargo",
                     "expiration": "Autorización vencida"}.get(card_type, "Abono de tarjeta")
            return "expense", f"CUENCA-CT-{ident}", _text(merchant, label), extra
        return "expense", f"CUENCA-CT-{ident}", merchant, extra
    if collection == "commissions":
        kind_code = str(item.get("type") or "").lower()
        label = {"card_request": "Comisión por reposición de tarjeta", "cash_deposit": "Comisión por depósito en efectivo",
                 "outgoing_spei": "Comisión por SPEI enviado", "card_shipping": "Comisión por envío de tarjeta",
                 "account_fee": "Comisión por manejo de cuenta"}.get(kind_code, "Comisión")
        extra["commission_type"] = kind_code or None
        return "fee", f"CUENCA-CO-{ident}", _text(label, descriptor if descriptor and descriptor != label else None), extra
    if collection == "bill_payments":
        return "expense", f"CUENCA-BP-{ident}", _text("Pago de servicio", name, descriptor), extra
    if collection == "wallet_transactions":
        wallet = entry.get("wallet_id") or "default"
        other = "apartado" if wallet == "default" else "cuenta principal"
        return "transfer", f"CUENCA-WT-{ident}", _text("Movimiento con " + other,
                                                       wallet_names.get(str(wallet)) if wallet != "default" else None), extra
    return "other", f"CUENCA-LE-{entry.get('id')}", _text(name, descriptor), extra


def _chain(entries: list[dict[str, Any]]) -> tuple[Decimal | None, Decimal | None, list[str]]:
    """(opening, closing, breaks) from rolling balances, oldest first."""
    breaks: list[str] = []
    opening = closing = None
    for entry in entries:
        amount, rolling = _signed(entry), _pesos(entry.get("rolling_balance"))
        if amount is None or rolling is None:
            breaks.append(str(entry.get("id")))
            continue
        if closing is None:
            opening = rolling - amount
        elif closing + amount != rolling:
            breaks.append(str(entry.get("id")))
        closing = rolling
    return opening, closing, breaks


def proposal_from_cuenca(snapshot: Mapping[str, Any], *, owner_id: str = "self", sandbox: bool = False,
                         retrieved_at: str | None = None) -> dict[str, Any]:
    """Map a fetched Cuenca snapshot to an ingest proposal (nothing is saved)."""
    as_of, since = snapshot["as_of"], snapshot["since"]
    related = snapshot.get("related") or {}
    savings = [s for s in snapshot.get("savings") or [] if not s.get("deactivated_at")]
    wallet_names = {str(s.get("id")): " ".join(str(s.get("name") or "Apartado").split()) for s in savings}
    warnings: list[str] = []
    reasons: list[str] = []
    assumptions = [
        "Balances and movements are Cuenca's own records (balance entries with rolling balances); SPEI movements "
        "are identified by their clave de rastreo and the rest by Cuenca ids, so a later sync adds only new lines.",
        "Descriptions keep Cuenca's Spanish text; CLABEs and card numbers are masked to their last four digits.",
        "Incoming SPEI is recorded as income only when its text says nómina/sueldo/honorarios; other incoming and "
        "outgoing transfers stay transfers until the person says what they are.",
    ]
    wallets: list[tuple[str, str, list[dict[str, Any]], Decimal | None]] = []
    main_entries = sorted(snapshot["entries"].get("default") or [], key=lambda e: (e.get("created_at") or "", e.get("id") or ""))
    latest = (snapshot.get("latest") or {}).get("default")
    current = _pesos(latest.get("rolling_balance")) if latest else None
    if main_entries and current is None:
        current = _pesos(main_entries[-1].get("rolling_balance"))
    wallets.append(("default", "Cuenta Cuenca", main_entries, current if current is not None else Decimal(0)))
    for saving in savings:
        wallet = str(saving.get("id"))
        rows = sorted(snapshot["entries"].get(wallet) or [], key=lambda e: (e.get("created_at") or "", e.get("id") or ""))
        # The account id comes from the wallet id (stable) rather than its name (the person can rename it).
        wallets.append((wallet, f"Apartado {wallet}", rows, _pesos(saving.get("balance"))))
    accounts = []
    for wallet, label, rows, balance in wallets:
        opening, closing, breaks = _chain(rows)
        if breaks:
            reasons.append(f"{label}: {len(breaks)} Cuenca movement(s) do not follow the rolling balance.")
        if balance is not None and closing is not None and closing != balance:
            reasons.append(f"{label}: the last movement's balance ({out(closing)}) differs from the current balance "
                           f"({out(balance)}); a movement may be missing.")
        if not rows:
            opening = closing = balance
        deposits = sum((a for a in (_signed(e) for e in rows) if a is not None and a > 0), Decimal(0))
        withdrawals = sum((-a for a in (_signed(e) for e in rows) if a is not None and a < 0), Decimal(0))
        accounts.append({
            "label": label, "type": "checking" if wallet == "default" else "savings", "currency": "MXN",
            "cash": [{"amount": out(balance), "currency": "MXN", "label": "Saldo Cuenca"}] if balance is not None else [],
            "positions": [], "period_start": since, "period_end": as_of,
            "flows": ({"opening": out(opening), "deposits": out(deposits), "withdrawals": out(withdrawals),
                       "closing": out(balance if balance is not None else closing)}
                      if opening is not None else None),
        })
    statement = {"institution": INSTITUTION, "institution_key": "cuenca", "as_of": as_of, "currency": "MXN",
                 "market": "mx", "period_start": since, "accounts": accounts}
    ref = "cuenca:sandbox" if sandbox else "cuenca"
    provenance = {"kind": "connector", "provider": NAME, "ref": ref, "period_start": since, "period_end": as_of,
                  "environment": "sandbox" if sandbox else "live",
                  "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds")}
    probe = build_proposal(statement, kind="connector", provenance=provenance, owner_id=owner_id)
    account_ids = [a["id"] for a in probe["result"]["household"]["accounts"]]
    transactions: list[dict[str, Any]] = []
    for (wallet, label, rows, _), account_id in zip(wallets, account_ids):
        used: set[str] = set()
        for entry in rows:
            amount, when = _signed(entry), _day(entry.get("created_at"))
            if amount is None or not when:
                warnings.append(f"A Cuenca movement in {label} has no amount or date; skipped.")
                continue
            kind, base, description, extra = _map_entry(entry, related, wallet_names)
            identifier = base
            if identifier in used:
                identifier = f"{base}-{entry.get('id')}"
            used.add(identifier)
            reason = None
            if kind == "fee" and amount > 0:
                reason = "commission refund; record it against the original commission"
            elif kind == "withdrawal" and amount > 0:
                kind = "transfer"
            elif kind in {"income", "deposit"} and amount < 0:
                reason = f"negative {kind} (a reversal); check it against the original movement"
            elif kind == "other":
                reason = "Cuenca movement without a known related transaction; record it by hand"
            row = {
                "id": identifier, "dedupe_hash": identifier, "account_id": account_id, "date": when,
                "settlement_date": None, "description": description or "Movimiento Cuenca", "amount": out(amount),
                "currency": "MXN", "type": kind, "symbol": None, "quantity": None, "price": None, "fees": None,
                "balance": out(_pesos(entry.get("rolling_balance"))), "page": None, "installment": None,
            }
            row.update({k: v for k, v in extra.items() if v is not None})
            if reason:
                row["not_posted_reason"] = reason
            transactions.append(row)
    transactions.sort(key=lambda t: (t["account_id"], t["date"], t["id"]))
    if not any(snapshot["entries"].values()):
        assumptions.append("No Cuenca movements in the period; only the current balance was recorded.")
    proposal = build_proposal(statement, kind="connector", provenance=provenance, owner_id=owner_id,
                              warnings=warnings, assumptions=assumptions, review_reasons=reasons,
                              transactions=transactions)
    result = proposal["result"]
    display = {f"Apartado {wallet}": f"Apartado {name}" for wallet, name in wallet_names.items()}
    for account in result["household"]["accounts"]:
        account["name"] = display.get(account.get("name"), account.get("name"))
    months = [f"{s.get('year')}-{int(s.get('month')):02d}" for s in snapshot.get("statements") or []
              if isinstance(s.get("year"), int) and isinstance(s.get("month"), int)]
    result["connector"] = {"name": NAME, "environment": "sandbox" if sandbox else "live",
                           "period": {"start": since, "end": as_of}, "movements": len(transactions),
                           "savings_pockets": len(savings), "statements_available": months}
    result["proposal_id"] = proposal_digest(result)
    result["summary"] = _summary(result)
    return proposal


# -- ledger posting ---------------------------------------------------------

def ledger_batch(proposal: Mapping[str, Any], *, batch_id: str, ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Posting batch for a confirmed Cuenca proposal (the statement mapper, keeping Cuenca's reasons).

    Card purchases post as ``expense`` with the merchant as the description, so
    :mod:`wealth.cashflow` categorises them like any statement line; commissions
    on this checking-type account count as bank fees.
    """
    from ..ingest_posting import proposal_to_batch

    result = proposal["result"]
    postable = [tx for tx in result.get("transactions") or [] if not tx.get("not_posted_reason")]
    mapping = proposal_to_batch({**proposal, "result": {**result, "transactions": postable}}, batch_id=batch_id,
                                ledger=ledger)
    mapping["not_posted"].extend(
        {"date": tx["date"], "description": tx["description"], "amount": tx["amount"], "account_id": tx["account_id"],
         "reason": tx["not_posted_reason"]} for tx in result.get("transactions") or [] if tx.get("not_posted_reason"))
    return mapping


# -- connector --------------------------------------------------------------

SETUP = (
    "Ask Cuenca for an API key and secret for your account (the API is offered to platforms; Cuenca does not "
    "publish whether individual app users can get one). Without a key, upload the monthly estado de cuenta instead.",
    f"Store them: security add-generic-password -U -s {KEYCHAIN_SERVICE} -a api_key -w  and  "
    f"security add-generic-password -U -s {KEYCHAIN_SERVICE} -a api_secret -w  (macOS prompts for each), or export "
    f"{KEY_ENV} and {SECRET_ENV} for one session.",
    "Sync with wealth ingest action=connector inputs={\"name\": \"cuenca\"} (optional since=YYYY-MM-DD, default the "
    "last 365 days).",
)


class CuencaConnector:
    """Pull a Cuenca account on demand and return an ingest proposal."""

    provider = NAME
    countries = ("MX",)

    def __init__(self, *, since: Any = None, sandbox: bool | None = None, keys: CuencaKeys | None = None,
                 transport: _rest.Transport | None = None, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None, today: date | None = None,
                 timeout: float = TIMEOUT_SECONDS, key_loader: Callable[[], CuencaKeys | None] = load_keys):
        self.sandbox = sandbox_default() if sandbox is None else bool(sandbox)
        self._today = today
        self.since = _since(since, self.today) if since not in (None, "") else None
        self._keys, self._key_loader = keys, key_loader
        self._transport, self._sleep, self._clock, self._timeout = transport, sleep, clock, timeout

    @property
    def today(self) -> date:
        return self._today or datetime.now(_MEXICO).date()

    @property
    def ref(self) -> str:
        return "cuenca:sandbox" if self.sandbox else "cuenca"

    def __repr__(self) -> str:
        return f"CuencaConnector(sandbox={self.sandbox!r})"

    def proposal(self, *, owner_id: str = "self", previous: dict[str, Any] | None = None) -> dict[str, Any]:
        keys = self._keys or self._key_loader()
        if keys is None:
            return envelope("needs_input", {"setup": list(SETUP)}, missing=[{
                "key": "cuenca.keys", "reason": "missing",
                "detail": f"No Cuenca API key and secret in the keychain (service {KEYCHAIN_SERVICE}, accounts api_key "
                          f"and api_secret) or {KEY_ENV}/{SECRET_ENV}. The person stores them; never paste them into "
                          "the chat. Without a key, upload the monthly statement."}])
        today = self.today
        since = self.since or _since(None, today)
        try:
            api = client(keys, sandbox=self.sandbox, transport=self._transport or default_transport,
                         sleep=self._sleep, clock=self._clock, timeout=self._timeout)
            snapshot = fetch_snapshot(api, since=since, today=today)
            proposal = proposal_from_cuenca(snapshot, owner_id=owner_id, sandbox=self.sandbox)
        except ConnectorError as exc:
            message = _rest.scrub(str(exc), *keys.values())
            return envelope("rejected", {"error": {"code": exc.status, "retryable": exc.retryable, "message": message}},
                            warnings=[message], sources=[self.ref])
        if previous is not None:
            proposal["result"]["changes"] = diff_proposals(previous, proposal)
        return proposal


def _since(value: Any, today: date) -> date:
    if value in (None, ""):
        return today - timedelta(days=DEFAULT_DAYS)
    try:
        chosen = date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError("since must be an ISO date (YYYY-MM-DD)") from None
    if chosen > today:
        raise ValueError("since cannot be in the future")
    return chosen


def status(environ: Mapping[str, str] | None = None, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    keys = load_keys(environ, runner)
    return {"name": NAME, "institution": INSTITUTION, "read_only": True, "token": keys.source if keys else "missing",
            "ready": keys is not None, "needs": [], "optional": ["since"], "setup": list(SETUP),
            "fallback": "Upload the monthly Cuenca estado de cuenta (PDF) with ingest action=file."}


__all__ = ["ALLOWED_PATHS", "CuencaConnector", "CuencaKeys", "fetch_snapshot", "ledger_batch", "load_keys",
           "proposal_from_cuenca", "status"]
