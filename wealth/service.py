"""One application boundary shared by the CLI and MCP transport."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
import importlib
from inspect import signature
import uuid

from . import situation as situation_module
from . import views as views_module
from .situation.schema import SCHEMA, out_of_range
from .store import DEFAULT_REVIEW_DAYS, REVIEW_DAYS, WealthStore, is_stale, secure_delete
from .workflows import prepare


def database_path(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    configured = os.environ.get("WEALTH_DB")
    if configured:
        return Path(configured).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base / "wealth-harness" / "clients.sqlite3"


TASK_MODULES = {
    "import": "household", "exposure": "household",
    "analyze": "market", "stress": "market", "compare": "market", "construct": "market", "factors": "market",
    "sic_premium": "market", "research": "research", "value": "research",
    "project": "planning", "income": "planning", "ladder": "planning", "tax": "tax",
    "mx_holdings": "mexico", "mx_interest": "mexico", "mx_deductions": "mexico", "mx_foreign": "mexico",
    "mx_calendar": "mexico", "estate": "estate",
    "ledger": "ledger", "performance": "ledger", "spending": "cashflow", "dca": "dca",
    "rebalance": "rebalance", "asset_location": "rebalance",
}
# Tasks answered by the service itself rather than one module.
SERVICE_TASKS = ("plan", "calendar", "monitor", "debt_payoff", "policy_draft", "policy_check")
# Investment policy tasks (wealth/policy.py) read the canonical picture, so the service runs them.
POLICY_TASKS = frozenset({"policy_draft", "policy_check"})
TASKS = (*TASK_MODULES, *SERVICE_TASKS)
# Tasks whose module reads the client's transaction ledger from context["ledger"].
LEDGER_TASKS = frozenset({"ledger", "performance", "spending", "dca", "rebalance"})
INGEST_ACTIONS = ("file", "extraction", "chat", "confirm", "confirm_duplicates", "diff", "connector", "connector_status")
_KEEP_PROPOSALS = 20


def upload_dir(client_id: str, db_path: str | Path | None = None) -> Path:
    """The only directory ``ingest`` reads files from for this client.

    ``WEALTH_UPLOAD_DIR`` overrides the root; otherwise it is ``<db dir>/uploads``,
    where the local browser chat saves attachments.  The client segment is
    sanitised exactly as ``web.Uploads`` does.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", client_id).strip(".") or "client"
    configured = os.environ.get("WEALTH_UPLOAD_DIR")
    root = (Path(configured).expanduser() if configured
            else database_path(db_path).expanduser().resolve().parent / "uploads")
    return root / safe[:64]


UPLOAD_RETENTION_DAYS = 30
"""Uploads not purged by a confirm are deleted after this many days (``WEALTH_UPLOAD_RETENTION_DAYS``)."""


def upload_retention_days() -> int | None:
    """Days an unconfirmed upload is kept; ``None`` when age-based purging is off (``0``)."""
    raw = os.environ.get("WEALTH_UPLOAD_RETENTION_DAYS", "").strip()
    try:
        days = int(raw) if raw else UPLOAD_RETENTION_DAYS
    except ValueError:
        days = UPLOAD_RETENTION_DAYS
    return days if days > 0 else None


def purge_on_confirm() -> bool:
    """Whether a confirmed statement's upload is deleted (``WEALTH_KEEP_CONFIRMED_UPLOADS=1`` keeps it)."""
    return os.environ.get("WEALTH_KEEP_CONFIRMED_UPLOADS", "").strip().lower() not in {"1", "true", "yes"}


def _upload_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]


def _sidecar(path: Path) -> Path:
    """The browser chat's metadata file next to an upload (``<id>.json``)."""
    return path.with_suffix(".json")


def purge_uploads(client_id: str, db_path: str | Path | None = None, *, sha256: str | None = None,
                  now: datetime | None = None) -> list[str]:
    """Securely delete this client's uploads: the file with ``sha256``, or those past retention.

    Returns the names removed.  Raw statements carry RFC, CURP, CLABE and account
    numbers; the saved facts and ledger never need the file again.
    """
    root = upload_dir(client_id, db_path)
    removed = []
    if sha256 is not None:
        for path in _upload_files(root):
            if path.suffix == ".json" or path.suffix == ".part":
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            if digest == sha256:
                for target in (path, _sidecar(path)):
                    if target.exists() and secure_delete(target):
                        removed.append(target.name)
        return removed
    days = upload_retention_days()
    if days is None:
        return removed
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - days * 86_400
    for path in _upload_files(root):
        try:
            old = path.stat().st_mtime < cutoff
        except OSError:
            continue
        if old and secure_delete(path):
            removed.append(path.name)
    return removed


def usable_snapshot(snapshot: dict) -> tuple[dict, list[dict]]:
    """The snapshot without facts no reader can handle, and those facts named.

    A fact saved before the schema bounded numbers (an amount of 1e308, say)
    would otherwise break every read of the picture.  It is left out, and
    listed so the person can correct or remove it.
    """
    kept, invalid = [], []
    for fact in snapshot.get("facts") or []:
        problem = out_of_range(fact.get("value"), str(fact.get("key")))
        if problem:
            invalid.append(_invalid(fact, problem))
        else:
            kept.append(fact)
    return ({**snapshot, "facts": kept} if invalid else snapshot), invalid


def _invalid(fact: dict, problem: str) -> dict:
    return {"key": fact.get("key"), "id": fact.get("id"), "problem": problem,
            "fix": f"This saved value of {fact.get('key')} cannot be used; correct it or remove it."}


def closed_ledger_accounts(ledger: dict | None) -> set[str]:
    """Ledger accounts whose every entry has been reversed (e.g. removed from the profile)."""
    entries = (ledger or {}).get("entries") or []
    reversed_ids = {e.get("reverses_id") for e in entries if e.get("kind") == "reversal"}
    seen, live = set(), set()
    for entry in entries:
        if entry.get("kind") == "reversal":
            continue
        seen.add(entry.get("account_id"))
        if entry.get("id") not in reversed_ids:
            live.add(entry.get("account_id"))
    return seen - live


def current_ledger(ledger: dict | None) -> dict | None:
    """The ledger without closed accounts: their history stays stored, but they no longer count."""
    closed = closed_ledger_accounts(ledger)
    if not closed:
        return ledger
    return {**ledger,
            "accounts": [a for a in ledger.get("accounts") or [] if a.get("id") not in closed],
            "entries": [e for e in ledger.get("entries") or [] if e.get("account_id") not in closed],
            "assertions": [a for a in ledger.get("assertions") or [] if a.get("account_id") not in closed]}


def build_situation(snapshot: dict, ledger: dict | None, today, *, since_revision: int | None = None) -> dict:
    """``situation.build`` that one bad fact cannot take down.

    Facts with numbers no reader can handle are left out up front; if the
    picture still fails, each fact is tried alone and the ones that fail are
    left out too.  Every excluded fact is named in ``invalid_facts``.
    """
    clean, invalid = usable_snapshot(snapshot)
    ledger = current_ledger(ledger)
    try:
        sit = situation_module.build(clean, ledger, today, since_revision=since_revision)
    except Exception:
        bad = []
        for fact in clean.get("facts") or []:
            try:
                situation_module.build({**clean, "facts": [fact]}, None, today)
            except Exception as exc:  # noqa: BLE001 - isolate the fact, name it, keep the rest
                bad.append(fact["id"])
                invalid.append(_invalid(fact, f"{fact['key']} could not be read ({type(exc).__name__})"))
        if not bad:
            raise
        clean = {**clean, "facts": [f for f in clean["facts"] if f["id"] not in bad]}
        sit = situation_module.build(clean, ledger, today, since_revision=since_revision)
    sit["invalid_facts"] = invalid
    return sit


