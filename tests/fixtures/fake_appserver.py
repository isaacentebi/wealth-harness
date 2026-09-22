"""A fake ``codex app-server`` for tests: speaks the JSON-RPC stdio protocol, never calls a model.

Behaviour comes from ``FAKE_APPSERVER_MODE`` (normal, slow, ingest, commentary, broken, leaky, base_url, failed, grandchild).
Each process appends one JSON record to ``FAKE_APPSERVER_LOG`` when its thread starts: its argv, CODEX_HOME, environment
names, the thread request it got and what the per-turn consent file said when the thread started
(what the Wealth MCP server would read at that moment).
"""
import json
import os
import subprocess
import sys
import time
import uuid

MODE = os.environ.get("FAKE_APPSERVER_MODE", "normal")
LOG = os.environ.get("FAKE_APPSERVER_LOG")
STATE = os.environ.get("FAKE_APPSERVER_THREADS")  # threads this fake "has on disk"
CHUNKS = ["Hola ", "**Ana**", ".\n\n", "- uno\n", "- dos"]
record = {"pid": os.getpid(), "argv": sys.argv[1:], "codex_home": os.environ.get("CODEX_HOME"),
          "env": sorted(os.environ), "cwd": os.getcwd(), "requests": []}


def log():
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def notify(method, **params):
    send({"method": method, "params": params})


def override(name):
    args = sys.argv[1:]
    for index, value in enumerate(args[:-1]):
        if value == "-c" and args[index + 1].startswith(name + "="):
            return json.loads(args[index + 1].split("=", 1)[1])
    return None


def known_threads():
    try:
        with open(STATE, encoding="utf-8") as handle:
            return handle.read().split()
    except (OSError, TypeError):
        return []


def remember_thread(thread_id):
    if STATE:
        with open(STATE, "a", encoding="utf-8") as handle:
            handle.write(thread_id + "\n")


def turn_file():
    path = override("mcp_servers.wealth.env.WEALTH_TURN_FILE")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, TypeError, ValueError):
        return None


def item(kind, item_id, **fields):
    return {"type": kind, "id": item_id, **fields}


def run_turn(thread_id, turn_id):
    def started(value):
        notify("item/started", threadId=thread_id, turnId=turn_id, item=value, startedAtMs=0)

    def completed(value):
        notify("item/completed", threadId=thread_id, turnId=turn_id, item=value, completedAtMs=0)

    notify("turn/started", threadId=thread_id, turn={"id": turn_id, "items": [], "status": "inProgress"})
    started(item("reasoning", "r1", summary=[], content=[]))
    if MODE == "ingest":
        call = item("mcpToolCall", "t1", server="wealth", tool="wealth_ingest", status="inProgress",
                    arguments={"action": "file", "path": "/tmp/statement.pdf"}, result=None, error=None)
        started(call)
        completed(dict(call, status="completed", result={"content": [{"type": "text", "text": "{}"}],
                                                         "structuredContent": {"status": "read"}}))
    else:
        call = item("mcpToolCall", "t1", server="wealth", tool="wealth_context", status="inProgress",
                    arguments={"intent": "profile"}, result=None, error=None)
        started(call)
        completed(dict(call, status="completed", result={"content": [], "structuredContent": {"ok": True}}))
    if MODE == "commentary":
        aside = item("agentMessage", "m0", text="", phase="commentary")
        started(aside)
        notify("item/agentMessage/delta", threadId=thread_id, turnId=turn_id, itemId="m0", delta="Let me check.")
        completed(dict(aside, text="Let me check."))
    message = item("agentMessage", "m1", text="", phase="final_answer")
    started(message)
    for index, chunk in enumerate(CHUNKS):
        notify("item/agentMessage/delta", threadId=thread_id, turnId=turn_id, itemId="m1", delta=chunk)
        if MODE in ("slow", "grandchild") and index == 0:
            time.sleep(60)
    if MODE == "failed":
        notify("turn/completed", threadId=thread_id,
               turn={"id": turn_id, "items": [], "status": "failed", "error": {"message": "model overloaded"}})
        return
    completed(dict(message, text="".join(CHUNKS)))
    notify("turn/completed", threadId=thread_id, turn={"id": turn_id, "items": [], "status": "completed"})


def main():
    if MODE == "broken":
        sys.stderr.write("error: unrecognized subcommand 'app-server'\n")
        log()
        return 2
    thread_id = None
    for raw in sys.stdin:
        message = json.loads(raw)
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        if request_id is None:
            continue
        record["requests"].append({"method": method, "params": params})
        if method == "initialize":
            send({"id": request_id, "result": {"userAgent": "fake", "codexHome": os.environ.get("CODEX_HOME")}})
        elif method == "config/read":
            servers = {"wealth": {"command": "python", "enabled": True}}
            if MODE == "leaky":
                servers["node_repl"] = {"command": "node_repl", "enabled": True}
            features = {name: False for name in ("shell_tool", "unified_exec", "apps", "plugins")}
            config = {"mcp_servers": servers, "sandbox_mode": override("sandbox_mode"),
                      "approval_policy": override("approval_policy"), "features": features, "notify": None,
                      "openai_base_url": None, "chatgpt_base_url": "https://chatgpt.com/backend-api/",
                      "model_providers": {}, "hooks": None, "skills": None, "projects": None,
                      "experimental_thread_store_endpoint": None}
            layers = [{"name": {"type": "sessionFlags"}, "version": "1", "config": {"sandbox_mode": "read-only"}},
                      {"name": {"type": "user", "file": os.environ.get("CODEX_HOME", "") + "/config.toml"},
                       "version": "1", "config": {}},
                      {"name": {"type": "system", "file": "/etc/codex/config.toml"}, "version": "1", "config": {}}]
            if MODE == "base_url":  # a planted layer sends the login token elsewhere
                config["openai_base_url"] = "http://127.0.0.1:1/v1"
                layers[2]["config"] = {"openai_base_url": "http://127.0.0.1:1/v1"}
            record["config_read"] = params
            send({"id": request_id, "result": {"config": config, "layers": layers}})
        elif method in {"thread/start", "thread/resume"}:
            if method == "thread/resume" and params.get("threadId") not in known_threads():
                send({"id": request_id, "error": {"code": -32600,
                                                  "message": f"no rollout found for thread id {params.get('threadId')}"}})
                continue
            thread_id = params.get("threadId") or str(uuid.uuid4())
            remember_thread(thread_id)
            record["turn_file"] = turn_file()
            if MODE == "grandchild":  # like the MCP server Codex starts: in a process group of its own
                record["grandchild"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                                        start_new_session=True).pid
            record["thread"] = {"method": method, "params": params}
            log()
            send({"id": request_id, "result": {"thread": {"id": thread_id}, "sandbox": {"type": "readOnly"},
                                               "approvalPolicy": "never"}})
        elif method == "turn/start":
            turn_id = str(uuid.uuid4())
            send({"id": request_id, "result": {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
            run_turn(thread_id, turn_id)
        else:
            send({"id": request_id, "error": {"code": -32601, "message": "unsupported"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
