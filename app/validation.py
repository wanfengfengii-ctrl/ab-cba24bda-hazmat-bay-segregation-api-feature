"""请求体校验：任何非法输入整体拒绝，并返回稳定错误代码。"""

from __future__ import annotations

from app.rules import CATEGORIES

MIN_ITEMS = 2
MAX_ITEMS = 20


class ApiError(Exception):
    """携带稳定错误代码与 HTTP 状态码的校验错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        extra: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        # 额外的错误上下文字段（如 currentRevision），并入 error 对象。
        self.extra = extra


def validate_items(raw_items: object) -> list[tuple[str, str]]:
    """校验货项数组，返回 ``[(编号, 类别), ...]``。

    数量、逐项结构（编号 → 类别 → 编号唯一性）的校验顺序与
    ``validate_payload`` 完全一致，供命令替换货项时复用。
    """
    if not isinstance(raw_items, list):
        raise ApiError("INVALID_REQUEST", "Field 'items' must be a JSON array.")

    count = len(raw_items)
    if not MIN_ITEMS <= count <= MAX_ITEMS:
        raise ApiError(
            "ITEM_COUNT_OUT_OF_RANGE",
            f"Field 'items' must contain between {MIN_ITEMS} and {MAX_ITEMS} "
            f"entries; got {count}.",
        )

    seen_ids: set[str] = set()
    items: list[tuple[str, str]] = []
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            raise ApiError(
                "INVALID_REQUEST", f"Item at index {index} must be a JSON object."
            )
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ApiError(
                "INVALID_ITEM_ID",
                f"Item at index {index} must have a non-empty string 'id'.",
            )
        category = entry.get("category")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ApiError(
                "UNKNOWN_CATEGORY",
                f"Item '{item_id}' has unknown category {category!r}; "
                f"expected one of {sorted(CATEGORIES)}.",
            )
        if item_id in seen_ids:
            raise ApiError(
                "DUPLICATE_ITEM_ID", f"Duplicate item id '{item_id}'."
            )
        seen_ids.add(item_id)
        items.append((item_id, category))

    return items


def validate_payload(payload: object) -> tuple[str, list[tuple[str, str]]]:
    """校验并解析请求体，返回 ``(舱位, [(编号, 类别), ...])``。

    校验顺序固定：整体结构 → 舱位 → 货项数量 → 逐项（编号 → 类别 →
    编号唯一性），保证同类非法输入永远得到同一个错误代码。
    """
    if not isinstance(payload, dict):
        raise ApiError("INVALID_REQUEST", "Request body must be a JSON object.")

    hold = payload.get("hold")
    if not isinstance(hold, str) or not hold.strip():
        raise ApiError("EMPTY_HOLD", "Field 'hold' must be a non-empty string.")

    items = validate_items(payload.get("items"))
    return hold, items


def canonical_request(hold: str, items: list[tuple[str, str]]) -> dict:
    """把请求规范化为判重与持久化使用的稳定结构：货项按编号升序。"""
    return {
        "hold": hold,
        "items": [
            {"id": item_id, "category": category}
            for item_id, category in sorted(items, key=lambda item: item[0])
        ],
    }
