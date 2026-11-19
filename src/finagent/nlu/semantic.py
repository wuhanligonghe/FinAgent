"""The immutable wire contract shared byte-for-byte with the SFT project."""
from hashlib import sha256
from importlib.resources import files
import json

VERSION = "nlu_sft_v2.1"
SCHEMA_BYTES = files("finagent").joinpath("resources/nlu_sft2_schema.json").read_bytes()
PROMPT_BYTES = files("finagent").joinpath("resources/nlu_sft2_system.txt").read_bytes()
SCHEMA = json.loads(SCHEMA_BYTES)
SYSTEM = PROMPT_BYTES.decode("utf-8")
PROMPT_HASH = sha256(PROMPT_BYTES).hexdigest()
SCHEMA_HASH = sha256(SCHEMA_BYTES).hexdigest()


def strict_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def invalid_constant(_):
        raise ValueError("Non-JSON numeric constant")

    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)


def messages(text, context):
    if not isinstance(text, str) or not text.strip() or not isinstance(context, dict):
        raise ValueError("请求必须为非空原话及上下文对象")
    allowed = {"confirmed_symbol", "history", "scope", "research_id", "pending_slot", "dimensions", "candidates"}
    if set(context) - allowed:
        raise ValueError("上下文含未知字段")
    if (not isinstance(context.get("history", []), list) or not isinstance(context.get("candidates", []), list)
            or not isinstance(context.get("scope", {}), dict)):
        raise ValueError("上下文话轮、候选或范围的类型无效")
    for key in ("confirmed_symbol", "research_id", "pending_slot"):
        if context.get(key) is not None and not isinstance(context[key], str):
            raise ValueError("上下文标识字段类型无效")
    # Bounds are checked before serialization/tokenization; never truncate.
    if len(context.get("history", [])) > 3 or len(context.get("candidates", [])) > 10:
        raise ValueError("上下文话轮或候选过多")
    if any(not isinstance(t, str) or len(t) > 1000 for t in context.get("history", [])):
        raise ValueError("上下文话轮无效或过长")
    user = json.dumps({"text": text, "context": context}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def render_prompt(conversation):
    """Qwen3 non-thinking, no-tools two-message subset. Verified during export."""
    return "".join("<|im_start|>" + m["role"] + "\n" + m["content"] + "<|im_end|>\n" for m in conversation) + "<|im_start|>assistant\n<think>\n\n</think>\n\n"


def validate_frame(value, text):
    from .contracts import Entity
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        raise ValueError("语义JSON缺失或包含未知字段")
    for field in ("schema_version", "task_type", "dialogue_act", "entity_binding"):
        spec = SCHEMA["properties"][field]
        if value[field] not in spec.get("enum", [spec.get("const")]):
            raise ValueError("语义枚举或版本无效")
    dims = value["dimension_state"]
    expected = SCHEMA["properties"]["dimension_state"]["properties"]
    if not isinstance(dims, dict) or set(dims) != set(expected) or any(v not in ("include", "exclude", "unspecified") for v in dims.values()):
        raise ValueError("必须提供五个维度的三态值")
    if not isinstance(value["entities"], list) or len(value["entities"]) > 64:
        raise ValueError("实体数组无效")
    end = 0
    for raw in value["entities"]:
        if not isinstance(raw, dict) or set(raw) != {"type", "role", "text", "start", "end"}:
            raise ValueError("实体缺失或包含未知字段")
        entity = Entity.model_validate(raw, strict=True)
        if entity.start < end or entity.end > len(text) or text[entity.start:entity.end] != entity.text:
            raise ValueError("实体位置并非有序且不重叠的原文片段")
        end = entity.end
    return value