def capabilities() -> dict:
    from .catalog import CATALOG, CONNECTORS
    return {
        "product": "wealth-harness", "release": "0.2.0", "tasks": CATALOG,
        "example_policy": "Catalog examples are fictional dated inputs, not current market evidence, recommended assumptions, or facts about this person. Missing fund constituents mean partial look-through coverage.",
        "workflow": "Recall the client; run a financial task; remember verified results; record the decision.",
        "memory": "Sourced facts, revisions, correction history, bounded keyword/concept recall and optional host-supplied semantic vectors.",
        "privacy": "Local plaintext SQLite. Retrieved context may reach the host's model provider. No credentials are stored.",
        "execution": "Analysis and decision support only; no trading, transfers, or external messaging.",
        "tax_scope": "US federal (2025/2026 brackets, LTCG stacking, NIIT, lots, wash sales, harvesting); Mexico "
                     "(Art. 129 BMV/SIC, real interest, deductions/PPR, foreign securities outside the SIC, calendar); "
                     "US estate exposure for non-residents. Not state tax, AFORE/IRA internals or filing positions.",
        "ingest": "wealth_ingest turns an uploaded statement, host extraction or chat facts into a reconciled "
                  "proposal. Nothing is saved until the person says yes and the host calls action=confirm.",
        "connectors": CONNECTORS,
        "monitoring": "Saved opt-in rules evaluated by the host or wealth watch. Unchanged checks stay quiet; no process starts automatically.",
        "fact_contract": fact_contract(),
    }


def _canonical_resources(value) -> bool:
    return isinstance(value, dict) and value.get("available_capital") is not None


def _calendar_schedule(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("months"), list)


def fact_contract() -> dict:
    today = datetime.now(timezone.utc).date()
    return {
        "fields": ["key", "value", "source", "confidence", "expires_on", "merge", "valid_from"],
        "source": {"kind": "user|document|web|tool|inference|pattern",
                   "ref": "actual source reference (URL for web)", "observed_on": "YYYY-MM-DD"},
        "source_kinds": "user: only what the person said themselves in this conversation. document: a file or "
                        "statement they supplied. web: a page you read (ref is its URL). tool: a Wealth result. "
                        "inference: your own interpretation. pattern: something noticed in their transactions "
                        "(e.g. pattern.<id> for a recurring transfer); always inferred and listed apart from what "
                        "they told you until they confirm it (then save it with source.kind=user). Document/web "
                        "facts for goals, profile, preferences, constraints or tax profile are saved as inferred "
                        "until the person confirms them.",
        "confidence": "confirmed: the person explicitly confirmed it | reported (default): the person stated it "
                      "or a document shows it | inferred: an interpretation. Only a user source may be confirmed.",
        "keys": ["client.profile", "income.<id>", "spending.monthly", "cash.<id>", "liability.<id>",
                 "investment.<id>", "goals", "reserve", "thread.<id>", "preference.*", "constraint.*", "onboarding",
                 "policy.ips (written by accepting an IPS decision)",
                 "thesis.*", "research.<SYMBOL>", "planning.project", "planning.income", "planning.ladder",
                 "planning.dca", "tax.profile", "monitor.rules", "account.<id> (statements, via wealth_ingest)"],
        "schema": SCHEMA,
        "legacy_keys": "plan.resources and income.schedule still work as explicit plan/calendar inputs; for the "
                       "person's picture save the canonical keys above (plan and calendar derive their inputs "
                       "from them).",
        "review_days": {**{pattern + ("*" if pattern.endswith(".") else ""): days for pattern, days in REVIEW_DAYS},
                        "other keys": DEFAULT_REVIEW_DAYS},
        "freshness": "Omit expires_on unless the source states a shorter validity; the store sets the review date "
                     "from observed_on and review_days. Past-review facts stay visible but marked stale; reconfirm "
                     "them with the person. Stale and inferred facts are excluded from calculations and decisions.",
        "default_review_on": (today + timedelta(days=DEFAULT_REVIEW_DAYS)).isoformat(),
        "writes": "Omit expected_revision to add new keys or to update existing ones with merge=true. Pass "
                  "expected_revision (the client_revision you read) to replace an existing value wholesale.",
        "merge": "merge=true applies value as a patch: object fields are updated, null removes a field, and "
                 "lists of objects with id (such as goals) are updated by id without dropping other entries.",
        "partial_values": "Incomplete canonical objects and goal entries may be remembered; omit unknown fields. "
                          "Calculations remain unavailable until required fields are present. Never use zero for "
                          "an unknown amount.",
        "valid_from": "When the value became true in the world (YYYY-MM-DD; default source.observed_on), e.g. "
                      "'my salary went up in March' -> valid_from 2026-03-01. The previous value is closed at that "
                      "date and kept in the history (wealth_inspect detail=history).",
        "update_policy": "Document, web, inference and pattern sources never overwrite what the person stated or "
                         "confirmed: the write is held and returned in the receipt's needs_user. Ask the person "
                         "with its question and save their answer with wealth_resolve_contradiction; never pick a "
                         "side silently. An inferred value is replaced by better evidence without expected_revision.",
        "forget": "A null value forgets a key: it leaves a tombstone in the history and is no longer used.",
        "never_store": "Government IDs (SSN, RFC, CURP), account or card numbers, street addresses, passwords, "
                       "tokens or other credentials.",
    }


