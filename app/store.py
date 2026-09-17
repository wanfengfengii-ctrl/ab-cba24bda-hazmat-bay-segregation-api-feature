"""配载预审评审的版本化存储：草稿、乐观并发与命令幂等。

每个评审（review）绑定一个舱位，从 ``revision=1`` 的草稿开始：

- ``POST /reviews`` 以 commandId 建立草稿，保存规范化请求与裁决；
- ``POST /reviews/{id}/commands`` 携带 commandId 与 expectedRevision，
  替换货项（复用既有校验与裁决并递增版本）或确认（冻结快照）；
- 所有请求先按 commandId 判重：同标识同内容原样重放，同标识不同内容
  返回 409 COMMAND_ID_REUSED；
- 新命令按 REVIEW_NOT_FOUND → REVISION_CONFLICT → REVIEW_FINALIZED 的
  顺序报错；争用同一 expectedRevision 的多个命令只有一个原子成功，
  其余得到 REVISION_CONFLICT（版本被替换推进时）或 REVIEW_FINALIZED
  （版本被确认冻结时），且不会留下部分状态。

存储为进程内实现，单把锁保证“判重 → 版本比对 → 应用”整段原子。
"""

from __future__ import annotations

import threading
import uuid

from app.rules import assess
from app.validation import ApiError, canonical_request, validate_items, validate_payload

DRAFT = "DRAFT"
FINALIZED = "FINALIZED"

KIND_CREATE = "CREATE"
KIND_REPLACE = "REPLACE"
KIND_CONFIRM = "CONFIRM"


class _Command:
    """一条已落地命令的判重记录与重放响应。"""

    __slots__ = (
        "review_id",
        "kind",
        "expected_revision",
        "canonical",
        "content",
        "status",
    )

    def __init__(
        self,
        review_id: str,
        kind: str,
        expected_revision: int | None,
        canonical: dict | None,
        content: dict,
        status: int,
    ) -> None:
        self.review_id = review_id
        self.kind = kind
        # CREATE 记录为 None；REPLACE/CONFIRM 记录提交时的 expectedRevision。
        self.expected_revision = expected_revision
        # CREATE/REPLACE 的规范化请求（CONFIRM 无请求内容，为 None）。
        self.canonical = canonical
        self.content = content
        self.status = status


class _Review:
    __slots__ = (
        "id",
        "hold",
        "revision",
        "status",
        "request",
        "decision",
        "snapshot",
        "commands",
    )

    def __init__(
        self, review_id: str, hold: str, request: dict, decision: dict
    ) -> None:
        self.id = review_id
        self.hold = hold
        self.revision = 1
        self.status = DRAFT
        self.request = request
        self.decision = decision
        self.snapshot: dict | None = None
        self.commands: dict[str, _Command] = {}


def _reused(command_id: str, detail: str = "a different payload") -> ApiError:
    return ApiError(
        "COMMAND_ID_REUSED",
        f"Command id '{command_id}' was already submitted with {detail}.",
        status=409,
    )


