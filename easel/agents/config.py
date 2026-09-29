from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .base import AgentError

BACKENDS = {"openclaw": "OpenClaw", "codex": "Codex", "codebuddy": "CodeBuddy"}


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("expected object")
        return result
    except (ValueError, OSError) as exc:
        raise AgentError(f"Agent 配置或会话记录损坏：{path.name}，请先恢复备份") from exc


class AgentConfig:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = Path(os.environ.get("EASEL_AGENT_STATE_DIR") or self.root / "outputs" / "_agents")

    def settings(self) -> dict:
        data = read_json(self.directory / "config.json")
        backend = os.environ.get("EASEL_AGENT_BACKEND") or data.get("backend", "openclaw")
        if backend not in BACKENDS:
            raise AgentError(f"不支持的 Agent 后端：{backend}")
        return {"backend": backend, "models": data.get("models", {}),
                "reasoningEfforts": data.get("reasoningEfforts", {}),
                "permissionModes": data.get("permissionModes", {}),
                "environmentOverride": bool(os.environ.get("EASEL_AGENT_BACKEND"))}

    def save(self, backend: str, model: str = "", reasoning_effort: str | None = None,
             *, make_default: bool = True, permission_mode: str | None = None) -> dict:
        if backend not in BACKENDS:
            raise AgentError("不支持的 Agent 后端")
        if len(model) > 150 or any(c.isspace() for c in model):
            raise AgentError("模型名称不能包含空白，且不得超过 150 字符")
        if reasoning_effort is not None and (len(reasoning_effort) > 150 or any(c.isspace() for c in reasoning_effort)):
            raise AgentError("无效的思考深度")
        if permission_mode is not None and (len(permission_mode) > 150 or any(c.isspace() for c in permission_mode)):
            raise AgentError("无效的权限模式")
        if backend == "openclaw" and permission_mode:
            raise AgentError("OpenClaw 权限由网关管理")
        override = os.environ.get("EASEL_AGENT_BACKEND")
        if make_default and override and override != backend:
            raise AgentError("EASEL_AGENT_BACKEND 固定了当前后端，请先移除该环境变量并重启")
        data = self.settings()
        if make_default:
            data["backend"] = backend
        elif override:
            # Saving another agent must not persist the environment override.
            data["backend"] = read_json(self.directory / "config.json").get("backend", "openclaw")
        data["models"][backend] = model
        if reasoning_effort is not None:
            data["reasoningEfforts"][backend] = reasoning_effort
        if permission_mode is not None:
            data["permissionModes"][backend] = permission_mode
        data.pop("environmentOverride", None)
        atomic_json(self.directory / "config.json", data)
        return self.settings()

    def session_path(self, session_id: str) -> Path:
        return self.directory / "sessions" / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")

    def session(self, session_id: str) -> dict:
        return read_json(self.session_path(session_id))

    @contextmanager
    def lock(self, session_id: str):
        path = self.session_path(session_id).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as f:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.write(b"0"); f.flush(); f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise AgentError("此会话正在运行，请等待结束或停止后重试") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def command(self, backend: str) -> list[str]:
        override = os.environ.get(f"EASEL_{backend.upper()}_BIN")
        candidates = [override] if override else [
            shutil.which(backend),
            str(self.root / ".easel" / "tools" / backend / "node_modules" / ".bin" / backend),
            str(Path.home() / ".local" / "bin" / backend),
        ]
        if backend == "codex" and not override and sys.platform == "darwin":
            candidates += [
                "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                "/Applications/Codex.app/Contents/Resources/codex",
            ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                candidate = os.path.abspath(candidate)
                # JS entrypoints can also be explicitly selected without a shell.
                if candidate.endswith((".js", ".mjs", ".cjs")):
                    node = shutil.which("node")
                    if node:
                        return [os.path.abspath(node), candidate]
                if os.access(candidate, os.X_OK):
                    return [candidate]
        raise AgentError(f"未找到 {BACKENDS[backend]} CLI；请安装并登录，或设置 EASEL_{backend.upper()}_BIN")

    def login_hint(self, backend: str) -> str:
        command = self.command(backend) + (["login"] if backend == "codex" else [])
        if os.name == "nt":
            # PowerShell requires the call operator for quoted executable paths.
            return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in command)
        return shlex.join(command)

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["EASEL_ROOT"] = str(self.root)
        venv_bin = self.root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
        if os.environ.get("EASEL_PROXY"):
            env.setdefault("https_proxy", os.environ["EASEL_PROXY"])
            env.setdefault("http_proxy", os.environ["EASEL_PROXY"])
        # The local CLI owns authentication. Do not source .env or copy tokens.
        env["EASEL_ASKUSER_CARDS"] = "0"  # use native questions, not OpenClaw RPC
        return env


def context(root: Path) -> str:
    parts = []
    for name in ("SOUL.md", "AGENTS.md"):
        p = root / "openclaw" / "workspace" / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8"))
    skills = sorted(p.parent.name for p in (root / "skills" / "openclaw").glob("*/SKILL.md"))
    parts.append(
        f"\n# Easel 当前运行环境（覆盖上述 OpenClaw 专属路径及工具说明）\n"
        f"你通过本地 CLI 运行，项目根目录为 {root}。使用当前 CLI 原生文件、命令和问答工具。"
        "无需运行 OpenClaw，不调用 gateway question RPC。需要用户输入时使用原生问答工具；"
        "没有问答工具时在回复中提问并结束本轮。不要安装或启动 OpenClaw。\n"
        "技能库为 skills/openclaw/<技能名>/SKILL.md，先读匹配技能再执行；"
        "技能脚本和 references 保持原目录。画像实际位于 profiles/（不是 easel-profiles/）。"
        "只读本轮指定画像。产物位于 outputs/，素材位于 assets/。"
        "outputs/_agents 为宿主运行状态，禁止读取或改写。"
        "主对话认证由 CLI 管理，.env 缺失不影响主对话；媒体技能仍独立检查自己的配置。\n"
        "可用技能：" + "、".join(skills)
    )
    return "\n\n".join(parts)
