from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from .base import AgentError, AgentRequest
from .codebuddy import CodeBuddyBackend
from .codex import CodexBackend
from .config import AgentConfig, BACKENDS, atomic_json
from .options import select_model, select_effort
from .permissions import select_permission

FACTORIES = {"codex": CodexBackend, "codebuddy": CodeBuddyBackend}


class AgentService:
    def __init__(self, root: Path):
        self.config = AgentConfig(root)
        self.running = {}
        self.questions = {}
        self.catalogs = {}
        self.catalog_tasks = {}

    def backend_for(self, session_id: str | None = None) -> str:
        saved = self.config.session(session_id) if session_id else {}
        return saved.get("backend") or self.config.settings()["backend"]

    def bind(self, session_id: str, backend: str, *, permission_mode: str = "") -> None:
        # Used for the compatibility OpenClaw route as well as native sessions.
        with self.config.lock(session_id):
            if not self.config.session(session_id):
                atomic_json(self.config.session_path(session_id), {"backend": backend, "permissionMode": permission_mode})

    def selection(self, session_id: str | None = None) -> dict:
        saved = self.config.session(session_id) if session_id else {}
        settings = self.config.settings()
        backend = saved.get("backend") or settings["backend"]
        return {"backend": backend, "model": saved.get("model", settings["models"].get(backend, "")),
                "reasoningEffort": saved.get("reasoningEffort", settings["reasoningEfforts"].get(backend, "")),
                "permissionMode": saved.get("permissionMode", "") if saved else settings["permissionModes"].get(backend, "")}

    def status(self) -> dict:
        settings = self.config.settings()
        rows = []
        for key, label in BACKENDS.items():
            try:
                command = self.config.command(key)
                installed = True
            except AgentError:
                command, installed = [], False
            rows.append({"id": key, "name": label, "installed": installed,
                         "command": command, "model": settings["models"].get(key, ""),
                         "loginCommand": self.config.login_hint(key) if installed and key in FACTORIES else "",
                         "permissionMode": settings["permissionModes"].get(key, ""),
                         "reasoningEffort": settings["reasoningEfforts"].get(key, "")})
        return {**settings, "backends": rows}

    async def discover(self, backend: str, model: str = "", *, refresh=False) -> dict:
        if backend not in BACKENDS:
            raise AgentError("不支持的 Agent 后端")
        if backend == "openclaw":
            return {"backend": backend, "available": True, "models": [], "defaultModel": "",
                    "selectedModel": "", "reasoningOptions": [], "defaultReasoningEffort": "",
                    "permissionOptions": [], "defaultPermissionMode": "",
                    "detail": "OpenClaw 使用网关中的模型与思考配置"}
        key = (backend, model)
        cached = self.catalogs.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < (60 if cached[1]["available"] else 5):
            return cached[1]
        async def discover():
            instance = FACTORIES[backend](self.config)
            try:
                data = await asyncio.wait_for(instance.discover(model), 50)
                result = {**data, "backend": backend, "available": True, "detail": "选项来自本地 CLI"}
            except (AgentError, asyncio.TimeoutError) as exc:
                result = {"backend": backend, "available": False, "models": [], "defaultModel": "",
                          "selectedModel": model, "reasoningOptions": [], "defaultReasoningEffort": "",
                          "permissionOptions": [], "defaultPermissionMode": "",
                          "detail": str(exc) or "读取 CLI 模型列表超时"}
            finally:
                await instance.close()
            self.catalogs[key] = (time.monotonic(), result)
            return result
        if key not in self.catalog_tasks:
            task = asyncio.create_task(discover())
            self.catalog_tasks[key] = task
            task.add_done_callback(lambda _: self.catalog_tasks.pop(key, None))
        return await asyncio.shield(self.catalog_tasks[key])

    async def probe(self, backend: str) -> dict:
        if backend not in FACTORIES:
            raise AgentError("OpenClaw 请使用现有网关自测")
        instance = FACTORIES[backend](self.config)
        try:
            return await asyncio.wait_for(instance.probe(), 50)
        except (AgentError, asyncio.TimeoutError) as exc:
            return {"ready": False, "authStatus": "unknown", "detail": str(exc) or "连接超时"}
        finally:
            await instance.close()

    async def configure(self, backend, model="", effort="", *, make_default=False, permission_mode=None):
        previous = self.config.settings()
        changed = model != previous["models"].get(backend, "") or effort != previous["reasoningEfforts"].get(backend, "")
        permission_changed = permission_mode is not None and permission_mode != previous["permissionModes"].get(backend, "")
        if (changed and (model or effort)) or (permission_changed and permission_mode):
            catalog = await self.discover(backend, model)
            if not catalog["available"]:
                raise AgentError(catalog["detail"])
            select_model(catalog, model)
            select_effort(catalog, effort)
            if permission_mode:
                select_permission(catalog, permission_mode)
        self.config.save(backend, model, effort, make_default=make_default, permission_mode=permission_mode)
        return self.status()

    async def ask(self, session_id, items, emit):
        if not items:
            raise AgentError("Agent 提交了空问题")
        qid = "agent_" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.questions[qid] = {"session": session_id, "items": items, "future": future}
        emit("question", {"id": qid, "questions": items})
        try:
            return await future
        finally:
            self.questions.pop(qid, None)

    def answer(self, question_id: str, answers: dict):
        pending = self.questions.get(question_id)
        if not pending or pending["future"].done():
            raise AgentError("问题已结束或已失效")
        cleaned = {}
        for item in pending["items"]:
            values = answers.get(item["questionId"])
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                raise AgentError("请回答每个问题")
            if not item.get("multiSelect") and len(values) != 1:
                raise AgentError("此问题只能选择一项")
            if item.get("allowCustom") is False:
                allowed = {o["label"] for o in item.get("options", [])}
                if any(v not in allowed for v in values):
                    raise AgentError("请选择列出的许可选项")
            cleaned[item["questionId"]] = values
        pending["future"].set_result(cleaned)

    async def run(self, request: AgentRequest, emit, ask=None):
        if request.session_id in self.running:
            raise AgentError("此会话正在运行")
        with self.config.lock(request.session_id):
            saved = self.config.session(request.session_id)
            backend = saved.get("backend") or self.config.settings()["backend"]
            if request.backend:
                if saved.get("backend") and saved["backend"] != request.backend:
                    raise AgentError("切换 Agent 请新建对话，原会话已保留")
                backend = request.backend
            if backend not in FACTORIES:
                raise AgentError("此会话使用 OpenClaw，请通过网关入口继续")
            settings = self.config.settings()
            if request.model is None:
                request.model = saved.get("model", settings["models"].get(backend, ""))
            if request.reasoning_effort is None:
                request.reasoning_effort = saved.get("reasoningEffort", settings["reasoningEfforts"].get(backend, ""))
            if request.permission_mode is None:
                # Old conversations keep the standard mode, even if the new-chat default is elevated.
                request.permission_mode = saved.get("permissionMode", "") if saved else settings["permissionModes"].get(backend, "")
            instance = FACTORIES[backend](self.config)
            saved.update(backend=backend, model=request.model, reasoningEffort=request.reasoning_effort,
                         permissionMode=request.permission_mode,
                         state="running", updatedAt=time.time())
            atomic_json(self.config.session_path(request.session_id), saved)

            def save_session(native_id):
                saved["nativeId"] = native_id
                atomic_json(self.config.session_path(request.session_id), saved)

            task = asyncio.current_task()
            self.running[request.session_id] = (instance, task)
            try:
                ask_callback = ask or (lambda items: self.ask(request.session_id, items, emit))
                await asyncio.wait_for(instance.run(request, saved.get("nativeId"), emit,
                                                    ask_callback, save_session), request.timeout)
                saved["state"] = "completed"
            except asyncio.CancelledError:
                saved["state"] = "cancelled"
                await instance.cancel()
                raise
            except asyncio.TimeoutError as exc:
                saved["state"] = "failed"
                await instance.cancel()
                raise AgentError("Agent 本轮超时，已停止；会话保留，可继续追问") from exc
            except Exception:
                saved["state"] = "failed"
                raise
            finally:
                try:
                    await instance.close()
                finally:
                    self.running.pop(request.session_id, None)
                    saved["updatedAt"] = time.time()
                    atomic_json(self.config.session_path(request.session_id), saved)

    async def stop(self, session_id: str) -> bool:
        running = self.running.get(session_id)
        if not running:
            return False
        _, task = running
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def close(self):
        await asyncio.gather(*(self.stop(key) for key in list(self.running)), return_exceptions=True)
        for task in list(self.catalog_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.catalog_tasks.values()), return_exceptions=True)
