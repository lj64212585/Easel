"""Deterministic protocol peer: exercises pipes, callbacks, EOF and resume."""
import json
import os
import sys
import time

codex = "app-server" in sys.argv
scenario = os.environ.get("EASEL_TEST_SCENARIO", "normal")
native = "native-codex" if codex else "native-codebuddy"
pending = None
selected_model = "fake-fast"
selected_mode = "default"
PERMISSION_MODES = ["default", "acceptEdits", "plan", "bypassPermissions", "fullAccess", "delegate"]


def model_rows():
    return [{"model": "fake-fast", "displayName": "Fast", "isDefault": True,
             "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}],
             "defaultReasoningEffort": "low"},
            {"model": "fake-deep", "displayName": "Deep", "isDefault": False,
             "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "max"}],
             "defaultReasoningEffort": "high"}]


def acp_config():
    levels = ["low", "medium"] if selected_model == "fake-fast" else ["high", "max"]
    if scenario == "boolean":
        return [{"id": "thinking", "category": "thought_level", "type": "boolean", "currentValue": False}]
    permissions = [{"id": "mode", "category": "mode", "type": "select", "currentValue": selected_mode,
                    "options": [{"value": mode, "name": mode} for mode in PERMISSION_MODES]}] if scenario == "permission_config" else []
    return permissions + [{"id": "model", "category": "model", "type": "select", "currentValue": selected_model,
             "options": [{"value": row["model"], "name": row["displayName"]} for row in model_rows()]},
            {"id": "thought", "category": "thought_level", "type": "select", "currentValue": levels[0],
             "options": [{"value": level, "name": level} for level in levels]}]


def send(data):
    print(json.dumps(data), flush=True)


def result(rid, value):
    send({"id": rid, "result": value})


def notification(method, params):
    send({"method": method, "params": params})


def finish(rid=None):
    if codex:
        notification("item/agentMessage/delta", {"threadId": native, "turnId": "turn-1", "itemId": "text-1", "delta": "真实回复"})
        notification("item/completed", {"threadId": native, "turnId": "turn-1", "item": {"type": "agentMessage", "id": "text-1", "text": "真实回复"}})
        notification("turn/completed", {"threadId": native, "turn": {"id": "turn-1", "status": "completed"}})
    else:
        notification("session/update", {"sessionId": native, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "真实回复"}}})
        result(rid, {"stopReason": "end_turn"})


for line in sys.stdin:
    msg = json.loads(line)
    if os.environ.get("EASEL_TEST_TRANSCRIPT"):
        with open(os.environ["EASEL_TEST_TRANSCRIPT"], "a") as f:
            f.write(json.dumps(msg) + "\n")
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        print("Agent startup banner", flush=True)
        result(rid, {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
    elif method == "account/read":
        result(rid, {"account": {"type": "chatgpt"}})
    elif method == "model/list":
        rows = model_rows()
        if scenario == "paged":
            second = msg["params"].get("cursor") == "page2"
            result(rid, {"data": rows[1:] if second else rows[:1], "nextCursor": None if second else "page2"})
        else:
            result(rid, {"data": rows, "nextCursor": None})
    elif method == "config/read":
        result(rid, {"config": {"model": "fake-fast", "model_reasoning_effort": "low"}})
    elif method in ("thread/start", "thread/resume"):
        result(rid, {"thread": {"id": native}})
    elif method in ("session/new", "session/load"):
        if scenario == "unauthenticated":
            send({"id": rid, "error": {"code": -32000, "message": "Authentication required"}})
            continue
        if method == "session/load":
            notification("session/update", {"sessionId": native, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "旧消息不可重复显示"}}})
        # Resume reports wider permissions to verify the client reapplies the chosen mode.
        selected_mode = "fullAccess" if method == "session/load" else "default"
        modes = {"availableModes": [{"id": mode, "name": mode} for mode in PERMISSION_MODES], "currentModeId": selected_mode}
        result(rid, {"sessionId": native, "configOptions": acp_config(), "modes": modes})
    elif method == "session/set_mode":
        selected_mode = msg["params"]["modeId"]
        result(rid, {})
    elif method == "session/set_model":
        selected_model = msg["params"]["modelId"]
        result(rid, {})
    elif method == "session/set_config_option":
        if isinstance(msg["params"]["value"], bool) and msg["params"].get("type") != "boolean":
            send({"id": rid, "error": {"code": -32602, "message": "boolean type tag is required"}})
            continue
        if msg["params"]["configId"] == "model":
            selected_model = msg["params"]["value"]
        elif msg["params"]["configId"] == "mode":
            selected_mode = msg["params"]["value"]
        result(rid, {"configOptions": acp_config()})
    elif method in ("turn/start", "session/prompt"):
        if codex:
            result(rid, {"turn": {"id": "turn-1", "status": "inProgress"}})
        if scenario == "crash":
            sys.exit(3)
        if scenario == "delay":
            time.sleep(.3)
        if scenario == "hang":
            continue
        if scenario == "approval":
            pending = rid
            if codex:
                notification("item/agentMessage/delta", {"threadId": "foreign", "itemId": "foreign", "delta": "不属于本会话"})
                send({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {"threadId": native, "turnId": "turn-1", "command": "echo hello"}})
            else:
                send({"id": "approval", "method": "session/request_permission", "params": {"sessionId": native, "toolCall": {"title": "echo hello"}, "options": [{"optionId": "allow", "name": "允许", "kind": "allow_once"}, {"optionId": "reject", "name": "拒绝", "kind": "reject_once"}]}})
        else:
            finish(rid)
    elif rid == "approval" and method is None:
        finish(pending)
    elif method == "turn/interrupt":
        result(rid, {})
        notification("turn/completed", {"threadId": native, "turn": {"id": "turn-1", "status": "interrupted"}})
    elif method == "session/cancel":
        pass
