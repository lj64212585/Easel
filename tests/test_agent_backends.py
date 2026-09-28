from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from easel.agents import AgentError, AgentRequest, AgentService
from easel.agents.config import AgentConfig, context

PEER = Path(__file__).parent / "fixtures" / "agent_rpc.py"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("EASEL_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("EASEL_AGENT_STATE_DIR", raising=False)
    monkeypatch.setenv("EASEL_TEST_TRANSCRIPT", str(tmp_path / "wire.jsonl"))
    monkeypatch.setattr(AgentConfig, "command", lambda self, backend: [sys.executable, str(PEER)])
    service = AgentService(tmp_path)
    service.config.save("codex")
    return service


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
def test_real_pipes_resume_without_replaying_old_text(service, backend):
    async def run():
        service.config.save(backend)
        for _ in range(2):
            events = []
            # Recreate service to require durable native session recovery.
            fresh = AgentService(service.config.root)
            await fresh.run(AgentRequest("conversation", "hello", 5), lambda k, d: events.append((k, d)))
            assert "".join(d for k, d in events if k == "token") == "真实回复"
            assert not fresh.running
        assert service.config.session("conversation")["state"] == "completed"
    asyncio.run(run())
    records = [json.loads(line) for line in (service.config.root / "wire.jsonl").read_text().splitlines()]
    assert sum(r.get("method") == ("thread/start" if backend == "codex" else "session/new") for r in records) == 1
    assert sum(r.get("method") == ("thread/resume" if backend == "codex" else "session/load") for r in records) == 1


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
def test_approval_waits_and_rejects_forged_answer(service, monkeypatch, backend):
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "approval")
    async def run():
        service.config.save(backend)
        events = []
        task = asyncio.create_task(service.run(AgentRequest("approval-test", "hello", 5), lambda k, d: events.append((k, d))))
        for _ in range(100):
            if service.questions:
                break
            await asyncio.sleep(.01)
        qid, pending = next(iter(service.questions.items()))
        assert not task.done()
        with pytest.raises(AgentError):
            service.answer(qid, {"decision": ["forged auto approval"]})
        label = pending["items"][0]["options"][-1]["label"]
        service.answer(qid, {"decision": [label]})
        await task
        assert not service.questions
        assert not any("不属于" in str(d) for _, d in events)
    asyncio.run(run())
    records = [json.loads(line) for line in (service.config.root / "wire.jsonl").read_text().splitlines()]
    response = next(r for r in records if r.get("id") == "approval")
    assert response["result"] == ({"decision": "decline"} if backend == "codex" else {"outcome": {"outcome": "selected", "optionId": "reject"}})


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
def test_eof_is_failure_not_success(service, monkeypatch, backend):
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "crash")
    async def run():
        service.config.save(backend)
        with pytest.raises(AgentError, match="提前退出"):
            await service.run(AgentRequest("crash", "hello", 5), lambda *_: None)
        assert service.config.session("crash")["state"] == "failed"
        assert service.config.session("crash")["nativeId"]
        assert not service.running
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
def test_cancel_releases_process_lock_and_pending_questions(service, monkeypatch, backend):
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "approval")
    async def run():
        service.config.save(backend)
        task = asyncio.create_task(service.run(AgentRequest("cancel", "hello", 10), lambda *_: None))
        for _ in range(100):
            if service.questions:
                break
            await asyncio.sleep(.01)
        instance = service.running["cancel"][0]
        assert await service.stop("cancel")
        assert task.cancelled()
        assert instance.rpc.process.returncode is not None
        assert not service.questions and not service.running
        assert service.config.session("cancel")["state"] == "cancelled"
        with service.config.lock("cancel"):
            pass
    asyncio.run(run())


def test_existing_session_keeps_backend_and_model(service):
    async def run():
        await service.run(AgentRequest("bound", "hello", 5), lambda *_: None)
        service.config.save("codebuddy", "another-model")
        assert service.backend_for("bound") == "codex"
        assert service.backend_for("new") == "codebuddy"
        await service.run(AgentRequest("bound", "again", 5), lambda *_: None)
        assert service.config.session("bound")["model"] == ""
    asyncio.run(run())


def test_storage_ids_are_collision_safe_and_locks_are_exclusive(service):
    cfg = service.config
    assert cfg.session_path("a/b") != cfg.session_path("a_b")
    assert cfg.session_path("../../secret").parent == cfg.directory / "sessions"
    with cfg.lock("x"):
        with pytest.raises(AgentError):
            with cfg.lock("x"):
                pass


def test_config_validation_and_env_override(service, monkeypatch):
    with pytest.raises(AgentError):
        service.config.save("arbitrary-command")
    with pytest.raises(AgentError):
        service.config.save("codex", "two words")
    monkeypatch.setenv("EASEL_AGENT_BACKEND", "codex")
    with pytest.raises(AgentError, match="固定"):
        service.config.save("codebuddy")


