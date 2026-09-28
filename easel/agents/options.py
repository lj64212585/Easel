"""Normalize advertised CLI capabilities; never invent model/effort values."""
from .base import AgentError


def select_model(catalog: dict, model: str) -> str:
    if model and model not in {row["id"] for row in catalog["models"]}:
        raise AgentError(f"CLI 当前未提供模型 {model}，请刷新模型列表后重新选择")
    return model or catalog.get("defaultModel", "")


def select_effort(catalog: dict, effort: str) -> str:
    if effort and effort not in {row["id"] for row in catalog["reasoningOptions"]}:
        raise AgentError(f"当前模型不支持思考深度 {effort}，请重新选择")
    return effort or catalog.get("defaultReasoningEffort", "")


def acp_choices(option: dict) -> list[dict]:
    if option.get("type") == "boolean":
        return [{"id": "true", "name": "开启"}, {"id": "false", "name": "关闭"}]
    result = []
    for row in option.get("options", []):
        if "options" in row:
            result.extend(acp_choices(row))
        elif "value" in row:
            result.append({"id": str(row["value"]), "name": row.get("name") or str(row["value"]),
                           "description": row.get("description", "")})
    return result


def acp_catalog(result: dict, model: str = "") -> dict:
    configs = result.get("configOptions") or []
    model_option = next((o for o in configs if o.get("category") == "model" or o.get("id") == "model"), {})
    state = result.get("models") or {}
    models = acp_choices(model_option) or [
        {"id": row["modelId"], "name": row.get("name") or row["modelId"],
         "description": row.get("description", "")}
        for row in state.get("availableModels", [])
    ]
    thought = next((o for o in configs if o.get("category") == "thought_level"), None)
    if not thought:
        thought = next((o for o in configs if o.get("id") in
                        ("reasoning_effort", "thinking_level", "thinking")), {})
    current = thought.get("currentValue", "")
    if isinstance(current, bool):
        current = str(current).lower()
    default_model = model_option.get("currentValue") or state.get("currentModelId", "")
    return {"models": models, "defaultModel": default_model, "selectedModel": model or default_model,
            "reasoningOptions": acp_choices(thought), "defaultReasoningEffort": str(current),
            "reasoningConfigId": thought.get("id"), "reasoningConfigType": thought.get("type"),
            "modelConfigId": model_option.get("id")}
