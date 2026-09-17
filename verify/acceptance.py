"""一次性验收：对运行中的 API 实例做 HTTP 级验收检查。

通过环境变量 API_BASE_URL 指向被验实例（默认 http://api:8000，
即 Compose 网络内的 api 服务）。全部检查通过退出码为 0，否则为 1。
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
ASSESS_URL = f"{BASE_URL}/api/v1/stowage/assess"
REMOVAL_IMPACT_URL = f"{BASE_URL}/api/v1/stowage/removal-impact"
HEALTH_URL = f"{BASE_URL}/health"

_failures: list[str] = []


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

    if _failures:
        print(f"\n{len(_failures)} acceptance check(s) failed")
        return 1
    print("\nAll acceptance checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