def test_context_keeps_project_skills_and_profile_paths(tmp_path):
    skill = tmp_path / "skills/openclaw/test-skill/SKILL.md"
    skill.parent.mkdir(parents=True); skill.write_text("# Test")
    text = context(tmp_path)
    assert str(tmp_path) in text and "test-skill" in text and "profiles/" in text
    assert "无需运行 OpenClaw" in text


def test_codex_file_approval_includes_changes_and_rejects_foreign_turn(service):
    from easel.agents.codex import CodexBackend
    async def run():
        backend = CodexBackend(service.config)
        backend.native_id, backend.turn_id = "thread", "turn"
        backend._event("item/started", {"threadId": "thread", "item": {
            "id": "edit", "type": "fileChange", "changes": [{"path": "/tmp/easel-review.txt", "diff": "+review me"}],
        }}, lambda *_: None)
        async def ask(items):
            assert "/tmp/easel-review.txt" in items[0]["question"]
            assert "+review me" in items[0]["question"]
            return {"decision": ["拒绝"]}
        params = {"threadId": "thread", "turnId": "turn", "itemId": "edit"}
        assert await backend._request("item/fileChange/requestApproval", params, ask) == {"decision": "decline"}
        with pytest.raises(AgentError, match="轮次"):
            await backend._request("item/fileChange/requestApproval", {**params, "turnId": "foreign"}, ask)
    asyncio.run(run())


@pytest.fixture
def web_service(service, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
    import app as web
    monkeypatch.setattr(web, "AGENTS", service)
    monkeypatch.setattr(web, "SESSIONS_DIR", tmp_path / "web-sessions")
    monkeypatch.setattr(web, "_LOCAL_AGENT_TASKS", {})
    return web


def test_sse_disconnect_and_duplicate_post_do_not_resubmit(web_service, monkeypatch):
    web = web_service
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "delay")
    async def run():
        req = web.ChatRequest(message="hello", sessionId="web-test", turnId="once-only")
        response = await web.api_chat_stream(req)
        # Browser disconnects before reading the first event.
        await response.body_iterator.aclose()
        duplicate = await web.api_chat_stream(req)
        events = [event async for event in duplicate.body_iterator]
        assert events[-1]["event"] == "done"
        assert len([e for e in events if e["event"] == "token"]) == 1
        last = await web.api_chat_last("web-test", "once-only")
        assert last["text"] == "真实回复" and last["clean_end"]
        replay = await web.api_chat_stream(req)
        assert [e async for e in replay.body_iterator] == events
    asyncio.run(run())
    wire = (web.AGENTS.config.root / "wire.jsonl").read_text()
    assert wire.count('"method": "turn/start"') == 1


def test_legacy_session_is_not_silently_switched(web_service):
    web = web_service
    web._save_turn("web:old", "done", "old OpenClaw result")
    assert web._agent_backend_for("old") == "openclaw"
    assert web._agent_backend_for("new") == "codex"


def test_web_question_routes_and_stop(web_service, monkeypatch):
    web = web_service
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "approval")
    async def run():
        response = await web.api_chat_stream(web.ChatRequest(message="hello", sessionId="web-approval", turnId="approval-turn"))
        for _ in range(100):
            if web.AGENTS.questions:
                break
            await asyncio.sleep(.01)
        qid = next(iter(web.AGENTS.questions))
        status = await web.api_question_status(web.QuestionStatusRequest(questionIds=[qid]))
        assert status["questions"][qid]["status"] == "pending"
        assert (await web.api_chat_stop(web.StopRequest(sessionId="web-approval")))["stopped"]
        assert not web.AGENTS.questions
        assert (await web.api_chat_last("web-approval"))["stop_reason"] == "user_stopped"
        await response.body_iterator.aclose()
    asyncio.run(run())


def test_stop_before_supervisor_starts_finishes_snapshot(web_service):
    web = web_service
    async def run():
        response = await web.api_chat_stream(web.ChatRequest(message="hello", sessionId="early-stop", turnId="early-turn"))
        assert (await web.api_chat_stop(web.StopRequest(sessionId="early-stop")))["stopped"]
        last = await web.api_chat_last("early-stop")
        assert last["status"] == "done" and last["stop_reason"] == "user_stopped"
        assert not web._LOCAL_AGENT_TASKS
        assert not web.AGENTS.running
        events = [e async for e in response.body_iterator]
        assert events[-1]["event"] == "done"
    asyncio.run(run())


