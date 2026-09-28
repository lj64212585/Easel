from __future__ import annotations

import json

from .base import AgentBackend, AgentError
from .config import AgentConfig, context
from .rpc import RpcProcess
from .options import acp_catalog, select_model, select_effort


class CodeBuddyBackend(AgentBackend):
    def __init__(self, config: AgentConfig):
        self.config = config
        self.rpc = None
        self.native_id = None
        self.loading = False
        self.configuration = {}

    async def connect(self):
        self.rpc = RpcProcess(self.config.command("codebuddy") + ["--acp"],
                              self.config.root, self.config.environment())
        await self.rpc.start()
        self.capabilities = await self.rpc.request("initialize", {
            "protocolVersion": 1,
            # CodeBuddy executes files and commands itself. Do not claim client
            # filesystem/terminal capabilities that Easel does not implement.
            "clientCapabilities": {},
            "clientInfo": {"name": "easel", "version": "0.2.1"},
        }, timeout=45)

    async def probe(self):
        await self.connect()
        try:
            # Opening an empty session checks the login gate without a prompt
            # or a billable model request. It cannot verify remaining quota.
            await self.rpc.request("session/new", {"cwd": str(self.config.root), "mcpServers": []})
        except AgentError as exc:
            if "authentication" in str(exc).lower() or "auth required" in str(exc).lower():
                return {"ready": False, "authStatus": "required", "authMode": "cli",
                        "detail": "CodeBuddy 尚未登录。请运行 python -m easel agent login codebuddy；WorkBuddy 桌面登录不一定共享。"}
            raise
        return {"ready": True, "authStatus": "unknown", "authMode": "cli",
                "detail": "CodeBuddy ACP 会话已就绪；实际模型权限与账户额度仍以对话结果为准。"}

    async def _open_session(self, native_id=None):
        params = {"cwd": str(self.config.root), "mcpServers": []}
        if native_id:
            caps = self.capabilities.get("agentCapabilities", {})
            if not caps.get("loadSession"):
                raise AgentError("当前 CodeBuddy CLI 不支持恢复会话；请升级 CLI，原会话记录已保留")
            params["sessionId"] = native_id
        self.native_id = native_id
        self.loading = True
        try:
            result = await self.rpc.request("session/load" if native_id else "session/new", params, timeout=60)
        except AgentError as exc:
            if "authentication" in str(exc).lower() or "auth required" in str(exc).lower():
                raise AgentError("CodeBuddy 尚未登录。请运行 python -m easel agent login codebuddy，并按提示或用 /login 登录后重试；WorkBuddy 桌面登录不一定共享。") from exc
            raise
        finally:
            self.loading = False
        self.native_id = native_id or result["sessionId"]
        self.configuration = result

    def _capture_config(self, method, params):
        if method != "session/update" or (self.native_id and params.get("sessionId") != self.native_id):
            return
        update = params.get("update", {})
        if update.get("sessionUpdate") == "config_option_update":
            self.configuration["configOptions"] = update.get("configOptions", [])
        elif update.get("sessionUpdate") == "model_update":
            self.configuration["models"] = update.get("models", update)

    async def _select_model(self, model):
        catalog = acp_catalog(self.configuration)
        selected = select_model(catalog, model)
        if selected:
            if catalog.get("modelConfigId"):
                result = await self.rpc.request("session/set_config_option", {
                    "sessionId": self.native_id, "configId": catalog["modelConfigId"], "value": selected})
            else:
                result = await self.rpc.request("session/set_model", {"sessionId": self.native_id, "modelId": selected})
            if isinstance(result, dict):
                self.configuration.update(result)
        return selected

    async def discover(self, model=""):
        await self.connect()
        self.rpc.on_notification = self._capture_config
        await self._open_session()
        initial = acp_catalog(self.configuration)
        default = initial["defaultModel"]
        if model and model not in {row["id"] for row in initial["models"]}:
            return {**initial, "selectedModel": model, "reasoningOptions": [], "defaultReasoningEffort": ""}
        if model:
            await self._select_model(model)
        catalog = acp_catalog(self.configuration, model)
        catalog["defaultModel"] = default
        return catalog

    async def run(self, request, native_id, emit, ask, save_session):
        await self.connect()
        self.rpc.on_notification = lambda method, params: self._event(method, params, emit)
        self.rpc.on_request = lambda method, params: self._request(method, params, ask)
        await self._open_session(native_id)
        save_session(self.native_id)
        if request.model:
            await self._select_model(request.model)
        catalog = acp_catalog(self.configuration, request.model or "")
        effort = select_effort(catalog, request.reasoning_effort or "")
        if effort and catalog.get("reasoningConfigId"):
            value = effort == "true" if catalog.get("reasoningConfigType") == "boolean" else effort
            await self.rpc.request("session/set_config_option", {
                "sessionId": self.native_id, "configId": catalog["reasoningConfigId"], "value": value,
                **({"type": "boolean"} if isinstance(value, bool) else {})})
        # ACP has no standard system-prompt field. Include the authoritative
        # Easel context on each turn so resumed sessions pick up skill updates.
        prompt = context(self.config.root) + "\n\n# 当前用户消息\n" + request.message
        emit("activity", "CodeBuddy 正在处理…")
        result = await self.rpc.request("session/prompt", {
            "sessionId": self.native_id, "prompt": [{"type": "text", "text": prompt}],
        }, timeout=request.timeout)
        reason = result.get("stopReason")
        if reason != "end_turn":
            raise AgentError(f"CodeBuddy 本轮未正常完成：{reason or '缺少终止状态'}")

    def _event(self, method, params, emit):
        self._capture_config(method, params)
        if self.loading or method != "session/update" or params.get("sessionId") != self.native_id:
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            # A child agent's interleaved output is not the main response.
            meta = {**(update.get("_meta") or {}), **((update.get("content") or {}).get("_meta") or {})}
            if meta.get("codebuddy.ai/isSubagent") or meta.get("codebuddy.ai/parentToolCallId"):
                return
            content = update.get("content", {})
            if content.get("type") == "text":
                emit("token" if kind == "agent_message_chunk" else "thinking", content.get("text", ""))
        elif kind in ("tool_call", "tool_call_update"):
            emit("activity", "CodeBuddy · " + str(update.get("title") or update.get("status") or "执行工具")[:800])

    async def _request(self, method, params, ask):
        if method != "session/request_permission":
            raise AgentError(f"Easel 尚不支持此 ACP 交互：{method}")
        if params.get("sessionId") != self.native_id:
            raise AgentError("请求不属于当前会话")
        options = params.get("options", [])
        labels = {f"{i + 1}. {opt.get('name', opt['optionId'])}": opt["optionId"] for i, opt in enumerate(options)}
        tool = params.get("toolCall", {})
        details = str(tool.get("title") or "工具调用")
        if tool.get("rawInput"):
            details += "\n" + json.dumps(tool["rawInput"], ensure_ascii=False)
        answers = await ask([{"questionId": "decision", "header": "CodeBuddy 请求许可",
                              "question": details, "allowCustom": False,
                              "options": [{"label": label} for label in labels]}])
        selected = answers.get("decision", [])
        if len(selected) == 1 and selected[0] in labels:
            return {"outcome": {"outcome": "selected", "optionId": labels[selected[0]]}}
        return {"outcome": {"outcome": "cancelled"}}

    async def cancel(self):
        if self.rpc and self.native_id:
            try:
                await self.rpc.notify("session/cancel", {"sessionId": self.native_id})
            except AgentError:
                pass

    async def close(self):
        if self.rpc:
            await self.rpc.close()
