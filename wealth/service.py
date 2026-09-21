"""One application boundary shared by the CLI and MCP transport."""
from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import importlib
import uuid

from .store import WealthStore
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
    "research": "research", "value": "research",
    "project": "planning", "income": "planning", "ladder": "planning", "tax": "tax",
}


def capabilities() -> dict:
    from .catalog import CATALOG
    return {
        "product": "wealth-harness", "release": "0.2.0", "tasks": CATALOG,
        "example_policy": "Catalog examples are fictional dated inputs, not current market evidence, recommended assumptions, or facts about this person. Missing fund constituents mean partial look-through coverage.",
        "workflow": "Recall the client; run a financial task; remember verified results; record the decision.",
        "memory": "Sourced facts, revisions, correction history, bounded keyword/concept recall and optional host-supplied semantic vectors.",
        "privacy": "Local plaintext SQLite. Retrieved context may reach the host's model provider. No credentials are stored.",
        "execution": "Analysis and decision support only; no trading, transfers, or external messaging.",
        "tax_scope": "US federal taxable securities and Mexican Article 129 qualifying listed shares; explicit inputs and coverage required.",
        "monitoring": "Saved opt-in rules evaluated by the host or wealth watch. Unchanged checks stay quiet; no process starts automatically.",
        "fact_contract": {
            "fields": ["key", "value", "source", "confidence", "expires_on"],
            "source": {"kind": "user|document|tool|inference", "ref": "actual source reference", "observed_on": "YYYY-MM-DD"},
            "confidence": "confirmed (user source only)|reported|inferred",
            "keys": ["client.profile", "household", "goals", "plan.resources", "constraint.*", "preference.*", "thesis.*", "research.<SYMBOL>", "planning.project", "planning.income", "planning.ladder", "tax.profile", "monitor.rules"],
            "freshness": "Use explicit expiries for material financial state. Stale and inferred inputs are excluded from calculations.",
            "review_policy": "For newly stated financial facts without a shorter supplied validity, use the provided default_review_on as a review deadline, not a prediction of continued accuracy. Do not extend old facts merely by recalling them.",
            "default_review_on": (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat(),
            "partial_values": "Incomplete canonical objects and goal entries may be remembered; omit unknown fields. Calculations remain unavailable until required fields are present. Never use zero for an unknown amount.",
            "structured_updates": "Before replacing a structured value, fetch the complete current fact using wealth_client inspect inputs.key; merge without dropping other fields or goals.",
        },
    }


class _Context(dict):
    """Record which remembered values a financial module actually consults."""
    def __init__(self, values, overridden=()):
        super().__init__(values)
        self.used = set()
        self.overridden = set(overridden)

    def get(self, key, default=None):
        if key in self and key not in self.overridden and key != "_evidence":
            self.used.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        if key in self and key not in self.overridden and key != "_evidence":
            self.used.add(key)
        return super().__getitem__(key)


class WealthService:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = database_path(db_path)

    def create(self, client_id: str, display_name: str) -> dict:
        with WealthStore(self.db_path) as store:
            return store.create_client(client_id, display_name)

    def remember(self, client_id: str, facts: list[dict], expected_revision: int,
                 request_id: str | None = None) -> dict:
        with WealthStore(self.db_path) as store:
            return store.remember(client_id, facts, expected_revision, request_id)

    def prepare(self, client_id: str, intent: str = "overview") -> dict:
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
        }
        result = self.recall(client_id, " ".join(filter(None, [query, routing.get(intent, intent)])))
        result["available_tasks"] = list(TASK_MODULES) + ["plan", "calendar", "monitor"]
        result["fact_contract"] = capabilities()["fact_contract"]
        with WealthStore(self.db_path) as store:
            facts = store.snapshot(client_id)["facts"]
        result["known_fact_keys"] = [f["key"] for f in facts[:50]]
        result["omitted_fact_keys"] = max(0, len(facts) - 50)
        result["next_step"] = "Use run for calculations; inspect a fact by key if its value was omitted."
        return result

    def recall(self, client_id: str, query: str = "", limit: int = 12,
               query_embedding: list | None = None, embedding_model: str | None = None,
               include_stale: bool = False) -> dict:
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
        if task not in {*TASK_MODULES, "plan", "calendar", "monitor"}:
            raise ValueError("unknown task; read context without a client for task schemas")
        if save_as and (not client_id or not expires_on):
            raise ValueError("saving a result requires client_id and expires_on")
        if save_as and not (task == "import" and save_as == "household"):
            if not save_as.startswith(("analysis.", "research.")):
                raise ValueError("save_as must be analysis.<name>, research.<symbol>, or household for an import")
        today = datetime.now(timezone.utc).date().isoformat()
        snapshot = {"client": {"id": None, "revision": None}, "facts": [], "decisions": []}
        if client_id:
            with WealthStore(self.db_path) as store:
                snapshot = store.snapshot(client_id)
        eligible = [f for f in snapshot["facts"] if f["confidence"] != "inferred"
                    and (not f.get("expires_on") or f["expires_on"] >= today)]
        context = _Context({f["key"]: f["value"] for f in eligible}, overridden=inputs.keys())
        if "household" in inputs:
            context["household"] = inputs["household"]
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
        if save_as:
            report["request_inputs"] = inputs
        if inputs:
            report.setdefault("assumptions", []).append("Explicit request inputs take precedence over remembered values; they are not saved unless requested.")
        if save_as and report.get("status") in {"ready", "partial"}:
            value = report["result"].get("household") if task == "import" and save_as == "household" else report
            if task != "import" and save_as == "household":
                raise ValueError("only a validated import may be saved as household")
            if value is None:
                raise ValueError("no validated value is available to save")
            derived_expiry = min([expires_on, *[f["expires_on"] for f in consumed if f.get("expires_on")]])
            with WealthStore(self.db_path) as store:
                saved = store.remember(client_id, [{"key": save_as, "value": value,
                    "source": {"kind": "tool", "ref": "wealth://" + task + "/" + uuid.uuid4().hex, "observed_on": today},
                    "confidence": "reported", "expires_on": derived_expiry}], snapshot["client"]["revision"])
            report["saved"] = {"key": save_as, "client_revision": saved["client"]["revision"], "expires_on": derived_expiry}
        return report

    def client(self, action: str, client_id: str, inputs: dict | None = None) -> dict:
        data = inputs or {}
        operations = {"create": self.create, "inspect": self.inspect, "export": lambda **kw: self.inspect(detail="export", **kw),
                      "forget": self.forget, "index": self.index}
        if action not in operations:
            raise ValueError("client action must be create,inspect,export,forget,index")
        return operations[action](client_id=client_id, **data)

    def decision(self, action: str, client_id: str, inputs: dict) -> dict:
        if action == "propose":
            return self.propose(client_id=client_id, **inputs)
        if action in {"accept", "dismiss"}:
            return self.resolve(client_id=client_id, status="accepted" if action == "accept" else "dismissed", **inputs)
        raise ValueError("decision action must be propose,accept,dismiss")

    def inspect(self, client_id: str, detail: str = "current", key: str | None = None) -> dict:
        with WealthStore(self.db_path) as store:
            if detail == "current":
                snapshot = store.snapshot(client_id)
                if key:
                    snapshot["facts"] = [f for f in snapshot["facts"] if f["key"] == key]
                    snapshot["decisions"] = []
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
                expected_revision: int) -> dict:
        with WealthStore(self.db_path) as store:
            return store.set_decision_status(client_id, decision_id, status, expected_revision)

    def forget(self, client_id: str, confirm_client_id: str) -> dict:
        with WealthStore(self.db_path) as store:
            return store.delete_client(client_id, confirm_client_id)


OPERATIONS = ("context", "run", "remember", "recall", "decision", "client", "capabilities", "create", "prepare", "inspect", "propose", "resolve", "forget", "index")


def dispatch(operation: str, arguments: dict, db_path: str | Path | None = None) -> dict:
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation: {operation}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    if operation == "capabilities":
        if arguments:
            raise ValueError("capabilities takes no arguments")
        return capabilities()
    return getattr(WealthService(db_path), operation)(**arguments)