class _Context(dict):
    """Record which remembered values a financial module actually consults."""
    def __init__(self, values, overridden=()):
        super().__init__(values)
        self.used = set()
        self.requested = set()
        self.overridden = set(overridden)

    def _track(self, key):
        if key != "_evidence" and key not in self.overridden:
            self.requested.add(key)
            if key in self:
                self.used.add(key)

    def get(self, key, default=None):
        self._track(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self._track(key)
        return super().__getitem__(key)


def _call(function, label: str, data: dict, **fixed):
    """Call with named inputs, reporting missing or unknown fields by name."""
    if not isinstance(data, dict):
        raise ValueError(f"{label} inputs must be an object")
    params = {name: p for name, p in signature(function).parameters.items() if name not in fixed}
    unknown = sorted(set(data) - set(params))
    missing = [name for name, p in params.items() if p.default is p.empty and name not in data]
    if unknown or missing:
        expected = ", ".join(name + ("" if p.default is p.empty else "?") for name, p in params.items())
        problems = [f"missing {missing}" if missing else "", f"unknown {unknown}" if unknown else ""]
        raise ValueError(f"{label} inputs: {'; '.join(filter(None, problems))}; expected {{{expected}}}")
    return function(**fixed, **data)


def freshness(facts: list[dict]) -> dict:
    today = datetime.now(timezone.utc).date()
    stale = [f["key"] for f in facts if is_stale(f, today)]
    inferred = [f["key"] for f in facts if f["confidence"] == "inferred" and f["key"] not in stale]
    return {"fresh_fact_keys": [f["key"] for f in facts if f["key"] not in {*stale, *inferred}],
            "stale_fact_keys": stale, "inferred_fact_keys": inferred}


class WealthService:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = database_path(db_path)

    def create(self, client_id: str, display_name: str) -> dict:
        with WealthStore(self.db_path) as store:
            return store.create_client(client_id, display_name)

    def situation(self, client_id: str, since_revision: int | None = None, today=None) -> dict:
        """The person's canonical picture (see ``wealth.situation.build``)."""
        with WealthStore(self.db_path) as store:
            snapshot = store.snapshot(client_id)
            ledger = store.ledger(client_id)
            pending = store.contradictions(client_id)
        sit = build_situation(snapshot, ledger, today or datetime.now(timezone.utc).date(),
                              since_revision=since_revision)
        sit["contradictions"] = pending  # questions waiting for the person, in their own wording
        return sit

    def remember(self, client_id: str, facts: list[dict], expected_revision: int | None = None,
                 request_id: str | None = None) -> dict:
        """Save facts; forgetting a statement account (``account.<id>``) also retires its ledger data.

        The account's ledger entries get reversal entries in the same
        transaction (append-only: the history stays, the numbers stop counting),
        so the brief, the net worth and ``task=ledger`` agree that it is gone.
        """
        with WealthStore(self.db_path) as store:
            with store.atomic():
                receipt = store.remember(client_id, facts, expected_revision, request_id)
                closed = _retire_forgotten_accounts(store, client_id, receipt)
        if closed:
            receipt["ledger"] = closed
        return receipt

    def contradictions(self, client_id: str) -> dict:
        """Pending contradictions between what the person said and newer evidence."""
        with WealthStore(self.db_path) as store:
            pending = store.contradictions(client_id)
        return {"client_id": client_id, "contradictions": pending,
                "next_step": ("Ask the person each question as worded; never pick a side silently. Save the answer "
                              "with resolve_contradiction (keep | use_new | changed, valid_from for changed).")
                if pending else None}

    def resolve_contradiction(self, client_id: str, contradiction_id: str, choice: str,
                              valid_from: str | None = None) -> dict:
        """Apply the person's answer: keep (theirs stands), use_new (theirs was wrong), changed (both, in turn)."""
        with WealthStore(self.db_path) as store:
            return store.resolve_contradiction(client_id, contradiction_id, choice, valid_from)

    def history(self, client_id: str, key: str) -> dict:
        """One key's timeline: "MXN 85,000 since 2026-03; MXN 78,000 from 2025-01 to 2026-03"."""
        with WealthStore(self.db_path) as store:
            return {"client_id": client_id, **store.timeline(client_id, key)}

    def prepare(self, client_id: str, intent: str = "overview") -> dict:
        """Workflow packet (plan, exposure or income) from remembered facts; used by the examples."""
        with WealthStore(self.db_path) as store:
            return prepare(store.snapshot(client_id), intent)

    def context(self, client_id: str | None = None, intent: str = "overview", query: str = "") -> dict:
        if intent == "situation":
            if client_id is None:
                raise ValueError("intent=situation needs client_id")
            sit = self.situation(client_id)
            return {"client_id": client_id, "client_revision": sit["revision"],
                    "brief": situation_module.brief(sit, sit["profile"].get("language")),
                    "situation": {k: v for k, v in sit.items() if k not in {"meta", "evidence"}},
                    "missing_for_onboarding": situation_module.missing_for_onboarding(sit),
                    "fact_contract": fact_contract(),
                    "views": views_module.summaries(views_module.views_for("situation", sit))}
        if client_id is None:
            catalog = capabilities()
            if intent != "overview":
                if intent not in catalog["tasks"]:
                    raise ValueError("unknown task intent; use overview for discovery")
                catalog["tasks"] = {intent: catalog["tasks"][intent]}
            return catalog
        routing = {
            "overview": "", "plan": "goals plan.resources constraint client.profile",
            "exposure": "household portfolio.snapshot constraint", "analyze": "household portfolio.snapshot constraint",
            "research": "household thesis research", "tax": "household tax",
            "income": "goals income.schedule planning.income", "project": "goals planning.project",
            "ladder": "goals planning.ladder", "calendar": "income.schedule",
            "spending": "income.schedule plan.resources account", "dca": "planning.dca constraint.dca",
            "ledger": "account household", "performance": "account household",
            "mx_holdings": "household tax", "mx_interest": "tax", "mx_deductions": "tax",
            "mx_foreign": "household tax", "mx_calendar": "tax", "estate": "household tax",
            "policy_draft": "goals reserve preference constraint client.profile policy",
            "policy_check": "policy constraint goals reserve",
            "rebalance": "household account tax goals reserve constraint", "asset_location": "household account tax",
        }
        from .recall import recall
        with WealthStore(self.db_path) as store:
            snapshot = store.snapshot(client_id)
            result = recall(snapshot, " ".join(filter(None, [query, routing.get(intent, intent)])),
                            embeddings=store.auxiliary(client_id, "embeddings"))
        facts = snapshot["facts"]
        result["available_tasks"] = list(TASKS)
        result["fact_contract"] = fact_contract()
        result["known_fact_keys"] = [f["key"] for f in facts[:50]]
        result["omitted_fact_keys"] = max(0, len(facts) - 50)
        result.update(freshness(facts))
        if result["stale_fact_keys"]:
            result["reconfirm"] = ("Stale facts are past their review date: still visible, excluded from "
                                   "calculations. Reconfirm them with the person before relying on them.")
        result["next_step"] = "Use wealth_run for calculations; wealth_inspect a key if its value was omitted."
        return result

    def recall(self, client_id: str, query: str = "", limit: int = 12,
               query_embedding: list | None = None, embedding_model: str | None = None,
               include_stale: bool = True) -> dict:
        from .recall import recall
        with WealthStore(self.db_path) as store:
            return recall(store.snapshot(client_id), query, limit=limit,
                          embeddings=store.auxiliary(client_id, "embeddings"),
                          query_embedding=query_embedding, embedding_model=embedding_model,
                          include_stale=include_stale)

    def index(self, client_id: str, fact_id: str, embedding: list, model: str) -> dict:
        from .recall import vector
        if not isinstance(model, str) or not model.strip():
            raise ValueError("embedding model must be an explicit identifier")
        normalized = vector(embedding)
        with WealthStore(self.db_path) as store:
            snapshot = store.snapshot(client_id)
            active = {f["id"] for f in snapshot["facts"]}
            if fact_id not in active:
                raise ValueError("embedding must refer to this client's current fact")
            def update(old):
                index = {k: v for k, v in old.items() if k in active}
                index[fact_id] = {"model": model, "vector": normalized}
                return index
            store.update_auxiliary(client_id, "embeddings", update)
            return {"indexed": fact_id, "model": model, "client_revision": snapshot["client"]["revision"]}

    def run(self, task: str, inputs: dict | None = None, client_id: str | None = None,
            save_as: str | None = None, expires_on: str | None = None) -> dict:
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be an object")
        if task not in TASKS:
            raise ValueError(f"unknown task {task!r}; tasks are {', '.join(TASKS)}")
        if save_as and (not client_id or not expires_on):
            raise ValueError("saving a result requires client_id and expires_on")
        if save_as and not (task == "import" and save_as == "household"):
            if not save_as.startswith(("analysis.", "research.")):
                raise ValueError("save_as must be analysis.<name>, research.<symbol>, or household for an import")
        today = datetime.now(timezone.utc).date().isoformat()
        snapshot = {"client": {"id": None, "revision": None}, "facts": [], "decisions": []}
        ledger = None
        invalid: list[dict] = []
        if client_id:
            with WealthStore(self.db_path) as store:
                snapshot = store.snapshot(client_id)
                if (task in LEDGER_TASKS or task in {"plan", "calendar", "debt_payoff"} or task in POLICY_TASKS) \
                        and "ledger" not in inputs:
                    ledger = current_ledger(store.ledger(client_id))
            snapshot, invalid = usable_snapshot(snapshot)
        eligible = [f for f in snapshot["facts"] if f["confidence"] != "inferred"
                    and (not f.get("expires_on") or f["expires_on"] >= today)]
        context = _Context({f["key"]: f["value"] for f in eligible}, overridden=inputs.keys())
        if "household" in inputs:
            context["household"] = inputs["household"]
        if ledger is not None:
            context["ledger"] = ledger
        planning_key = "planning." + task
        remembered_plan = dict.get(context, planning_key)
        if isinstance(remembered_plan, dict) and all(key in inputs for key in remembered_plan):
            context.overridden.add(planning_key)
        context["_evidence"] = {f["key"]: {k: f.get(k) for k in ("id", "source", "expires_on")} for f in eligible}
        derived_evidence: list[str] = []
        if task == "debt_payoff":
            report = self._debt_payoff(inputs, snapshot, ledger, today)
            derived_evidence = report.pop("_evidence", [])
        elif task in POLICY_TASKS:
            report = self._policy(task, inputs, client_id, snapshot, ledger, today)
            derived_evidence = report.pop("_evidence", [])
        elif task in {"plan", "calendar"}:
            # Direct inputs may supply the same canonical facts without requiring a profile.
            keys = ("plan.resources", "goals") if task == "plan" else ("income.schedule",)
            working = {**snapshot, "facts": [f for f in snapshot["facts"] if f["key"] in {*keys, "client.profile"}]}
            supplied = {key: inputs[key] for key in keys if key in inputs}
            derived_missing, derived_assumptions = [], []
            stored = {f["key"]: f["value"] for f in snapshot["facts"]}
            needs_model = bool(client_id) and (
                (task == "plan" and "plan.resources" not in inputs and not _canonical_resources(stored.get("plan.resources")))
                or (task == "calendar" and "income.schedule" not in inputs and not _calendar_schedule(stored.get("income.schedule"))))
            if needs_model:
                # The canonical model supplies what the person told us; no hand-assembled plan.resources.
                sit = build_situation(snapshot, ledger, today)
                invalid += [i for i in sit["invalid_facts"] if i["key"] not in {x["key"] for x in invalid}]
                derive = situation_module.plan_inputs if task == "plan" else situation_module.calendar_inputs
                values, derived_missing, derived_assumptions = derive(sit)
                for key, value in values.items():
                    supplied.setdefault(key, value)
                derived_evidence = list(sit["evidence"].values())
            for key, value in supplied.items():
                working["facts"] = [f for f in working["facts"] if f["key"] != key]
                working["facts"].append({"id": "request:" + key, "key": key, "value": value,
                    "confidence": "reported", "source": {"kind": "user", "ref": "current request", "observed_on": today},
                    "expires_on": today, "revision": 0})
            if derived_missing:
                packet = {"status": "needs_input", "calculations": {}, "missing": derived_missing,
                          "warnings": [], "evidence_ids": []}
            else:
                packet = prepare(working, "plan" if task == "plan" else "income")
            report = {"status": packet["status"], "result": packet["calculations"],
                      "missing": packet["missing"], "warnings": packet["warnings"],
                      "sources": packet["evidence_ids"] + derived_evidence if needs_model else packet["evidence_ids"],
                      "assumptions": derived_assumptions}
            if needs_model:
                report["sources"] = [i for i in report["sources"] if not str(i).startswith("request:")]
                report["assumptions"].append("Inputs were derived from the saved picture (income, spending, cash, "
                                             "investments, debts, goals, reserve).")
        elif task == "monitor":
            if not client_id:
                raise ValueError("monitoring requires an explicit client")
            from .monitor import evaluate
            with WealthStore(self.db_path) as store:
                def update(old):
                    nonlocal report
                    report = evaluate(snapshot, old, inputs)
                    return report.pop("state")
                report = {}
                store.update_auxiliary(client_id, "monitor", update)
        else:
            module = importlib.import_module("." + TASK_MODULES[task], __package__)
            report = module.run(task, inputs, context)
        if task in {"plan", "calendar", "debt_payoff"} or task in POLICY_TASKS:
            used_ids = set(packet["evidence_ids"] if task in {"plan", "calendar"} else []) | set(derived_evidence)
            consumed = [f for f in eligible if f["id"] in used_ids and f["key"] not in inputs]
        elif task == "monitor":
            consumed = eligible  # rules may inspect all facts and decision freshness
        else:
            consumed = [f for f in eligible if f["key"] in context.used]
        report.update(task=task, client_id=client_id,
                      client_revision=snapshot["client"]["revision"],
                      evaluated_at=datetime.now(timezone.utc).isoformat(),
                      evidence_ids=[f["id"] for f in consumed])
        report["excluded_evidence"] = [{"key": f["key"], "reason": "inferred" if f["confidence"] == "inferred" else "expired"}
                                       for f in snapshot["facts"] if f not in eligible]
        if task == "debt_payoff":
            relevant = {f["key"] for f in snapshot["facts"] if f["key"].startswith("liability.")}
        elif task in POLICY_TASKS:
            from .policy import POLICY_FACT_KEYS
            relevant = {f["key"] for f in snapshot["facts"] if f["key"].startswith(POLICY_FACT_KEYS)}
        elif task in {"plan", "calendar"}:
            relevant = set(keys)
        else:
            relevant = {f["key"] for f in snapshot["facts"]} if task == "monitor" else context.requested
        for fact in snapshot["facts"]:
            if fact["key"] in relevant and fact["key"] not in inputs and is_stale(fact):
                report.setdefault("warnings", []).append(
                    f"{fact['key']} is stale (observed {fact['source']['observed_on']}, review date "
                    f"{fact['expires_on']} passed) and was not used; reconfirm it with the person.")
        for item in invalid:
            report.setdefault("warnings", []).append(f"{item['problem']}; it was left out. {item['fix']}")
        if save_as:
            report["request_inputs"] = inputs
        if inputs:
            report.setdefault("assumptions", []).append("Explicit request inputs take precedence over remembered values; they are not saved unless requested.")
        if save_as and report.get("status") in {"ready", "partial"}:
            value = report["result"].get("household") if save_as == "household" else report
            if value is None:
                raise ValueError("no validated value is available to save")
            derived_expiry = min([expires_on, *[f["expires_on"] for f in consumed if f.get("expires_on")]])
            with WealthStore(self.db_path) as store:
                saved = store.remember(client_id, [{"key": save_as, "value": value,
                    "source": {"kind": "tool", "ref": "wealth://" + task + "/" + uuid.uuid4().hex, "observed_on": today},
                    "confidence": "reported", "expires_on": derived_expiry}], snapshot["client"]["revision"])
            report["saved"] = {"key": save_as, "client_revision": saved["write_result"]["resulting_revision"],
                               "expires_on": derived_expiry}
        # Engine-drawn views of this result the answer may place with [[view:<id>]] (never saved with it).
        report["views"] = views_module.summaries(views_module.views_for(task, report))
        return report

    def _debt_payoff(self, inputs: dict, snapshot: dict, ledger, today: str) -> dict:
        """Payoff dates for a monthly debt budget: stored liabilities unless ``liabilities`` is supplied."""
        allowed = {"monthly_amount", "liabilities", "order", "currency", "as_of"}
        unknown = sorted(set(inputs) - allowed)
        if unknown or "monthly_amount" not in inputs:
            raise ValueError(f"debt_payoff inputs: {'unknown ' + str(unknown) if unknown else 'missing monthly_amount'}; "
                             "expected {monthly_amount, liabilities?, order?, currency?, as_of?}")
        as_of = inputs.get("as_of", today)
        evidence: list[str] = []
        liabilities = inputs.get("liabilities")
        if liabilities is None:
            sit = build_situation(snapshot, ledger, as_of)
            liabilities = [{"id": r["id"], "name": r.get("name") or r["kind"], "balance": r["balance"],
                            "annual_rate": r["annual_rate"], "monthly_payment": r["monthly_payment"],
                            "currency": r["currency"]} for r in sit["liabilities"]
                           if inputs.get("currency") is None or r["currency"] == inputs["currency"]]
            evidence = [sit["evidence"][r["key"]] for r in sit["liabilities"] if r["key"] in sit["evidence"]]
        if not isinstance(liabilities, list):
            raise ValueError("liabilities must be a list of {id, balance, annual_rate, monthly_payment, currency?}")
        report = situation_module.debt_payoff(liabilities, inputs["monthly_amount"], inputs.get("currency"),
                                              inputs.get("order"), as_of)
        report["_evidence"] = sorted(set(evidence))
        return report

    def _policy(self, task: str, inputs: dict, client_id: str | None, snapshot: dict, ledger, today: str) -> dict:
        """policy_draft / policy_check; ``propose=true`` records the draft as a decision for the person."""
        from . import policy
        propose = inputs.get("propose", False)
        if not isinstance(propose, bool):
            raise ValueError("propose must be true or false")
        if propose and not client_id:
            raise ValueError("propose=true needs client_id: the draft becomes the person's decision")
        if propose and "facts" in inputs:
            raise ValueError("propose=true drafts from the saved picture; leave out facts")
        report = policy.run_task(task, inputs, snapshot, ledger, today)
        if propose:
            with WealthStore(self.db_path) as store:
                report["decision"] = policy.propose(store, client_id, report, snapshot["client"]["revision"])
        return report

    def client(self, action: str, client_id: str, inputs: dict | None = None) -> dict:
        """CLI client actions. MCP exposes create/index here and reads via wealth_inspect."""
        operations = {"create": self.create, "inspect": self.inspect,
                      "export": lambda client_id: self.inspect(client_id, detail="export"),
                      "forget": self.forget, "index": self.index}
        if action not in operations:
            raise ValueError(f"client action must be one of {', '.join(operations)}")
        return _call(operations[action], f"client {action}", inputs or {}, client_id=client_id)

    def decision(self, action: str, client_id: str, inputs: dict) -> dict:
        if action == "propose":
            return _call(self.propose, "decision propose", inputs, client_id=client_id)
        if action in {"accept", "dismiss"}:
            return _call(self.resolve, f"decision {action}", inputs, client_id=client_id,
                         status="accepted" if action == "accept" else "dismissed")
        raise ValueError("decision action must be propose, accept, or dismiss")

    def inspect(self, client_id: str, detail: str = "current", key: str | None = None,
                keys: list[str] | None = None) -> dict:
        if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
            raise ValueError("keys must be a list of fact keys")
        wanted = set(keys or []) | ({key} if key else set())
        with WealthStore(self.db_path) as store:
            if detail == "current":
                snapshot = store.snapshot(client_id)
                if wanted:
                    snapshot["facts"] = [f for f in snapshot["facts"] if f["key"] in wanted]
                    snapshot["decisions"] = []
                    absent = sorted(wanted - {f["key"] for f in snapshot["facts"]})
                    if absent:
                        snapshot["absent_keys"] = absent
                for fact in snapshot["facts"]:
                    fact["stale"] = is_stale(fact)
                return snapshot
            if detail == "export":
                return store.export_client(client_id)
            if detail == "history" and key:
                timeline = store.timeline(client_id, key)
                return {"client_id": client_id, "key": key, "history": store.history(client_id, key),
                        "timeline": timeline["entries"], "text": timeline["text"]}
            if detail == "contradictions":
                return {"client_id": client_id, "contradictions": store.contradictions(client_id)}
            raise ValueError("detail must be current, export, contradictions, or history with a key")

    def propose(self, client_id: str, title: str, rationale: str, expected_revision: int,
                evidence_ids: list[str], alternatives: list | None = None) -> dict:
        with WealthStore(self.db_path) as store:
            return store.save_decision(client_id, title, rationale, expected_revision,
                                       evidence_ids, alternatives)

    def resolve(self, client_id: str, decision_id: str, status: str,
                expected_revision: int | None = None) -> dict:
        with WealthStore(self.db_path) as store:
            decision = store.set_decision_status(client_id, decision_id, status, expected_revision)
            if status == "accepted":
                from .policy import on_accepted
                stored = on_accepted(store, client_id, decision)  # an accepted IPS becomes policy.ips
                if stored:
                    decision["policy"] = stored
            return decision

    def forget(self, client_id: str, confirm_client_id: str) -> dict:
        """Delete the client's rows and securely delete its upload directory (raw statements)."""
        with WealthStore(self.db_path) as store:
            return store.delete_client(client_id, confirm_client_id)

    # ------------------------------------------------------------------ ingest

    def ingest(self, client_id: str, action: str, inputs: dict | None = None) -> dict:
        """Statement/chat ingestion into a server-held proposal; ``confirm`` saves it.

        Proposals are stored under the client (auxiliary namespace ``ingest``)
        keyed by ``proposal_id``.  ``confirm`` loads that stored proposal, never
        one supplied by the caller, and must follow the person's explicit yes.
        """
        handlers = {"file": self._ingest_file, "extraction": self._ingest_extraction, "chat": self._ingest_chat,
                    "confirm": self._ingest_confirm, "confirm_duplicates": self._ingest_confirm_duplicates,
                    "diff": self._ingest_diff, "connector": self._ingest_connector,
                    "connector_status": self._ingest_connector_status}
        if action not in handlers:
            raise ValueError(f"action must be one of {', '.join(INGEST_ACTIONS)}")
        try:
            purge_uploads(client_id, self.db_path)  # unconfirmed uploads past retention
        except OSError:
            pass
        return _call(handlers[action], f"ingest {action}", inputs or {}, client_id=client_id)

    def _ingest_state(self, client_id: str, update=None) -> dict:
        with WealthStore(self.db_path) as store:
            if update is None:
                return store.auxiliary(client_id, "ingest")
            return store.update_auxiliary(client_id, "ingest", update)

    def _hold(self, client_id: str, proposal: dict) -> dict:
        """Store a proposal (or its extraction request) and return it for display."""
        result = proposal.get("result") or {}
        now = datetime.now(timezone.utc).isoformat()
        request = result.get("extraction_request")
        extraction_id = None
        if isinstance(request, dict):
            extraction_id = hashlib.sha256(repr(sorted(request.get("source", {}).items())).encode()
                                           + repr(request.get("pages")).encode()).hexdigest()[:24]
        pid = result.get("proposal_id") if proposal["status"] in {"ready_to_confirm", "needs_review"} else None

        def update(old):
            state = {k: dict(old.get(k) or {}) for k in ("pending", "extractions", "confirmed")}
            if pid:
                state["pending"][pid] = {"proposal": proposal, "created_at": now}
            if extraction_id:
                state["extractions"][extraction_id] = {"request": request, "created_at": now}
            for name in ("pending", "extractions", "confirmed"):
                recent = sorted(state[name].items(), key=lambda item: item[1].get("created_at", ""))[-_KEEP_PROPOSALS:]
                state[name] = dict(recent)
            return state

        if pid or extraction_id:
            self._ingest_state(client_id, update)
        else:
            self._ingest_state(client_id)  # still validates the client
        shown = {**proposal, "result": dict(result)}
        if pid and isinstance(result.get("household"), dict):
            # What the statement means next to what the person already told us (deterministic).
            shown["result"]["insights"] = situation_module.statement_insights(result, self.situation(client_id))
            from .ingest_posting import missing_positions
            with WealthStore(self.db_path) as store:
                gone = missing_positions(proposal, current_ledger(store.ledger(client_id)))
            shown["result"]["reconciliation"] = {"missing_positions": gone}
        if extraction_id:
            shown["result"]["extraction_id"] = extraction_id
        if pid:
            needs_ack = proposal["status"] == "needs_review"
            shown["result"]["confirmation"] = {
                "required": True,
                "next_step": "Show the summary and any discrepancies. Only after the person says yes, call "
                             f"wealth_ingest action=confirm with proposal_id={pid}"
                             + (" and acknowledge_discrepancies=true (they must accept the listed differences)." if needs_ack else "."),
            }
        elif extraction_id:
            shown["result"]["next_step"] = ("Fill extraction_request.schema from its page text only, then call wealth_ingest "
                                            f"action=extraction with extraction_id={extraction_id} and payload=<the JSON>.")
        return shown

    def _ingest_file(self, client_id: str, path: str, owner_id: str = "self", preset: str | None = None,
                     aliases: dict | None = None, currency: str | None = None, as_of: str | None = None,
                     tolerance: str | None = None, source_text: str | None = None) -> dict:
        from .ingest import ingest_file
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be the uploaded file's path or name")
        root = upload_dir(client_id, self.db_path)
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        proposal = ingest_file(candidate, allowed_roots=[root], owner_id=owner_id, preset=preset, aliases=aliases,
                               currency=currency, as_of=as_of, tolerance=tolerance, source_text=source_text)
        if proposal["status"] == "rejected" and not (root.exists()):
            proposal["warnings"].append(f"Files are read only from the upload directory {root}.")
        return self._hold(client_id, proposal)

    def _ingest_extraction(self, client_id: str, extraction_id: str, payload: dict, owner_id: str = "self",
                           tolerance: str | None = None) -> dict:
        from .ingest import validate_llm_extraction
        stored = (self._ingest_state(client_id).get("extractions") or {}).get(extraction_id)
        if stored is None:
            raise ValueError("extraction_id is unknown or expired; run action=file again")
        proposal = validate_llm_extraction(payload, stored["request"], owner_id=owner_id, tolerance=tolerance)
        return self._hold(client_id, proposal)

    def _ingest_chat(self, client_id: str, items: list, as_of: str | None = None, currency: str | None = None,
                     owner_id: str = "self", conversation_ref: str = "conversation") -> dict:
        from .ingest import proposal_from_chat
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a nonempty list")
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("quote"), str) or not item["quote"].strip():
                raise ValueError(f"items[{index}].quote must hold the person's own words for this item")
        proposal = proposal_from_chat(items, as_of=as_of, currency=currency, owner_id=owner_id,
                                      conversation_ref=conversation_ref)
        return self._hold(client_id, proposal)

    def _ingest_confirm(self, client_id: str, proposal_id: str, acknowledge_discrepancies: bool = False,
                        expires_on: str | None = None) -> dict:
        """Save a held proposal: facts, ledger lines, reconciliation and proposal state in one transaction.

        Idempotent: a confirm that already succeeded (including one that won a
        race with this call) returns the same receipt with ``replayed=true``;
        one that failed part-way left nothing behind, so it can simply be retried.
        Other writes in the meantime do not conflict with it.
        """
        from . import ledger as ledger_module
        from .connectors import batch_mapper
        from .ingest import proposal_to_facts
        from .ingest_posting import describe_changes, is_newest, missing_positions, proposal_to_batch, reconciliation_lines
        if not isinstance(acknowledge_discrepancies, bool):
            raise ValueError("acknowledge_discrepancies must be true or false")
        today = datetime.now(timezone.utc).date()
        with WealthStore(self.db_path) as store:
            with store.atomic():
                state = store.auxiliary(client_id, "ingest")
                done = (state.get("confirmed") or {}).get(proposal_id)
                if done is not None:
                    return {**done["report"], "replayed": True}
                stored = (state.get("pending") or {}).get(proposal_id)
                if stored is None:
                    raise ValueError("proposal_id is unknown or expired; ingest the file or chat again and show "
                                     "the new summary")
                proposal = stored["proposal"]
                packet = proposal_to_facts(proposal, confirmed=True, proposal_id=proposal_id,
                                           acknowledge_discrepancies=acknowledge_discrepancies, expires_on=expires_on)
                if packet["status"] != "ready":
                    return packet
                facts = packet["result"]["facts"]
                batch_id = "ingest:" + proposal_id
                snapshot = store.snapshot(client_id)
                ledger_before = store.ledger(client_id)
                before = build_situation(snapshot, ledger_before, today)
                # The write lock is held, so the revision just read is current: statement keys are
                # replaced without a conflict from writes that happened since the proposal was made.
                saved = store.remember(client_id, facts, snapshot["client"]["revision"],
                                       packet["result"]["request_id"])
                gone = missing_positions(proposal, ledger_before)
                mapper = batch_mapper(proposal) or proposal_to_batch
                mapping = mapper(proposal, batch_id=batch_id, ledger=ledger_before)
                receipt = ledger_module.post(store, client_id, mapping["batch"]) if mapping["batch"] else None
                changes = []
                if receipt is not None and (receipt.get("reconciliation") or {}).get("breaks"):
                    changes = self._reconcile_to_statement(store, client_id, proposal, mapping, receipt,
                                                           ledger_before, batch_id, is_newest, reconciliation_lines)
                after = build_situation(store.snapshot(client_id), store.ledger(client_id), today)
                ledger_view = _ledger_summary(receipt, mapping)
                if changes:
                    ledger_view["adjustments"] = changes
                    ledger_view["plain"] += (" Reconciled to the statement with labelled adjustments: "
                                             + describe_changes(changes) + ".")
                summary = [f"Saved {len(facts)} record{'s' if len(facts) != 1 else ''} dated "
                           f"{proposal['result']['as_of']}."]
                if receipt is not None:
                    summary.append(ledger_view["plain"])
                if gone:
                    summary.append("No longer on this statement: " + ", ".join(
                        f"{g['symbol']} ({g['quantity']} units)" for g in gone) + ".")
                report = {
                    "status": "saved",
                    "result": {
                        "summary": " ".join(summary),
                        "saved": {"keys": [w["key"] for w in saved["written"]],
                                  "client_revision": saved["client"]["revision"],
                                  "expires_on": packet["result"]["expires_on"]},
                        "needs_user": saved["needs_user"],
                        "ledger": ledger_view,
                        "reconciliation": {"adjustments": changes, "missing_positions": gone},
                        "statement_prices": mapping["prices"],
                        "picture_after": situation_module.picture_delta(before, after,
                                                                        after["profile"].get("language")),
                        "next_step": ("To value these holdings, run task=ledger view=household with currency and "
                                      "prices=result.statement_prices, then task=exposure with that household."),
                    },
                    "missing": [], "warnings": packet["warnings"] + saved.get("warnings", []) + mapping["notes"],
                    "sources": packet["sources"], "assumptions": packet["assumptions"],
                }
                now = datetime.now(timezone.utc).isoformat()

                def update(old):
                    state = {k: dict(old.get(k) or {}) for k in ("pending", "extractions", "confirmed")}
                    state["pending"].pop(proposal_id, None)
                    state["confirmed"][proposal_id] = {"proposal": proposal, "created_at": now,
                                                       "batch": mapping["batch"], "held": ledger_view["held"],
                                                       "report": report}
                    state["confirmed"] = dict(sorted(state["confirmed"].items(),
                                                     key=lambda item: item[1].get("created_at", ""))[-_KEEP_PROPOSALS:])
                    return state

                store.update_auxiliary(client_id, "ingest", update)
        sha = ((proposal.get("result") or {}).get("provenance") or {}).get("sha256")
        if sha and purge_on_confirm():
            try:
                report["result"]["upload_removed"] = bool(purge_uploads(client_id, self.db_path, sha256=sha))
            except OSError:
                report["result"]["upload_removed"] = False
        return report

    @staticmethod
    def _reconcile_to_statement(store, client_id, proposal, mapping, receipt, ledger_before, batch_id,
                                is_newest, reconciliation_lines) -> list[dict]:
        """Post labelled adjustments for balance checks the statement's own lines do not explain.

        Only for accounts the ledger already had, when this statement is at least
        as new as anything the ledger knows about them, and when none of their
        lines are held as possible duplicates (those may explain the gap).
        """
        from . import ledger as ledger_module
        as_of = proposal["result"]["as_of"]
        known = {e["account_id"] for e in ledger_before.get("entries") or []}
        held_lines = {item["line"] for item in receipt.get("held") or []}
        held_accounts = {line.get("account_id") for index, line in enumerate(mapping["batch"]["transactions"])
                         if index in held_lines}
        accounts = {a["id"] for a in mapping["batch"]["accounts"]
                    if a["id"] in known and a["id"] not in held_accounts and is_newest(ledger_before, a["id"], as_of)}
        if not accounts:
            return []
        ledger_now = store.ledger(client_id)
        instruments = {i["id"]: i for i in ledger_now.get("instruments") or []}
        lines, changes = reconciliation_lines(receipt["reconciliation"]["breaks"], accounts, instruments)
        if not lines:
            return []
        source = dict(mapping["batch"]["source"])
        ledger_module.post(store, client_id, {"batch_id": batch_id + ":reconcile", "source": source,
                                              "transactions": lines})
        asserted = set(receipt["assertions"]["added"]) | set(receipt["assertions"]["existing"])
        ledger_now = store.ledger(client_id)
        check = ledger_module.reconcile(dict(ledger_now, assertions=[a for a in ledger_now["assertions"]
                                                                     if a["id"] in asserted]))
        receipt["reconciliation"] = {"status": check["status"], "breaks": check["result"]["breaks"],
                                     "checks": len(check["result"]["checks"])}
        return changes

    def _ingest_confirm_duplicates(self, client_id: str, proposal_id: str, entry_ids: list) -> dict:
        from . import ledger as ledger_module
        from .ledger.model import normalize_batch
        if not isinstance(entry_ids, list) or not entry_ids or not all(isinstance(e, str) for e in entry_ids):
            raise ValueError("entry_ids must be a nonempty list of held entry ids")
        record = (self._ingest_state(client_id).get("confirmed") or {}).get(proposal_id)
        if record is None or not record.get("batch"):
            raise ValueError("proposal_id has no confirmed ledger posting; confirm the proposal first")
        held = {item["entry_id"] for item in record.get("held") or []}
        unknown = sorted(set(entry_ids) - held)
        if unknown:
            raise ValueError(f"entry_ids {unknown} were not held for this proposal")
        batch = record["batch"]
        normalized, _ = normalize_batch(batch)
        lines = [dict(batch["transactions"][index], confirm_not_duplicate=True)
                 for index, entry in enumerate(normalized["transactions"]) if entry["id"] in entry_ids]
        suffix = hashlib.sha256(",".join(sorted(entry_ids)).encode()).hexdigest()[:12]
        repost = {"batch_id": f"ingest:{proposal_id}:not-duplicate:{suffix}", "source": batch["source"],
                  "accounts": batch["accounts"], "instruments": batch["instruments"], "transactions": lines}
        with WealthStore(self.db_path) as store:
            receipt = ledger_module.post(store, client_id, repost)
        remaining = [item for item in record["held"] if item["entry_id"] not in receipt["posted"]]

        def update(old):
            state = {k: dict(old.get(k) or {}) for k in ("pending", "extractions", "confirmed")}
            if proposal_id in state["confirmed"]:
                state["confirmed"][proposal_id] = {**state["confirmed"][proposal_id], "held": remaining}
            return state

        self._ingest_state(client_id, update)
        return {"status": "saved",
                "result": {"summary": f"Posted {len(receipt['posted'])} line(s) the person confirmed are separate "
                                      f"transactions; {len(remaining)} still held.",
                           "posted": receipt["posted"], "still_held": remaining},
                "missing": [], "warnings": receipt.get("warnings", []), "sources": [], "assumptions": []}

    def _ingest_connector(self, client_id: str, name: str, query_id: str | None = None, owner_id: str = "self",
                          sic_listed: list | dict | None = None) -> dict:
        """Pull a read-only connector (e.g. IBKR Flex) into a held proposal; confirm saves it, as for a file.

        The credential is read by the connector from the OS keychain or its
        environment variable; it is never an input, never stored and never shown.
        """
        from . import connectors
        if not isinstance(name, str) or name not in connectors.names():
            raise ValueError(f"name must be one of {', '.join(connectors.names())}")
        if query_id is None:
            raise ValueError(f"connector {name} needs query_id (the Activity Flex Query id from the IBKR portal)")
        if sic_listed is not None and not isinstance(sic_listed, (list, dict)):
            raise ValueError("sic_listed must be a list of symbols or {symbol: true|false}")
        instance = connectors.connector(name, query_id=query_id, sic_listed=sic_listed)
        state = self._ingest_state(client_id)
        earlier = [(record.get("created_at", ""), record["proposal"]) for record in (state.get("confirmed") or {}).values()
                   if (record["proposal"].get("result", {}).get("provenance") or {}).get("ref") == instance.ref]
        previous = max(earlier, key=lambda item: item[0])[1] if earlier else None
        proposal = instance.proposal(owner_id=owner_id, previous=previous)
        shown = self._hold(client_id, proposal)
        if previous is not None and "changes" in shown.get("result", {}):
            shown["result"]["previous_proposal_id"] = previous["result"]["proposal_id"]
        return shown

    def _ingest_connector_status(self, client_id: str, name: str | None = None) -> dict:
        """Which connectors have a credential available and when each last synced (never the credential)."""
        from . import connectors
        if name is not None and name not in connectors.names():
            raise ValueError(f"name must be one of {', '.join(connectors.names())}")
        state = self._ingest_state(client_id)
        rows = []
        for entry in connectors.status(name):
            synced = []
            for bucket in ("confirmed", "pending"):
                for pid, record in (state.get(bucket) or {}).items():
                    result = record["proposal"].get("result") or {}
                    provenance = result.get("provenance") or {}
                    if provenance.get("provider") == entry["name"]:
                        synced.append({"proposal_id": pid, "as_of": result.get("as_of"), "confirmed": bucket == "confirmed",
                                       "query_id": provenance.get("query_id"), "pulled_at": record.get("created_at")})
            synced.sort(key=lambda item: item["pulled_at"] or "")
            rows.append({**entry, "last_sync": synced[-1] if synced else None})
        return {"status": "ready", "result": {"connectors": rows}, "missing": [], "warnings": [], "sources": [],
                "assumptions": ["Connectors pull only when asked; nothing runs in the background."]}

    def _ingest_diff(self, client_id: str, proposal_id: str, previous_proposal_id: str | None = None) -> dict:
        from .ingest import diff_proposals
        state = self._ingest_state(client_id)
        known = {**(state.get("confirmed") or {}), **(state.get("pending") or {})}
        if proposal_id not in known:
            raise ValueError("proposal_id is unknown or expired")
        current = known[proposal_id]["proposal"]
        if previous_proposal_id is not None:
            if previous_proposal_id not in known:
                raise ValueError("previous_proposal_id is unknown or expired")
            previous_id = previous_proposal_id
        else:
            accounts = {a["id"] for a in current["result"]["household"]["accounts"]}
            earlier = [(record.get("created_at", ""), pid) for pid, record in (state.get("confirmed") or {}).items()
                       if pid != proposal_id and accounts & {a["id"] for a in record["proposal"]["result"]["household"]["accounts"]}]
            previous_id = max(earlier)[1] if earlier else None
        previous = known[previous_id]["proposal"] if previous_id else None
        changes = diff_proposals(previous, current)
        return {"status": "ready",
                "result": {"proposal_id": proposal_id, "previous_proposal_id": previous_id, "changes": changes},
                "missing": [], "warnings": [] if previous_id else ["No earlier confirmed statement covers these accounts; every item is new."],
                "sources": [], "assumptions": []}


