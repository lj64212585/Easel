# 本地 Agent 后端

Easel 的 Web 对话、终端对话、技能执行和后台画像任务可以使用本地 Codex 或 CodeBuddy CLI。CLI 管理登录和模型访问，Easel 负责画像、技能、产物、流式显示和交互。无需为这两个后端安装 OpenClaw 或启动 Gateway。

## 使用

在已安装依赖的项目目录中执行：

```bash
source .venv/bin/activate
python -m easel agent status
python -m easel agent use codex
python -m easel agent probe codex
python -m easel web
```

也可在 Web「设置 → 模型配置 → 对话与脚本 → 执行助手」选择后端、填写可选模型、点击「保存助手」。修改后新建对话；已有会话保持原后端与模型。模型留空时使用 CLI 默认值，其默认值日后仍可能随 CLI 配置变化。`easel chat --session <会话ID>` 可继续终端会话。

Codex 使用 `codex app-server`。CLI 已通过 ChatGPT 登录时，Easel 沿用该登录；若 CLI 使用 API Key，则仍按其 API 配置运行。Easel 不转换订阅、不复制凭证，也不保证账户剩余额度。首次登录：

```bash
python -m easel agent login codex
```

CodeBuddy 使用官方 CodeBuddy Code 的 `--acp` 接口。这次验证的版本为 `@tencent-ai/codebuddy-code@2.157.0`；可安装在项目内，避免修改全局 npm 环境：

```bash
npm install --prefix .easel/tools/codebuddy --no-audit --no-fund @tencent-ai/codebuddy-code@2.157.0
python -m easel agent login codebuddy
# 在 CLI 中按提示登录，需要时输入 /login。
python -m easel agent probe codebuddy
python -m easel agent use codebuddy
```

WorkBuddy 桌面应用和 CodeBuddy CLI 的登录是否共享，以 CLI 实际认证为准。当前适配器不连接 WorkBuddy 私有桌面协议，也不读取其登录文件。CLI 连接成功与有权使用具体模型是两项检查；探测不发模型消息，实际额度需要通过对话验证。

图片、视频、音乐和配音等技能的服务配置仍独立管理，主对话订阅不自动提供这些媒体 API。

## 后端接口与数据流

```text
Web / CLI / Skill / 后台任务
             │
       AgentService
       ├─ AgentBackend → CodexBackend → codex app-server
       └─ AgentBackend → CodeBuddyBackend → codebuddy --acp

OpenClaw 继续沿用原有网关/CLI 路径，由入口按会话后端分派。
```

`easel/agents/base.py` 定义 `run / cancel / close / probe`。适配器把原生协议转换为 `token / thinking / activity / question` 事件；`AgentService` 管理会话、锁、超时和问答。后续增加后端时实现该接口并注册即可。OpenClaw 的已有 SSE、问答与恢复逻辑保留，尚未重写为该接口。

- Codex 用 `thread/start`、`thread/resume` 和 `turn/start`；CodeBuddy 用 `session/new`、`session/load` 和 `session/prompt`。原生会话 ID 在发送消息前落盘，恢复失败时明确报错，不静默新建会话。
- 权限请求通过 Web 问答卡片或终端传回原生 CLI。不会自动同意。后台非交互任务遇到需要许可的工具会返回交互不可用错误，应改在对话页执行。
- Web 的模型任务与浏览器连接分离。断线、刷新或重复提交同一 `turnId` 只读取已有事件，不重复发送消息。停止会取消当前轮并终止本地子进程，保留会话供继续使用。服务重启后的残留任务明确显示中断。
- 同一会话使用进程内检查和文件锁串行运行。Web 应采用单进程运行；不要启用多个 Uvicorn workers，运行任务和待回答问题属于当前进程。
- 默认设置和原生会话映射在 `outputs/_agents/`；Web 事件日志在 `outputs/_sessions/`。它们是运行状态，不出现在内容库中，不存储 CLI 凭证。
- CLI 命令通过参数数组启动，不使用 shell 拼接。认证沿用 CLI 环境；适配器不自动加载项目 `.env` 的聊天 API 配置。

## 技能与画像

Codex 的 `developerInstructions` 注入现有 `SOUL.md`、`AGENTS.md` 和当前项目路径。ACP 没有标准 system prompt 字段，CodeBuddy 每轮把同样内容放在消息上下文中。原有内容规划、制作和发布确认规则保留。

两个后端直接读取 `skills/openclaw/<技能>/SKILL.md`、`profiles/<画像>/`、`assets/` 和 `outputs/`。目录名中的 openclaw 是现有技能存放位置，无需复制或同步到 OpenClaw。CodeBuddy 未提供可用的原生问答工具时，在回复中提出问题，用户下一轮回答；原生工具许可仍使用卡片。

## 可选环境变量

以下变量需在启动 Easel 的进程环境中设置，例如 `export EASEL_CODEX_BIN=/path/to/codex`，仅写入 `.env` 不会自动生效。

| 变量 | 作用 |
|---|---|
| `EASEL_AGENT_BACKEND` | 固定新会话后端：`openclaw`、`codex`、`codebuddy`，优先于设置页 |
| `EASEL_CODEX_BIN` | Codex 可执行文件的完整路径 |
| `EASEL_CODEBUDDY_BIN` | CodeBuddy 可执行文件或 JS 入口的完整路径，不接受 shell 命令串 |
| `EASEL_AGENT_STATE_DIR` | 替换 `outputs/_agents` 状态目录，供隔离实例/测试使用 |
| `EASEL_PROXY` | 给 CLI 继承的代理；不会覆盖已设定的代理变量 |

默认依次探测 PATH、项目 `.easel/tools/<后端>/node_modules/.bin/`、`~/.local/bin/`，并兼容 macOS ChatGPT/Codex 应用自带的 Codex。`.easel/` 已加入 Git 忽略规则。当前原生运行验收针对 macOS，其他平台需另行验证。

## 验证边界

协议测试覆盖两个后端的会话恢复、工具许可、停止、超时和进程退出，以及 Web 的断线重连、重复提交、过早停止与重启残留日志。真实 Codex 已验证 ChatGPT 认证、多轮恢复及文件写入；浏览器实测通过设置保存、连接检测、流式结束、刷新后重新打开历史会话并继续追问，未发现浏览器脚本错误。当前机器的 CodeBuddy 已验证 ACP 握手和未登录提示，尚未完成账户登录后的真实模型调用。

协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Codex 认证](https://learn.chatgpt.com/docs/auth)、[CodeBuddy ACP](https://www.codebuddy.ai/docs/cli/acp)、[CodeBuddy CLI 快速开始](https://www.codebuddy.ai/docs/cli/quickstart)。
