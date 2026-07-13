"""模型注册表 —— 唯一真相: 哪些模型存在、属哪个 family、官方定价。

为什么是单一来源: 新增/调价一个模型只改这里一处, 不用再 token-dashboard / cc-report
两头同步。定价表的消费方现在是 token-dashboard(generate.py); Phase C 的 💰成本 页 /api
也会读它。各产品的「显示名 / 调色板」仍各管各的(珊瑚 vs 灰阶 = 设计身份), 不进注册表。

定价单位: $/1M token, 元组 = (input, output, cache_write_5m, cache_read)。
cache_write_5m = 1.25×input, cache_read = 0.1×input (Anthropic 官方倍率)。
price=None = 第三方模型(GLM), 不计等效 API 成本。
"""

# 顺序 = 旧 generate.py PRICING 的顺序, 保证 pricing_dict() 与原 PRICING 逐键一致。
MODELS = [
    {"id": "claude-fable-5",            "family": "fable",  "price": (10.0, 50.0, 12.50, 1.00)},
    {"id": "claude-opus-4-8",           "family": "opus",   "price": ( 5.0, 25.0,  6.25, 0.50)},
    {"id": "claude-opus-4-7",           "family": "opus",   "price": ( 5.0, 25.0,  6.25, 0.50)},
    {"id": "claude-opus-4-6",           "family": "opus",   "price": ( 5.0, 25.0,  6.25, 0.50)},
    {"id": "claude-sonnet-5",           "family": "sonnet", "price": ( 3.0, 15.0,  3.75, 0.30)},  # 官方 intro $2/$10 至 2026-08-31; 此处用价目表标准价, 与"等效 API 成本"口径一致
    {"id": "claude-sonnet-4-6",         "family": "sonnet", "price": ( 3.0, 15.0,  3.75, 0.30)},
    {"id": "claude-haiku-4-5-20251001", "family": "haiku",  "price": ( 1.0,  5.0,  1.25, 0.10)},
    {"id": "claude-haiku-4-5",          "family": "haiku",  "price": ( 1.0,  5.0,  1.25, 0.10)},
    {"id": "glm-4.7",                   "family": "glm",    "price": None},
    {"id": "glm-4.5-air",               "family": "glm",    "price": None},
    {"id": "glm-4-flash",               "family": "glm",    "price": None},
]

_BY_ID = {m["id"]: m for m in MODELS}
MODEL_ORDER = [m["id"] for m in MODELS]


def pricing_dict():
    """{model_id: (in, out, cache_write, cache_read)} —— 仅含有定价的模型。
    与旧 generate.py 的 PRICING 常量逐键等价。"""
    return {m["id"]: m["price"] for m in MODELS if m["price"] is not None}


def family_of(model_id):
    """模型 id → family (opus/sonnet/haiku/fable/glm); 未知 → 'other'。
    GLM 走前缀匹配(glm-4.7 / glm-4.5-air …都归 glm)。"""
    m = _BY_ID.get(model_id)
    if m:
        return m["family"]
    if model_id and model_id.startswith("glm-"):
        return "glm"
    if model_id and model_id.startswith("claude-"):
        return "other"
    return "other"


def price_of(model_id):
    """模型 id → 定价元组; 无定价(第三方/未知) → None。"""
    m = _BY_ID.get(model_id)
    return m["price"] if m else None