def test_orphaned_event_stream_ends_after_server_restart(web_service):
    web = web_service
    async def run():
        path = web._job_event_file("orphan")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        path.with_suffix(".meta.json").write_text(json.dumps({"sessionId": "orphan-session"}))
        response = await web.api_chat_job_stream("orphan")
        events = [e async for e in response.body_iterator]
        assert len(events) == 1 and events[0]["event"] == "error"
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
def test_timeout_closes_child_and_releases_session(service, monkeypatch, backend):
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "hang")
    async def run():
        service.config.save(backend)
        with pytest.raises(AgentError, match="超时"):
            await service.run(AgentRequest("timeout", "hello", .3), lambda *_: None)
        assert service.config.session("timeout")["state"] == "failed"
        assert not service.running
        with service.config.lock("timeout"):
            pass
    asyncio.run(run())


def test_codebuddy_probe_reports_login_without_sending_prompt(service, monkeypatch):
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "unauthenticated")
    async def run():
        result = await service.probe("codebuddy")
        assert not result["ready"] and result["authStatus"] == "required"
        assert "agent login codebuddy" in result["detail"]
        service.config.save("codebuddy")
        with pytest.raises(AgentError, match="尚未登录"):
            await service.run(AgentRequest("needs-login", "hello", 5), lambda *_: None)
    asyncio.run(run())
    wire = (service.config.root / "wire.jsonl").read_text()
    assert "session/prompt" not in wire


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
def test_login_uses_resolved_command_and_inherits_terminal(service, monkeypatch, backend):
    from types import SimpleNamespace
    from easel.agents import cli
    calls = []
    def invoke(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli.subprocess, "run", invoke)
    assert cli.cmd_agent(SimpleNamespace(action="login", backend=backend)) == 0
    command, kwargs = calls[0]
    assert command == [sys.executable, str(PEER)] + (["login"] if backend == "codex" else [])
    assert "capture_output" not in kwargs and "shell" not in kwargs


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
def test_catalog_tracks_model_specific_efforts_and_never_prompts(service, backend):
    async def run():
        catalog = await service.discover(backend)
        assert catalog["available"] and catalog["defaultModel"] == "fake-fast"
        assert [m["id"] for m in catalog["models"]] == ["fake-fast", "fake-deep"]
        assert [o["id"] for o in catalog["reasoningOptions"]] == ["low", "medium"]
        deep = await service.discover(backend, "fake-deep")
        assert [o["id"] for o in deep["reasoningOptions"]] == ["high", "max"]
        assert deep["defaultReasoningEffort"] == "high"
        assert deep["defaultModel"] == "fake-fast"
    asyncio.run(run())
    records = [json.loads(line) for line in (service.config.root / "wire.jsonl").read_text().splitlines()]
    assert not any(r.get("method") in ("session/prompt", "turn/start") for r in records)


def test_codex_catalog_paginates_and_coalesces_parallel_reads(service, monkeypatch):
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "paged")
    async def run():
        first, second = await asyncio.gather(service.discover("codex"), service.discover("codex"))
        assert len(first["models"]) == 2 and first == second
        assert await service.discover("codex") == first
    asyncio.run(run())
    records = [json.loads(line) for line in (service.config.root / "wire.jsonl").read_text().splitlines()]
    assert sum(r.get("method") == "initialize" for r in records) == 1
    assert sum(r.get("method") == "model/list" for r in records) == 2


def test_multiple_agent_defaults_are_independent_and_do_not_switch_active_backend(service, monkeypatch):
    async def run():
        await service.configure("codex", "fake-fast", "medium")
        await service.configure("codebuddy", "fake-deep", "max")
        settings = service.config.settings()
        assert settings["backend"] == "codex"
        assert settings["models"] == {"codex": "fake-fast", "codebuddy": "fake-deep"}
        assert settings["reasoningEfforts"] == {"codex": "medium", "codebuddy": "max"}
        await service.configure("codebuddy", "fake-deep", "max", make_default=True)
        assert service.backend_for() == "codebuddy"
        monkeypatch.setenv("EASEL_AGENT_BACKEND", "codex")
        await service.configure("codebuddy", "fake-fast", "low")
        assert service.backend_for() == "codex"
        monkeypatch.delenv("EASEL_AGENT_BACKEND")
        assert service.backend_for() == "codebuddy"
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
def test_turn_selection_reaches_native_cli_and_can_reset_to_default(service, backend):
    async def run():
        # Explicit per-chat selection overrides the global default.
        await service.run(AgentRequest("selected", "first", 5, "fake-deep", backend, "max"), lambda *_: None)
        saved = service.config.session("selected")
        assert (saved["backend"], saved["model"], saved["reasoningEffort"]) == (backend, "fake-deep", "max")
        await service.run(AgentRequest("selected", "second", 5, "fake-fast", backend, ""), lambda *_: None)
        assert service.config.session("selected")["reasoningEffort"] == ""
        other = "codebuddy" if backend == "codex" else "codex"
        with pytest.raises(AgentError, match="新建对话"):
            await service.run(AgentRequest("selected", "cannot switch", 5, backend=other), lambda *_: None)
    asyncio.run(run())
    records = [json.loads(line) for line in (service.config.root / "wire.jsonl").read_text().splitlines()]
    if backend == "codex":
        turns = [r["params"] for r in records if r.get("method") == "turn/start"]
        assert [(t["model"], t["effort"]) for t in turns] == [("fake-deep", "max"), ("fake-fast", "low")]
    else:
        changes = [r["params"] for r in records if r.get("method") == "session/set_config_option"]
        assert [(c["configId"], c["value"]) for c in changes] == [("model", "fake-deep"), ("thought", "max"), ("model", "fake-fast"), ("thought", "low")]


