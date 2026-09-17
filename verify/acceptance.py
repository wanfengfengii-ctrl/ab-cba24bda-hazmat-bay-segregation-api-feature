"""一次性验收：对运行中的 API 实例做 HTTP 级验收检查。

通过环境变量 API_BASE_URL 指向被验实例（默认 http://api:8000，
即 Compose 网络内的 api 服务）。全部检查通过退出码为 0，否则为 1。
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
ASSESS_URL = f"{BASE_URL}/api/v1/stowage/assess"
REMOVAL_IMPACT_URL = f"{BASE_URL}/api/v1/stowage/removal-impact"
REVIEWS_URL = f"{BASE_URL}/api/v1/stowage/reviews"
HEALTH_URL = f"{BASE_URL}/health"

_failures: list[str] = []

# 每次运行使用唯一命令标识，避免对同一实例重复验收时 commandId 撞车。
RUN_ID = uuid.uuid4().hex[:12]


def cid(name: str) -> str:
    return f"{name}-{RUN_ID}"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        if detail:
            print(f"       {detail}")
        _failures.append(name)


def wait_for_api(timeout_seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(1.0)
    return False


def post_assess(payload: dict) -> tuple[int, bytes]:
    return _post(ASSESS_URL, payload)


def post_removal_impact(payload: dict) -> tuple[int, bytes]:
    return _post(REMOVAL_IMPACT_URL, payload)


def _post(url: str, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def expect_error(name: str, payload: dict, code: str, url: str = ASSESS_URL) -> None:
    status, body = _post(url, payload)
    parsed = json.loads(body)
    check(
        name,
        status == 400
        and set(parsed) == {"error"}
        and parsed.get("error", {}).get("code") == code,
        f"status={status} body={body!r}",
    )


def main() -> int:
    check("api healthy", wait_for_api(), f"no /health at {BASE_URL}")
    if _failures:
        return 1

    status, body = post_assess(
        {
            "hold": "HOLD-3",
            "items": [
                {"id": "C101", "category": "FLAM"},
                {"id": "C205", "category": "OXID"},
                {"id": "C330", "category": "WET"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "forbid pair yields FORBID with evidence",
        status == 200
        and data.get("conclusion") == "FORBID"
        and data.get("evidence")
        == [
            {
                "first": "C101",
                "second": "C205",
                "firstCategory": "FLAM",
                "secondCategory": "OXID",
                "rule": "FORBID",
            }
        ],
        f"status={status} body={body!r}",
    )

    status, body = post_assess(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "TOX"},
                {"id": "B2", "category": "FLAM"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "partition pair yields PARTITION",
        status == 200
        and data.get("conclusion") == "PARTITION"
        and len(data.get("evidence", [])) == 1,
        f"status={status} body={body!r}",
    )

    status, body = post_assess(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "WET"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "rule-free pairs yield ALLOW with empty evidence",
        status == 200
        and data.get("conclusion") == "ALLOW"
        and data.get("evidence") == [],
        f"status={status} body={body!r}",
    )

    items = [
        {"id": "P1", "category": "FLAM"},
        {"id": "P2", "category": "OXID"},
        {"id": "P3", "category": "TOX"},
        {"id": "P4", "category": "WET"},
        {"id": "P5", "category": "CORR"},
    ]
    bodies = set()
    for perm in itertools.permutations(items):
        status, body = post_assess({"hold": "H9", "items": list(perm)})
        bodies.add(body if status == 200 else f"status={status}".encode())
    check(
        "permuted submissions are byte-identical",
        len(bodies) == 1,
        f"{len(bodies)} distinct response bodies across {len(list(itertools.permutations(items)))} permutations",
    )

    expect_error(
        "duplicate item id rejected",
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "A1", "category": "GAS"},
            ],
        },
        "DUPLICATE_ITEM_ID",
    )
    expect_error(
        "unknown category rejected",
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "RADIO"},
            ],
        },
        "UNKNOWN_CATEGORY",
    )
    expect_error(
        "item count out of range rejected",
        {"hold": "H1", "items": [{"id": "A1", "category": "FLAM"}]},
        "ITEM_COUNT_OUT_OF_RANGE",
    )
    expect_error(
        "empty hold rejected",
        {
            "hold": "  ",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
            ],
        },
        "EMPTY_HOLD",
    )

    # ---- 单件移除影响分析（removal-impact）----

    status, body = post_removal_impact(
        {
            "hold": "HOLD-3",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
                {"id": "C3", "category": "CORR"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "removal-impact: forbid scenario offers multiple improvement levels",
        status == 200
        and data.get("original", {}).get("conclusion") == "FORBID"
        and [(r.get("removed"), r.get("conclusion")) for r in data.get("removals", [])]
        == [("A1", "PARTITION"), ("B2", "ALLOW"), ("C3", "FORBID")]
        and data.get("recommendations") == ["A1", "B2"],
        f"status={status} body={body!r}",
    )

    status, body = post_removal_impact(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "TOX"},
                {"id": "B2", "category": "FLAM"},
                {"id": "C3", "category": "WET"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "removal-impact: partition scenario can drop to allow",
        status == 200
        and data.get("original", {}).get("conclusion") == "PARTITION"
        and [(r.get("removed"), r.get("conclusion")) for r in data.get("removals", [])]
        == [("A1", "ALLOW"), ("B2", "ALLOW"), ("C3", "PARTITION")]
        and data.get("recommendations") == ["A1", "B2"],
        f"status={status} body={body!r}",
    )

    status, body = post_removal_impact(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "OXID"},
                {"id": "C3", "category": "WET"},
                {"id": "D4", "category": "CORR"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "removal-impact: no improvement yields empty recommendations",
        status == 200
        and data.get("original", {}).get("conclusion") == "FORBID"
        and all(r.get("conclusion") == "FORBID" for r in data.get("removals", []))
        and data.get("recommendations") == [],
        f"status={status} body={body!r}",
    )

    items = [
        {"id": "P1", "category": "FLAM"},
        {"id": "P2", "category": "GAS"},
        {"id": "P3", "category": "CORR"},
        {"id": "P4", "category": "TOX"},
        {"id": "P5", "category": "WET"},
    ]
    bodies = set()
    for perm in itertools.permutations(items):
        status, body = post_removal_impact({"hold": "H9", "items": list(perm)})
        bodies.add(body if status == 200 else f"status={status}".encode())
    check(
        "removal-impact: permuted submissions are byte-identical",
        len(bodies) == 1,
        f"{len(bodies)} distinct response bodies across {len(list(itertools.permutations(items)))} permutations",
    )

    expect_error(
        "removal-impact: duplicate item id rejected",
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "A1", "category": "GAS"},
            ],
        },
        "DUPLICATE_ITEM_ID",
        url=REMOVAL_IMPACT_URL,
    )
    expect_error(
        "removal-impact: unknown category rejected",
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "RADIO"},
            ],
        },
        "UNKNOWN_CATEGORY",
        url=REMOVAL_IMPACT_URL,
    )
    expect_error(
        "removal-impact: item count out of range rejected",
        {"hold": "H1", "items": [{"id": "A1", "category": "FLAM"}]},
        "ITEM_COUNT_OUT_OF_RANGE",
        url=REMOVAL_IMPACT_URL,
    )
    expect_error(
        "removal-impact: empty hold rejected",
        {
            "hold": "  ",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
            ],
        },
        "EMPTY_HOLD",
        url=REMOVAL_IMPACT_URL,
    )

    request = urllib.request.Request(
        REMOVAL_IMPACT_URL,
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    parsed = json.loads(body)
    check(
        "removal-impact: malformed json rejected",
        status == 400
        and set(parsed) == {"error"}
        and parsed.get("error", {}).get("code") == "INVALID_JSON",
        f"status={status} body={body!r}",
    )

    # ---- 交接评审（reviews）：草稿、版本推进、判重与冻结 ----

    create_payload = {
        "commandId": cid("acc-create-1"),
        "hold": "HOLD-9",
        "items": [
            {"id": "B2", "category": "FLAM"},
            {"id": "A1", "category": "OXID"},
        ],
    }
    status, body = _post(REVIEWS_URL, create_payload)
    data = json.loads(body)
    review_id = data.get("reviewId")
    check(
        "review: create revision=1 draft with canonical request and decision",
        status == 201
        and data.get("revision") == 1
        and data.get("status") == "DRAFT"
        and [i.get("id") for i in data.get("request", {}).get("items", [])] == ["A1", "B2"]
        and data.get("request", {}).get("hold") == "HOLD-9"
        and data.get("decision", {}).get("conclusion") == "FORBID"
        and isinstance(review_id, str)
        and bool(review_id),
        f"status={status} body={body!r}",
    )

    # 创建重试（货项次序不同，规范化后同内容）必须字节一致、同一 reviewId。
    status, replay_body = _post(
        REVIEWS_URL,
        {
            "commandId": cid("acc-create-1"),
            "hold": "HOLD-9",
            "items": [
                {"id": "A1", "category": "OXID"},
                {"id": "B2", "category": "FLAM"},
            ],
        },
    )
    check(
        "review: create retry replays byte-identical response",
        status == 201 and replay_body == body,
        f"status={status} first={body!r} replay={replay_body!r}",
    )

    # 同 commandId 不同内容 → 409 COMMAND_ID_REUSED。
    status, body = _post(
        REVIEWS_URL,
        {
            "commandId": cid("acc-create-1"),
            "hold": "HOLD-9",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    check(
        "review: reused commandId with different payload rejected",
        status == 409
        and json.loads(body).get("error", {}).get("code") == "COMMAND_ID_REUSED",
        f"status={status} body={body!r}",
    )

    commands_url = f"{REVIEWS_URL}/{review_id}/commands"

    replace_command = {
        "commandId": cid("acc-replace-1"),
        "expectedRevision": 1,
        "action": "REPLACE",
        "items": [
            {"id": "A1", "category": "WET"},
            {"id": "B2", "category": "WET"},
        ],
    }
    status, replace_body = _post(commands_url, replace_command)
    data = json.loads(replace_body)
    check(
        "review: replace advances to revision 2 and recomputes decision",
        status == 200
        and data.get("revision") == 2
        and data.get("status") == "DRAFT"
        and data.get("decision", {}).get("conclusion") == "ALLOW"
        and data.get("decision", {}).get("evidence") == [],
        f"status={status} body={replace_body!r}",
    )
    status, replace_replay = _post(commands_url, replace_command)
    check(
        "review: replace retry is byte-identical and does not bump revision",
        status == 200 and replace_replay == replace_body,
        f"status={status} first={replace_body!r} replay={replace_replay!r}",
    )

    # 旧 expectedRevision 争用失败：409 REVISION_CONFLICT 且带 currentRevision。
    status, body = _post(
        commands_url,
        {
            "commandId": cid("acc-stale-1"),
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    data = json.loads(body)
    check(
        "review: stale expectedRevision conflicts with currentRevision",
        status == 409
        and data.get("error", {}).get("code") == "REVISION_CONFLICT"
        and data.get("error", {}).get("currentRevision") == 2,
        f"status={status} body={body!r}",
    )

    # 冲突命令按新版本重试成功（失败未消耗 commandId）。
    status, body = _post(
        commands_url,
        {
            "commandId": cid("acc-stale-1"),
            "expectedRevision": 2,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    check(
        "review: conflict command retries successfully on new revision",
        status == 200 and json.loads(body).get("revision") == 3,
        f"status={status} body={body!r}",
    )

    # 不存在的评审：404 REVIEW_NOT_FOUND（即使 items 非法也先报这个）。
    status, body = _post(
        f"{REVIEWS_URL}/no-such-review/commands",
        {
            "commandId": cid("acc-missing-1"),
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [{"id": "A1", "category": "BOOM"}],
        },
    )
    check(
        "review: unknown review reports REVIEW_NOT_FOUND first",
        status == 404
        and json.loads(body).get("error", {}).get("code") == "REVIEW_NOT_FOUND",
        f"status={status} body={body!r}",
    )

    # 非法替换整体拒绝，不留下部分状态（版本不变）。
    status, body = _post(
        commands_url,
        {
            "commandId": cid("acc-invalid-1"),
            "expectedRevision": 3,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "A1", "category": "GAS"},
            ],
        },
    )
    check(
        "review: invalid replacement rejected without partial state",
        status == 400
        and json.loads(body).get("error", {}).get("code") == "DUPLICATE_ITEM_ID",
        f"status={status} body={body!r}",
    )

    # 确认：冻结 revision=3 的快照；重试字节一致。
    confirm_command = {
        "commandId": cid("acc-confirm-1"),
        "expectedRevision": 3,
        "action": "CONFIRM",
    }
    status, confirm_body = _post(commands_url, confirm_command)
    data = json.loads(confirm_body)
    check(
        "review: confirm finalizes frozen snapshot",
        status == 200
        and data.get("status") == "FINALIZED"
        and data.get("snapshot", {}).get("revision") == 3
        and data.get("snapshot", {}).get("hold") == "HOLD-9"
        and data.get("snapshot", {}).get("decision", {}).get("conclusion") == "ALLOW",
        f"status={status} body={confirm_body!r}",
    )
    status, confirm_replay = _post(commands_url, confirm_command)
    check(
        "review: confirm retry is byte-identical",
        status == 200 and confirm_replay == confirm_body,
        f"status={status} first={confirm_body!r} replay={confirm_replay!r}",
    )

    # 已确认评审再下新命令：409 REVIEW_FINALIZED。
    status, body = _post(
        commands_url,
        {
            "commandId": cid("acc-after-final-1"),
            "expectedRevision": 3,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "WET"},
                {"id": "B2", "category": "WET"},
            ],
        },
    )
    check(
        "review: finalized review rejects further commands",
        status == 409
        and json.loads(body).get("error", {}).get("code") == "REVIEW_FINALIZED",
        f"status={status} body={body!r}",
    )

    # 并发替换与确认争用同一版本：仅一个原子成功。
    status, body = _post(
        REVIEWS_URL,
        {
            "commandId": cid("acc-race-create"),
            "hold": "HOLD-RACE",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "OXID"},
            ],
        },
    )
    race_review_id = json.loads(body).get("reviewId")
    race_url = f"{REVIEWS_URL}/{race_review_id}/commands"
    contenders = [
        {
            "commandId": cid("acc-race-replace"),
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "WET"},
                {"id": "B2", "category": "WET"},
            ],
        },
        {"commandId": cid("acc-race-confirm"), "expectedRevision": 1, "action": "CONFIRM"},
    ]
    race_results: list[tuple[int, bytes]] = []
    race_barrier = threading.Barrier(len(contenders))

    def fire_race(payload: dict) -> None:
        race_barrier.wait()
        race_results.append(_post(race_url, payload))

    threads = [threading.Thread(target=fire_race, args=(p,)) for p in contenders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [b for st, b in race_results if st == 200]
    failures = [
        b for st, b in race_results
        if st == 409
        and json.loads(b).get("error", {}).get("code")
        in ("REVISION_CONFLICT", "REVIEW_FINALIZED")
    ]
    winner_status = json.loads(winners[0]).get("status") if winners else None
    check(
        "review: concurrent replace+confirm commit exactly one result",
        len(race_results) == 2 and len(winners) == 1 and len(failures) == 1,
        f"results={[(st, b[:120]) for st, b in race_results]!r}",
    )

    # 失败者可按新版本重试：替换输了就按 rev2 确认；确认赢了（版本不递增）
    # 则替换按 REVIEW_FINALIZED 失败，不留下任何部分状态。
    if winner_status == "DRAFT":
        status, body = _post(
            race_url,
            {"commandId": cid("acc-race-retry"), "expectedRevision": 2, "action": "CONFIRM"},
        )
        check(
            "review: losing command retries successfully on new revision",
            status == 200
            and json.loads(body).get("status") == "FINALIZED"
            and json.loads(body).get("snapshot", {}).get("revision") == 2,
            f"status={status} body={body!r}",
        )
    else:
        failure_code = json.loads(failures[0]).get("error", {}).get("code")
        status, body = _post(
            race_url,
            {
                "commandId": cid("acc-race-retry"),
                "expectedRevision": 1,
                "action": "REPLACE",
                "items": [
                    {"id": "A1", "category": "WET"},
                    {"id": "B2", "category": "WET"},
                ],
            },
        )
        check(
            "review: confirm winner froze review; loser leaves no partial state",
            failure_code == "REVIEW_FINALIZED"
            and status == 409
            and json.loads(body).get("error", {}).get("code") == "REVIEW_FINALIZED",
            f"failure={failure_code} retry-status={status} body={body!r}",
        )

    if _failures:
        print(f"\n{len(_failures)} acceptance check(s) failed")
        return 1
    print("\nAll acceptance checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