def _retire_forgotten_accounts(store: WealthStore, client_id: str, receipt: dict) -> dict | None:
    """Reverse the ledger entries of statement accounts this write forgot (inside the caller's transaction)."""
    from . import ledger as ledger_module
    forgotten = [w for w in receipt.get("written") or []
                 if w.get("action") == "forget" and w["key"].startswith("account.") and w["key"].count(".") == 1]
    if not forgotten:
        return None
    account_ids = {}
    for write in forgotten:
        account_id = write["key"].split(".", 1)[1]
        for row in reversed(store.history(client_id, write["key"])):
            value = row.get("value")
            if isinstance(value, dict) and isinstance(value.get("account"), dict) and value["account"].get("id"):
                account_id = value["account"]["id"]
                break
        account_ids[account_id] = write
    ledger = store.ledger(client_id)
    entries = ledger.get("entries") or []
    reversed_ids = {e.get("reverses_id") for e in entries if e.get("kind") == "reversal"}
    today = datetime.now(timezone.utc).date().isoformat()
    result = {"reversed": 0, "accounts": sorted(account_ids)}
    for account_id, write in sorted(account_ids.items()):
        lines = [{"kind": "reversal", "account_id": account_id, "date": max(today, e["date"]),
                  "reverses_id": e["id"], "description": "Account removed from the profile"}
                 for e in entries if e.get("account_id") == account_id and e.get("kind") != "reversal"
                 and e["id"] not in reversed_ids]
        if not lines:
            continue
        posted = ledger_module.post(store, client_id, {
            "batch_id": f"forget:{write['key']}:{write['id']}",
            "source": {"kind": "user", "ref": f"removed {write['key']} from the profile", "observed_on": today},
            "transactions": lines})
        result["reversed"] += len(posted["posted"])
    return result


