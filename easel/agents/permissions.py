"""Explicit permission presets; empty selections use Easel's standard mode."""
from .base import AgentError


CODEX_PERMISSIONS = [
    {"id": "read-only", "name": "只读", "description": "允许读取和分析；禁止写入，也不申请提权。"},
    {"id": "workspace-write", "name": "工作区读写", "description": "可修改工作区文件；越界操作按需请求确认。"},
    {"id": "danger-full-access", "name": "完全访问", "description": "不使用沙盒限制文件与命令操作，无需权限确认。"},
]
CODEBUDDY_PERMISSIONS = {
    "default": ("请求确认", "需要权限时显示确认，由你决定是否允许。"),
    "acceptEdits": ("自动允许编辑", "自动允许文件编辑，其他操作仍由 CLI 判断是否需要确认。"),
    "plan": ("仅规划", "使用 CLI 的规划模式进行分析，不直接实施修改。"),
    "auto": ("自动审核", "由 CLI 自动审核操作；审核不可用时可能请求确认。"),
    "dontAsk": ("无需询问", "执行已授权操作，拒绝需要额外确认的操作。"),
    "bypassPermissions": ("跳过常规确认", "跳过常规权限确认；高风险操作仍可能要求确认。"),
    "fullAccess": ("完全访问", "跳过所有权限检查，包括高风险命令。"),
}


def select_permission(catalog: dict, mode: str) -> str:
    selected = mode or catalog.get("defaultPermissionMode", "")
    if selected and selected not in {row["id"] for row in catalog.get("permissionOptions", [])}:
        raise AgentError(f"当前 Agent 不支持权限模式 {selected}，请刷新选项后重新选择")
    return selected


def codex_permissions(mode: str) -> dict:
    selected = select_permission({"permissionOptions": CODEX_PERMISSIONS,
                                  "defaultPermissionMode": "workspace-write"}, mode)
    return {"sandbox": selected, "approvalPolicy": "on-request" if selected == "workspace-write" else "never"}


def acp_permissions(choices: list[dict], config_id: str | None = None) -> dict:
    rows = []
    for row in choices:
        if row["id"] == "delegate":
            continue  # Easel's main conversation has no parent to grant permissions.
        label, description = CODEBUDDY_PERMISSIONS.get(row["id"], (row.get("name", row["id"]), row.get("description", "")))
        rows.append({"id": row["id"], "name": label, "description": description})
    return {"permissionOptions": rows, "permissionConfigId": config_id,
            "defaultPermissionMode": "default" if rows else ""}
