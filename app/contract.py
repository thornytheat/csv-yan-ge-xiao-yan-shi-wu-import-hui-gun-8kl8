"""模块 2 · 表头契约（按契约版本注册）。

背景：上游订单 CSV 正把金额从「元」切换到「分」，旧文件还会重发。
系统不再只认一套列契约，而是注册两套版本，每次导入必须解析出明确版本：

- v1-yuan  ：历史契约（2026.11），amount 按元，decimal、2 位小数；
- v2-cents ：新契约（2026.12），amount 按分，整数、单位=分。

两套版本只有 amount 列口径不同，其余列完全一致；金额入库前统一换算成
amount_cents（分），因此跨版本的站点汇总与总金额仍可对比、可对账。

规则（两版相同）：列顺序无关；表头去首尾空白；必需列缺失=错误；额外列按
reject|warn 策略；重复列名=错误；额外列在 warn 策略下被忽略但不影响入库。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

SITES = ["beijing", "shanghai", "guangzhou", "shenzhen", "chengdu", "hangzhou"]
CATEGORIES = ["electronics", "grocery", "apparel"]

REQUIRED = ["site", "category", "order_id", "order_date", "amount", "qty"]

# 两版共用的列（amount 除外，按版本分别定义）
_SHARED_SPECS: dict[str, dict] = {
    "site": {"required": True, "type": "enum", "enum": SITES, "max_length": 32, "label": "站点"},
    "category": {"required": True, "type": "enum", "enum": CATEGORIES, "max_length": 32, "label": "品类"},
    "order_id": {"required": True, "type": "int", "max_length": 32, "label": "订单号"},
    "order_date": {"required": True, "type": "date", "max_length": 10,
                   "format": "YYYY-MM-DD", "label": "下单日期"},
    "qty": {"required": True, "type": "int", "max_length": 8, "min": 1, "max": 100000,
            "label": "件数"},
    "memo": {"required": False, "type": "string", "max_length": 60, "label": "备注"},
}

# 金额上限两版对齐：99,999,999.99 元 = 9,999,999,999 分
MAX_AMOUNT_CENTS = 9_999_999_999

CONTRACT_V1 = "v1-yuan"
CONTRACT_V2 = "v2-cents"
DEFAULT_VERSION = CONTRACT_V1          # 历史契约：缺省按元校验，兼容旧文件
CENTS_VERSION = CONTRACT_V2            # 新契约：按分校验

CONTRACTS: dict[str, dict] = {
    CONTRACT_V1: {
        "version": CONTRACT_V1,
        "label": "2026.11 版 · 金额按元",
        "amount_unit": "yuan",
        "amount_rule": "decimal：元，最多 2 位小数，0.01 ~ 99999999.99",
        "amount_spec": {"required": True, "type": "decimal", "max_length": 16,
                        "scale": 2, "min": "0.01", "label": "金额（元）"},
    },
    CONTRACT_V2: {
        "version": CONTRACT_V2,
        "label": "2026.12 版 · 金额按分",
        "amount_unit": "cent",
        "amount_rule": "int：分，1 ~ 9999999999（= 0.01 ~ 99999999.99 元）",
        "amount_spec": {"required": True, "type": "int", "max_length": 12,
                        "min": 1, "max": MAX_AMOUNT_CENTS, "label": "金额（分）"},
    },
}

# 兼容旧引用：模块级 COLUMN_SPECS 始终是默认版（v1-yuan）的列定义
COLUMN_SPECS: dict[str, dict] = {
    "site": _SHARED_SPECS["site"],
    "category": _SHARED_SPECS["category"],
    "order_id": _SHARED_SPECS["order_id"],
    "order_date": _SHARED_SPECS["order_date"],
    "amount": CONTRACTS[CONTRACT_V1]["amount_spec"],
    "qty": _SHARED_SPECS["qty"],
    "memo": _SHARED_SPECS["memo"],
}

POLICIES = ("reject", "warn")


class UnknownContractVersion(ValueError):
    """请求了未注册的契约版本；versions 给出当前可用版本清单。"""

    def __init__(self, requested: str) -> None:
        self.requested = requested
        self.versions = sorted(CONTRACTS)
        super().__init__(
            f"未知契约版本「{requested}」。可用版本：{', '.join(self.versions)}"
            f"（缺省 = {DEFAULT_VERSION}，金额按元）。"
        )


def resolve_version(version: str | None) -> str:
    """把请求里的契约版本解析成注册版本号；空值 → 默认版（兼容旧调用）。"""
    candidate = (version or "").strip()
    if not candidate:
        return DEFAULT_VERSION
    if candidate not in CONTRACTS:
        raise UnknownContractVersion(candidate)
    return candidate


def specs_for(version: str | None) -> dict[str, dict]:
    """指定契约版本的完整列定义（共享列 + 该版本的 amount 列）。"""
    resolved = resolve_version(version)
    specs = dict(_SHARED_SPECS)
    # 保持契约列顺序：amount 在 order_date 之后、qty 之前
    ordered: dict[str, dict] = {}
    for name in ("site", "category", "order_id", "order_date", "amount", "qty", "memo"):
        ordered[name] = (CONTRACTS[resolved]["amount_spec"] if name == "amount"
                         else specs[name])
    return ordered


def amount_unit(version: str | None) -> str:
    """金额口径：yuan（元）| cent（分）。"""
    return CONTRACTS[resolve_version(version)]["amount_unit"]


def version_label(version: str | None) -> str:
    """展示用标签；未迁移的历史数据（NULL）显示为 legacy。"""
    if version is None or version == "":
        return "legacy（待迁移，按 v1-yuan 口径）"
    info = CONTRACTS.get(version)
    return info["label"] if info else str(version)


def versions_info() -> list[dict]:
    """API/页面用：已注册版本清单 + 金额口径说明。"""
    return [
        {
            "version": v["version"],
            "label": v["label"],
            "amount_unit": v["amount_unit"],
            "amount_rule": v["amount_rule"],
            "is_default": v["version"] == DEFAULT_VERSION,
        }
        for v in CONTRACTS.values()
    ]


def parse_amount_cents(raw: str, version: str | None) -> int:
    """按版本口径把金额原值换算成分；非法值抛 ValueError（迁移复核用）。"""
    unit = amount_unit(version)
    text = (raw or "").strip()
    if unit == "cent":
        try:
            cents = int(text)
        except ValueError as exc:
            raise ValueError(f"金额（分）「{raw}」不是整数") from exc
        if not 1 <= cents <= MAX_AMOUNT_CENTS:
            raise ValueError(f"金额（分）「{raw}」超出 1~{MAX_AMOUNT_CENTS}")
        return cents
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"金额（元）「{raw}」不是合法 decimal") from exc
    cents = int(amount * 100)
    if not 1 <= cents <= MAX_AMOUNT_CENTS:
        raise ValueError(f"金额（元）「{raw}」超出 0.01~99999999.99")
    return cents


def format_amount_raw(amount_cents: int, version: str | None) -> str:
    """由分反推该版本口径下的原始字符串（迁移回填历史明细的 amount_raw 用）。"""
    unit = amount_unit(version)
    cents = int(amount_cents)
    if unit == "cent":
        return str(cents)
    return f"{cents // 100}.{cents % 100:02d}"


def normalize_header(cells: list[str]) -> list[str]:
    """去掉首尾空白（含 BOM 已剥离后的 \\ufeff、全角空格）。"""
    out: list[str] = []
    for cell in cells:
        cleaned = (cell or "").replace("\ufeff", "").strip()
        out.append(cleaned)
    return out


def check(header_cells: list[str], extra_policy: str = "reject",
          version: str | None = None) -> dict:
    """返回契约检查结论；ok=True 才允许进入行校验。version 缺省 = 默认版。"""
    resolved = resolve_version(version)
    specs = specs_for(resolved)

    if extra_policy not in POLICIES:
        extra_policy = "reject"

    header = normalize_header(header_cells)
    known = set(specs)

    missing = [c for c in REQUIRED if c not in header]
    extra = [c for c in header if c not in known]
    duplicate = sorted({c for c in header if c and header.count(c) > 1}, key=header.index)
    blank = [i + 1 for i, c in enumerate(header) if not c]

    errors: list[dict] = []
    warnings: list[dict] = []

    if not header:
        errors.append({"code": "empty_header", "message": "表头为空：第 1 行没有读到任何列名。"})
    if blank:
        errors.append({
            "code": "blank_column_name",
            "message": f"表头第 {', '.join(map(str, blank))} 个列名为空，无法建索引。",
            "columns": [str(i) for i in blank],
        })
    if duplicate:
        errors.append({
            "code": "duplicate_column",
            "message": f"表头存在重复列：{', '.join(duplicate)}（同名列无法确定取值列）。",
            "columns": duplicate,
        })
    if missing:
        errors.append({
            "code": "missing_column",
            "message": f"缺少必需列：{', '.join(missing)}。契约要求：{', '.join(REQUIRED)}。",
            "columns": missing,
        })
    if extra:
        if extra_policy == "reject":
            errors.append({
                "code": "extra_column",
                "message": f"出现契约外列：{', '.join(extra)}（当前策略=reject，整份文件拒绝）。",
                "columns": extra,
            })
        else:
            warnings.append({
                "code": "extra_column_ignored",
                "message": f"契约外列 {', '.join(extra)} 已按 warn 策略忽略，不参与校验与入库。",
                "columns": extra,
            })

    usable = [c for c in header if c in known and c not in duplicate]

    return {
        "ok": not errors,
        "contract_version": resolved,
        "amount_unit": CONTRACTS[resolved]["amount_unit"],
        "header": header,
        "missing": missing,
        "extra": extra,
        "duplicate": duplicate,
        "blank_index": blank,
        "extra_policy": extra_policy,
        "errors": errors,
        "warnings": warnings,
        "columns_used": usable,
        "column_contract": [
            {"name": name, "required": spec["required"], "type": spec["type"],
             "label": spec["label"], "max_length": spec.get("max_length"),
             "enum": spec.get("enum")}
            for name, spec in specs.items()
        ],
    }


def index_of(header: list[str], columns_used: list[str]) -> dict[str, int]:
    """列顺序无关：按列名建索引，只索引契约内且未被判重复的列。"""
    return {name: header.index(name) for name in columns_used}
