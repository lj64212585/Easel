"""Deterministic protocol peer: exercises pipes, callbacks, EOF and resume."""
import json
import os
import sys
import time

codex = "app-server" in sys.argv
scenario = os.environ.get("EASEL_TEST_SCENARIO", "normal")
native = "native-codex" if codex else "native-codebuddy"
pending = None


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
    elif method in ("thread/start", "thread/resume"):
        result(rid, {"thread": {"id": native}})
    elif method in ("session/new", "session/load"):
        if scenario == "unauthenticated":
            send({"id": rid, "error": {"code": -32000, "message": "Authentication required"}})
            continue
        if method == "session/load":
            notification("session/update", {"sessionId": native, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "旧消息不可重复显示"}}})
        result(rid, {"sessionId": native})
    elif method == "session/set_model":
        result(rid, {})
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
