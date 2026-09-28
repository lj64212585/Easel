"""Terminal entrypoints sharing the same service as Web chat."""
from __future__ import annotations

import asyncio
import getpass
import json
import subprocess
import sys
import uuid
from pathlib import Path

from .base import AgentError, AgentRequest
from .service import AgentService


async def console_ask(items):
    if not sys.stdin.isatty():
        raise AgentError("Agent 需要交互确认；请在终端或 Web 对话中运行")
    answers = {}
    for item in items:
        print("\n" + item.get("header", "") + "\n" + item["question"])
        options = item.get("options", [])
        for i, option in enumerate(options, 1):
            print(f"  {i}) {option['label']}")
        while True:
            reader = getpass.getpass if item.get("isSecret") else input
            answer = await asyncio.to_thread(reader, "选择编号或输入答案：")
            if answer.isdigit() and 0 < int(answer) <= len(options):
                answer = options[int(answer) - 1]["label"]
            if answer.strip() and (item.get("allowCustom") is not False or answer in [o["label"] for o in options]):
                answers[item["questionId"]] = [answer]
                break
    return answers


async def run_console(service, request):
    def emit(kind, data):
        if kind == "token":
            print(data, end="", flush=True)
        elif kind == "activity":
            print(f"\n[{data}]", file=sys.stderr, flush=True)
    try:
        await service.run(request, emit, ask=console_ask)
        print()
        return 0
    except AgentError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 1


def run_once(root: Path, message: str, timeout: float, session_id=None):
    async def run():
        service = AgentService(root)
        try:
            return await run_console(service, AgentRequest(session_id or uuid.uuid4().hex, message, timeout))
        finally:
            await service.close()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


def interactive(root, session_id, persona):
    from easel.persona import chat_turn_message
    async def run():
        service = AgentService(root)
        print(f"后端：{service.backend_for(session_id)}；输入 /exit 退出。")
        try:
            while True:
                try:
                    message = await asyncio.to_thread(input, "\n你：")
                except EOFError:
                    return 0
                if message.strip() in ("/exit", "/quit"):
                    return 0
                if message.strip():
                    await run_console(service, AgentRequest(session_id, chat_turn_message(message, persona)))
        finally:
            await service.close()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


def cmd_agent(args):
    root = Path(__file__).resolve().parents[2]
    service = AgentService(root)
    try:
        if args.action == "use":
            if not args.backend:
                raise AgentError("请指定 openclaw、codex 或 codebuddy")
            service.config.save(args.backend, args.model or "")
            print(f"新会话将使用 {args.backend}；已有会话保持原后端。")
        elif args.action == "probe":
            backend = args.backend or service.backend_for()
            result = asyncio.run(service.probe(backend))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ready"] else 1
        elif args.action == "login":
            backend = args.backend or service.backend_for()
            if backend not in ("codex", "codebuddy"):
                raise AgentError("login 支持 codex 或 codebuddy")
            command = service.config.command(backend)
            if backend == "codex":
                command += ["login"]
            else:
                print("在 CodeBuddy 中按提示登录；如需切换账户，输入 /login。", flush=True)
            return subprocess.run(command, cwd=root, env=service.config.environment()).returncode
        else:
            print(json.dumps(service.status(), ensure_ascii=False, indent=2))
        return 0
    except (AgentError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