def _ledger_summary(receipt: dict | None, mapping: dict) -> dict:
    """The ledger receipt in the terms a person needs: what was added, skipped, or needs a yes."""
    batch = mapping.get("batch") or {}
    lines = {i: line for i, line in enumerate(batch.get("transactions", []))}
    if receipt is None:
        return {"posted": 0, "already_recorded": 0, "held": [], "not_posted": mapping["not_posted"],
                "reconciliation": None, "plain": "Nothing could be posted to the transaction ledger."}
    held = [{"entry_id": item["id"], "date": lines.get(item["line"], {}).get("date"),
             "description": lines.get(item["line"], {}).get("description"),
             "amount": lines.get(item["line"], {}).get("amount"), "matches": item["matches"]}
            for item in receipt["held"]]
    recon = receipt.get("reconciliation")
    parts = [f"Ledger: {len(receipt['posted'])} new line(s)"]
    if receipt["duplicates"]:
        parts.append(f"{len(receipt['duplicates'])} already recorded")
    if held:
        parts.append(f"{len(held)} held as possible duplicates (ask the person)")
    if mapping["not_posted"]:
        parts.append(f"{len(mapping['not_posted'])} not posted")
    text = ", ".join(parts) + "."
    if recon:
        text += (" Balances agree with the statement." if recon["status"] == "ready"
                 else f" {len(recon['breaks'])} balance(s) disagree with the statement.")
    return {"posted": len(receipt["posted"]), "already_recorded": len(receipt["duplicates"]), "held": held,
            "not_posted": mapping["not_posted"], "reconciliation": recon, "plain": text}


OPERATIONS = ("context", "run", "remember", "recall", "decision", "ingest", "client", "forget",
              "history", "contradictions", "resolve_contradiction")


def dispatch(operation: str, arguments: dict, db_path: str | Path | None = None) -> dict:
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation {operation!r}; operations are {', '.join(OPERATIONS)}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    service = WealthService(db_path)
    return _call(getattr(service, operation), operation, arguments)
