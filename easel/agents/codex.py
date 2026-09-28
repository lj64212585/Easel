from __future__ import annotations

import asyncio
import json

from .base import AgentBackend, AgentError
from .config import AgentConfig, context
from .rpc import RpcProcess


class CodexBackend(AgentBackend):
    def __init__(self, config: AgentConfig):
        self.config = config
        self.rpc = None
        self.native_id = None
        self.turn_id = None
        self.done = None
        self.items = {}

    async def connect(self):
        self.rpc = RpcProcess(self.config.command("codex") + ["app-server"],
                              self.config.root, self.config.environment(), jsonrpc=False)
        await self.rpc.start()
        await self.rpc.request("initialize", {
            "clientInfo": {"name": "easel", "version": "0.2.1"},
            "capabilities": {"experimentalApi": True},
        })
        await self.rpc.notify("initialized")

    async def probe(self):
        await self.connect()
        result = await self.rpc.request("account/read", {"refreshToken": False})
        account = result.get("account")
        authenticated = bool(account)
        return {"ready": authenticated, "authStatus": "authenticated" if authenticated else "required",
                "authMode": account.get("type") if account else None,
                "detail": "已连接 Codex，认证由本地 CLI 管理" if authenticated else "请先运行 python -m easel agent login codex"}

    async def run(self, request, native_id, emit, ask, save_session):
        await self.connect()
        self.done = asyncio.get_running_loop().create_future()
        self.native_id = native_id
        self.seen = {}
        self.rpc.on_notification = lambda method, params: self._event(method, params, emit)
        self.rpc.on_request = lambda method, params: self._request(method, params, ask)
        params = {"cwd": str(self.config.root), "approvalPolicy": "on-request",
                  "sandbox": "workspace-write", "developerInstructions": context(self.config.root)}
        if request.model:
            params["model"] = request.model
        if native_id:
            params["threadId"] = native_id
        result = await self.rpc.request("thread/resume" if native_id else "thread/start", params, timeout=60)
        self.native_id = result["thread"]["id"]
        save_session(self.native_id)
        emit("activity", "Codex 正在处理…")
        result = await self.rpc.request("turn/start", {
            "threadId": self.native_id, "input": [{"type": "text", "text": request.message}],
        }, timeout=60)
        self.turn_id = result["turn"]["id"]
        await self.rpc.wait_for(self.done)

    def _event(self, method, params, emit):
        if params.get("threadId") and params["threadId"] != self.native_id:
            return
        if self.turn_id and params.get("turnId") and params["turnId"] != self.turn_id:
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            key = params.get("itemId", "")
            self.seen[key] = self.seen.get(key, "") + delta
            emit("token", delta)
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            emit("thinking", params.get("delta", ""))
        elif method == "turn/started":
            self.turn_id = params.get("turn", {}).get("id", self.turn_id)
        elif method == "item/started":
            item = params.get("item", {})
            self.items[item.get("id", "")] = item
            if item.get("type") not in ("agentMessage", "userMessage", "reasoning"):
                emit("activity", "Codex · " + str(item.get("command") or item.get("tool") or item.get("type", "执行工具"))[:800])
        elif method == "item/completed":
            item = params.get("item", {})
            self.items[item.get("id", "")] = item
            if item.get("type") == "agentMessage":
                text = item.get("text", "")
                seen = self.seen.get(item.get("id", ""), "")
                if text.startswith(seen) and text != seen:
                    emit("token", text[len(seen):])
                self.seen[item.get("id", "")] = text
        elif method == "error":
            # Retries belong to Codex; only a terminal turn determines success.
            emit("activity", "Codex · " + str(params.get("error", {}).get("message", "连接恢复中"))[:800])
        elif method == "turn/completed" and not self.done.done():
            turn = params.get("turn", {})
            if self.turn_id and turn.get("id") != self.turn_id:
                return
            if turn.get("status") == "completed":
                self.done.set_result(None)
            else:
                error = turn.get("error") or {}
                self.done.set_exception(AgentError(error.get("message") or f"Codex 本轮状态：{turn.get('status', 'unknown')}"))

    async def _request(self, method, params, ask):
        if params.get("threadId") and params["threadId"] != self.native_id:
            raise AgentError("请求不属于当前会话")
        if self.turn_id and params.get("turnId") and params["turnId"] != self.turn_id:
            raise AgentError("请求不属于当前轮次")
        if method == "item/tool/requestUserInput":
            items = [{"questionId": q["id"], "header": q.get("header", ""),
                      "question": q["question"], "options": q.get("options") or [],
                      "allowCustom": True, "isSecret": q.get("isSecret", False)}
                     for q in params.get("questions", [])]
            answers = await ask(items)
            return {"answers": {key: {"answers": vals} for key, vals in answers.items()}}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                      "item/permissions/requestApproval"):
            item = self.items.get(params.get("itemId", ""), {})
            description = params.get("command") or item.get("command") or "文件或权限变更"
            for key in ("reason", "cwd", "grantRoot"):
                if params.get(key):
                    description += f"\n{key}: {params[key]}"
            if item.get("changes"):
                description += "\n" + json.dumps(item["changes"], ensure_ascii=False)
            if params.get("permissions"):
                description += "\n" + json.dumps(params["permissions"], ensure_ascii=False)
            answers = await ask([{
                "questionId": "decision", "header": "Codex 请求许可", "question": description,
                "allowCustom": False, "options": [{"label": "允许本次"}, {"label": "拒绝"}],
            }])
            allowed = answers.get("decision") == ["允许本次"]
            if method == "item/permissions/requestApproval":
                return {"permissions": params.get("permissions", {}) if allowed else {}, "scope": "turn"}
            return {"decision": "accept" if allowed else "decline"}
        raise AgentError(f"Easel 尚不支持此 Codex 交互：{method}")

    async def cancel(self):
        if self.rpc and self.native_id and self.turn_id:
            try:
                await self.rpc.request("turn/interrupt", {"threadId": self.native_id, "turnId": self.turn_id}, timeout=3)
            except AgentError:
                pass

    async def close(self):
        if self.rpc:
            await self.rpc.close()
        if self.done and self.done.done() and not self.done.cancelled():
            self.done.exception()  # consume a terminal error racing with cancellation