class ReviewStore:
    """线程安全的评审存储。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reviews: dict[str, _Review] = {}
        # 全局命令索引：commandId -> 已落地命令（跨评审也不可复用）。
        self._commands: dict[str, _Command] = {}

    def reset(self) -> None:
        """清空全部评审（测试使用）。"""
        with self._lock:
            self._reviews.clear()
            self._commands.clear()

    def create_review(
        self, command_id: str, raw_payload: object
    ) -> tuple[str, dict, int]:
        """按 commandId 建立 revision=1 的草稿。

        返回 ``(reviewId, 响应体, HTTP 状态码)``。请求先按 commandId 判重：
        同标识同内容原样重放（同一个 reviewId、字节一致的响应），同标识
        不同内容抛 409 COMMAND_ID_REUSED；未见过的 commandId 才执行常规
        校验（非法输入抛 400）并建立草稿。
        """
        with self._lock:
            existing = self._commands.get(command_id)
            if existing is not None:
                # 已落地的首请求必然合法；新请求若无法规范化，内容必不相同。
                canonical = self._try_canonical_payload(raw_payload)
                if (
                    existing.kind == KIND_CREATE
                    and canonical is not None
                    and canonical == existing.canonical
                ):
                    return existing.review_id, existing.content, existing.status
                raise _reused(command_id)

            # 未见过的 commandId：执行与 assess 完全相同的校验与裁决。
            hold, items = validate_payload(raw_payload)
            canonical = canonical_request(hold, items)
            decision = assess(items)

            review_id = uuid.uuid4().hex
            review = _Review(review_id, hold, canonical, decision)
            content = {
                "reviewId": review_id,
                "revision": 1,
                "status": DRAFT,
                "request": canonical,
                "decision": decision,
            }
            record = _Command(review_id, KIND_CREATE, None, canonical, content, 201)
            review.commands[command_id] = record
            self._reviews[review_id] = review
            self._commands[command_id] = record
            return review_id, content, 201

    def apply_command(
        self, review_id: str, command_id: str, raw_payload: object
    ) -> tuple[dict, int]:
        """下达替换货项或确认命令。

        ``raw_payload`` 为原始 JSON：``action``、``expectedRevision``，
        替换时还有 ``items``。判重最先发生，故 action/expectedRevision/items
        的合法性只对“未见过的 commandId”校验；疑似重试的请求只按已落地
        命令比对内容，不产生任何新状态。
        """
        with self._lock:
            review = self._reviews.get(review_id)
            existing = self._commands.get(command_id)

            if existing is not None:
                kind = self._payload_kind(raw_payload)
                if existing.review_id != review_id or kind != existing.kind:
                    raise _reused(command_id, "a different command")
                if (
                    not isinstance(raw_payload, dict)
                    or raw_payload.get("expectedRevision")
                    != existing.expected_revision
                ):
                    raise _reused(command_id, "a different expectedRevision")
                if kind == KIND_CONFIRM:
                    # 确认命令无请求体，同评审、同类型、同版本即同内容。
                    return existing.content, existing.status
                # 替换命令：规范化新货项后与已落地内容比对。
                canonical = self._try_canonical_items(review, raw_payload.get("items"))
                if canonical is not None and canonical == existing.canonical:
                    return existing.content, existing.status
                raise _reused(command_id)

            # 未见过的 commandId：先做命令自身的结构校验。
            kind = self._require_kind(raw_payload)
            expected_revision = self._require_expected_revision(raw_payload)

            # 再按 REVIEW_NOT_FOUND → REVISION_CONFLICT →
            # REVIEW_FINALIZED 的顺序报错。
            if review is None:
                raise ApiError(
                    "REVIEW_NOT_FOUND",
                    f"Review '{review_id}' does not exist.",
                    status=404,
                )

            if expected_revision != review.revision:
                raise ApiError(
                    "REVISION_CONFLICT",
                    f"Expected revision {expected_revision} but the review is at "
                    f"revision {review.revision}.",
                    status=409,
                    extra={"currentRevision": review.revision},
                )

            if review.status == FINALIZED:
                raise ApiError(
                    "REVIEW_FINALIZED",
                    f"Review '{review_id}' is finalized and no longer accepts commands.",
                    status=409,
                )

            if kind == KIND_REPLACE:
                # 复用与 assess 完全相同的校验与裁决；非法输入整体拒绝，
                # 评审版本与状态保持不变。
                raw_items = raw_payload.get("items")
                items = validate_items(raw_items)
                canonical = canonical_request(review.hold, items)
                decision = assess(items)
                review.revision += 1
                review.request = canonical
                review.decision = decision
                content = {
                    "reviewId": review.id,
                    "revision": review.revision,
                    "status": DRAFT,
                    "request": canonical,
                    "decision": decision,
                }
            else:
                review.status = FINALIZED
                review.snapshot = {
                    "revision": review.revision,
                    "hold": review.hold,
                    "request": review.request,
                    "decision": review.decision,
                }
                content = {
                    "reviewId": review.id,
                    "revision": review.revision,
                    "status": FINALIZED,
                    "snapshot": review.snapshot,
                }

            record = _Command(
                review.id,
                kind,
                expected_revision,
                content.get("request"),
                content,
                200,
            )
            review.commands[command_id] = record
            self._commands[command_id] = record
            return content, 200

    @staticmethod
    def _payload_kind(raw_payload: object) -> str | None:
        """从原始命令读取类型；非法或未知返回 None。"""
        if not isinstance(raw_payload, dict):
            return None
        action = raw_payload.get("action")
        if action == KIND_REPLACE:
            return KIND_REPLACE
        if action == KIND_CONFIRM:
            return KIND_CONFIRM
        return None

    def _require_kind(self, raw_payload: object) -> str:
        kind = self._payload_kind(raw_payload)
        if kind is None:
            raise ApiError(
                "INVALID_REQUEST",
                "Field 'action' must be either 'REPLACE' or 'CONFIRM'.",
            )
        return kind

    @staticmethod
    def _require_expected_revision(raw_payload: object) -> int:
        expected_revision = (
            raw_payload.get("expectedRevision")
            if isinstance(raw_payload, dict)
            else None
        )
        # bool 是 int 的子类，需显式排除；revision 从 1 开始。
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ApiError(
                "INVALID_REQUEST",
                "Field 'expectedRevision' must be a positive integer.",
            )
        return expected_revision

    @staticmethod
    def _try_canonical_payload(raw_payload: object) -> dict | None:
        """尝试规范化创建请求；非法输入返回 None（用于判重比对）。"""
        try:
            hold, items = validate_payload(raw_payload)
        except ApiError:
            return None
        return canonical_request(hold, items)

    @staticmethod
    def _try_canonical_items(review: _Review | None, raw_items: object) -> dict | None:
        """尝试规范化替换货项；评审不存在或输入非法时返回 None。"""
        if review is None:
            return None
        try:
            items = validate_items(raw_items)
        except ApiError:
            return None
        return canonical_request(review.hold, items)


store = ReviewStore()