@pytest.mark.parametrize("backend", ["codex", "codebuddy"])
@pytest.mark.parametrize("model,effort", [("missing-model", ""), ("fake-fast", "max")])
def test_invalid_model_or_effort_never_sends_prompt(service, backend, model, effort):
    async def run():
        with pytest.raises(AgentError):
            await service.run(AgentRequest("invalid", "hello", 5, model, backend, effort), lambda *_: None)
    asyncio.run(run())
    records = [json.loads(line) for line in (service.config.root / "wire.jsonl").read_text().splitlines()]
    assert not any(r.get("method") in ("session/prompt", "turn/start") for r in records)


def test_unauthenticated_catalog_has_no_invented_choices(service, monkeypatch):
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "unauthenticated")
    async def run():
        result = await service.discover("codebuddy")
        assert not result["available"] and result["models"] == [] and result["reasoningOptions"] == []
        assert "尚未登录" in result["detail"]
    asyncio.run(run())


def test_acp_grouped_models_and_boolean_thinking_follow_advertised_config():
    from easel.agents.options import acp_catalog
    result = acp_catalog({"configOptions": [
        {"id": "model", "category": "model", "type": "select", "currentValue": "m",
         "options": [{"group": "models", "options": [{"value": "m", "name": "Model"}]}]},
        {"id": "thinking", "type": "boolean", "category": "thought_level", "currentValue": False},
    ]})
    assert result["models"][0]["id"] == "m"
    assert result["reasoningConfigId"] == "thinking" and result["defaultReasoningEffort"] == "false"
    assert [r["id"] for r in result["reasoningOptions"]] == ["true", "false"]


def test_web_chat_selection_overrides_default_and_is_recoverable(web_service):
    web = web_service
    async def run():
        req = web.ChatRequest(message="hello", sessionId="choice-web", turnId="choice-turn",
                              backend="codebuddy", model="fake-deep", reasoningEffort="max")
        response = await web.api_chat_stream(req)
        events = [e async for e in response.body_iterator]
        assert events[-1]["event"] == "done"
        assert await web.api_agent_selection("choice-web") == {"backend": "codebuddy", "model": "fake-deep", "reasoningEffort": "max"}
        with pytest.raises(web.HTTPException) as error:
            await web.api_chat_stream(web.ChatRequest(message="hello", sessionId="choice-web", backend="codex"))
        assert error.value.status_code == 409
        assert web.AGENTS.backend_for() == "codex"
    asyncio.run(run())


def test_cli_switch_preserves_each_agents_defaults(service, monkeypatch):
    from types import SimpleNamespace
    from easel.agents import cli
    service.config.save("codebuddy", "fake-deep", "max", make_default=False)
    monkeypatch.setattr(cli, "AgentService", lambda _: service)
    assert cli.cmd_agent(SimpleNamespace(action="use", backend="codebuddy", model=None)) == 0
    assert service.config.settings()["models"]["codebuddy"] == "fake-deep"
    assert service.config.settings()["reasoningEfforts"]["codebuddy"] == "max"


@pytest.mark.parametrize("effort", ["true", "false"])
def test_codebuddy_boolean_thinking_uses_typed_acp_value(service, monkeypatch, effort):
    monkeypatch.setenv("EASEL_TEST_SCENARIO", "boolean")
    async def run():
        await service.run(AgentRequest("boolean", "hello", 5, backend="codebuddy", reasoning_effort=effort), lambda *_: None)
    asyncio.run(run())
    records = [json.loads(line) for line in (service.config.root / "wire.jsonl").read_text().splitlines()]
    change = next(r["params"] for r in records if r.get("method") == "session/set_config_option")
    assert change == {"sessionId": "native-codebuddy", "configId": "thinking", "type": "boolean", "value": effort == "true"}
