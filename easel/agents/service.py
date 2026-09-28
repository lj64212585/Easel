from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from .base import AgentError, AgentRequest
from .codebuddy import CodeBuddyBackend
from .codex import CodexBackend
from .config import AgentConfig, BACKENDS, atomic_json

FACTORIES = {"codex": CodexBackend, "codebuddy": CodeBuddyBackend}


class AgentService:
    def __init__(self, root: Path):
        self.config = AgentConfig(root)
        self.running = {}
        self.questions = {}

    def backend_for(self, session_id: str | None = None) -> str:
        saved = self.config.session(session_id) if session_id else {}
        return saved.get("backend") or self.config.settings()["backend"]

    def bind(self, session_id: str, backend: str) -> None:
        # Used for the compatibility OpenClaw route as well as native sessions.
        with self.config.lock(session_id):
            if not self.config.session(session_id):
                atomic_json(self.config.session_path(session_id), {"backend": backend})

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
                         "command": command, "model": settings["models"].get(key, "")})
        return {**settings, "backends": rows}

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
            if backend not in FACTORIES:
                raise AgentError("此会话使用 OpenClaw，请通过网关入口继续")
            request.model = request.model or saved.get("model", self.config.settings()["models"].get(backend, ""))
            instance = FACTORIES[backend](self.config)
            saved.update(backend=backend, model=request.model, state="running", updatedAt=time.time())
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
