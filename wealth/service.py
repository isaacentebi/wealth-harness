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

from .store import DEFAULT_REVIEW_DAYS, REVIEW_DAYS, WealthStore, is_stale
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
}
# Tasks answered by the service itself rather than one module.
SERVICE_TASKS = ("plan", "calendar", "monitor")
TASKS = (*TASK_MODULES, *SERVICE_TASKS)
# Tasks whose module reads the client's transaction ledger from context["ledger"].
LEDGER_TASKS = frozenset({"ledger", "performance", "spending", "dca"})
INGEST_ACTIONS = ("file", "extraction", "chat", "confirm", "confirm_duplicates", "diff")
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


def capabilities() -> dict:
    from .catalog import CATALOG
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
        "monitoring": "Saved opt-in rules evaluated by the host or wealth watch. Unchanged checks stay quiet; no process starts automatically.",
        "fact_contract": fact_contract(),
    }


def fact_contract() -> dict:
    today = datetime.now(timezone.utc).date()
    return {
        "fields": ["key", "value", "source", "confidence", "expires_on", "merge"],
        "source": {"kind": "user|document|web|tool|inference", "ref": "actual source reference (URL for web)",
                   "observed_on": "YYYY-MM-DD"},
        "source_kinds": "user: only what the person said themselves in this conversation. document: a file or "
                        "statement they supplied. web: a page you read (ref is its URL). tool: a Wealth result. "
                        "inference: your own interpretation. Document/web facts for goals, profile, preferences, "
                        "constraints or tax profile are saved as inferred until the person confirms them.",
        "confidence": "confirmed: the person explicitly confirmed it | reported (default): the person stated it "
                      "or a document shows it | inferred: an interpretation. Only a user source may be confirmed.",
        "keys": ["client.profile", "household", "portfolio.snapshot", "goals", "plan.resources", "income.schedule",
                 "constraint.*", "preference.*", "thesis.*", "research.<SYMBOL>", "planning.project",
                 "planning.income", "planning.ladder", "planning.dca", "tax.profile", "monitor.rules",
                 "account.<id>", "liability.<id>", "income.<id>"],
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

    def remember(self, client_id: str, facts: list[dict], expected_revision: int | None = None,
                 request_id: str | None = None) -> dict:
        with WealthStore(self.db_path) as store:
            return store.remember(client_id, facts, expected_revision, request_id)

    def prepare(self, client_id: str, intent: str = "overview") -> dict:
        """Workflow packet (plan, exposure or income) from remembered facts; used by the examples."""
        with WealthStore(self.db_path) as store:
            return prepare(store.snapshot(client_id), intent)

    def context(self, client_id: str | None = None, intent: str = "overview", query: str = "") -> dict:
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
        if client_id:
            with WealthStore(self.db_path) as store:
                snapshot = store.snapshot(client_id)
                if task in LEDGER_TASKS and "ledger" not in inputs:
                    ledger = store.ledger(client_id)
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
        if task in {"plan", "calendar"}:
            # Direct inputs may supply the same canonical facts without requiring a profile.
            keys = ("plan.resources", "goals") if task == "plan" else ("income.schedule",)
            working = {**snapshot, "facts": [f for f in snapshot["facts"] if f["key"] in {*keys, "client.profile"}]}
            for key in keys:
                if key in inputs:
                    working["facts"] = [f for f in working["facts"] if f["key"] != key]
                    working["facts"].append({"id": "request:" + key, "key": key, "value": inputs[key],
                        "confidence": "reported", "source": {"kind": "user", "ref": "current request", "observed_on": today},
                        "expires_on": today, "revision": 0})
            packet = prepare(working, "plan" if task == "plan" else "income")
            report = {"status": packet["status"], "result": packet["calculations"],
                      "missing": packet["missing"], "warnings": packet["warnings"],
                      "sources": packet["evidence_ids"], "assumptions": []}
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
        if task in {"plan", "calendar"}:
            used_ids = set(packet["evidence_ids"])
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
        if task in {"plan", "calendar"}:
            relevant = set(keys)
        else:
            relevant = {f["key"] for f in snapshot["facts"]} if task == "monitor" else context.requested
        for fact in snapshot["facts"]:
            if fact["key"] in relevant and fact["key"] not in inputs and is_stale(fact):
                report.setdefault("warnings", []).append(
                    f"{fact['key']} is stale (observed {fact['source']['observed_on']}, review date "
                    f"{fact['expires_on']} passed) and was not used; reconfirm it with the person.")
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
                return {"client_id": client_id, "key": key, "history": store.history(client_id, key)}
            raise ValueError("detail must be current, export, or history with a key")

    def propose(self, client_id: str, title: str, rationale: str, expected_revision: int,
                evidence_ids: list[str], alternatives: list | None = None) -> dict:
        with WealthStore(self.db_path) as store:
            return store.save_decision(client_id, title, rationale, expected_revision,
                                       evidence_ids, alternatives)

    def resolve(self, client_id: str, decision_id: str, status: str,
                expected_revision: int | None = None) -> dict:
        with WealthStore(self.db_path) as store:
            return store.set_decision_status(client_id, decision_id, status, expected_revision)

    def forget(self, client_id: str, confirm_client_id: str) -> dict:
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
                    "diff": self._ingest_diff}
        if action not in handlers:
            raise ValueError(f"action must be one of {', '.join(INGEST_ACTIONS)}")
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
        from . import ledger as ledger_module
        from .ingest import proposal_to_facts
        from .ingest_posting import proposal_to_batch
        if not isinstance(acknowledge_discrepancies, bool):
            raise ValueError("acknowledge_discrepancies must be true or false")
        state = self._ingest_state(client_id)
        done = (state.get("confirmed") or {}).get(proposal_id)
        if done is not None:
            return {**done["report"], "replayed": True}
        stored = (state.get("pending") or {}).get(proposal_id)
        if stored is None:
            raise ValueError("proposal_id is unknown or expired; ingest the file or chat again and show the new summary")
        proposal = stored["proposal"]
        packet = proposal_to_facts(proposal, confirmed=True, proposal_id=proposal_id,
                                   acknowledge_discrepancies=acknowledge_discrepancies, expires_on=expires_on)
        if packet["status"] != "ready":
            return packet
        facts = packet["result"]["facts"]
        batch_id = "ingest:" + proposal_id
        with WealthStore(self.db_path) as store:
            revision = store.snapshot(client_id)["client"]["revision"]
            saved = store.remember(client_id, facts, revision, packet["result"]["request_id"])
            mapping = proposal_to_batch(proposal, batch_id=batch_id, ledger=store.ledger(client_id))
            receipt = ledger_module.post(store, client_id, mapping["batch"]) if mapping["batch"] else None
        ledger_view = _ledger_summary(receipt, mapping)
        summary = [f"Saved {len(facts)} record{'s' if len(facts) != 1 else ''} dated {proposal['result']['as_of']}."]
        if receipt is not None:
            summary.append(ledger_view["plain"])
        report = {
            "status": "saved",
            "result": {
                "summary": " ".join(summary),
                "saved": {"keys": [f["key"] for f in facts], "client_revision": saved["client"]["revision"],
                          "expires_on": packet["result"]["expires_on"]},
                "ledger": ledger_view,
                "statement_prices": mapping["prices"],
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
            state["confirmed"][proposal_id] = {"proposal": proposal, "created_at": now, "batch": mapping["batch"],
                                               "held": ledger_view["held"], "report": report}
            state["confirmed"] = dict(sorted(state["confirmed"].items(),
                                             key=lambda item: item[1].get("created_at", ""))[-_KEEP_PROPOSALS:])
            return state

        self._ingest_state(client_id, update)
        return report

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


OPERATIONS = ("context", "run", "remember", "recall", "decision", "ingest", "client", "forget")


def dispatch(operation: str, arguments: dict, db_path: str | Path | None = None) -> dict:
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation {operation!r}; operations are {', '.join(OPERATIONS)}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    service = WealthService(db_path)
    return _call(getattr(service, operation), operation, arguments)
