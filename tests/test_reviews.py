"""交接评审判据：草稿建立、版本推进、命令判重、冻结与并发原子性。"""

from __future__ import annotations

import itertools
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store

REVIEWS_URL = "/api/v1/stowage/reviews"


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()
    yield
    store.reset()


@pytest.fixture()
def client():
    return TestClient(app)


def commands_url(review_id: str) -> str:
    return f"{REVIEWS_URL}/{review_id}/commands"


def make_review(client, command_id="cmd-1", hold="H1", items=None):
    items = items or [
        {"id": "B2", "category": "FLAM"},
        {"id": "A1", "category": "OXID"},
    ]
    response = client.post(
        REVIEWS_URL, json={"commandId": command_id, "hold": hold, "items": items}
    )
    assert response.status_code == 201, response.content
    return response


# ---------- 建立草稿 ----------


def test_create_review_is_revision_one_draft_with_canonical_request(client):
    response = make_review(client)
    body = response.json()
    assert body["revision"] == 1
    assert body["status"] == "DRAFT"
    # 规范化请求：货项按编号升序，与录入顺序无关。
    assert [item["id"] for item in body["request"]["items"]] == ["A1", "B2"]
    assert body["request"]["hold"] == "H1"
    # 裁决复用规则引擎：FLAM–OXID 禁止。
    assert body["decision"] == {
        "conclusion": "FORBID",
        "evidence": [
            {
                "first": "A1",
                "second": "B2",
                "firstCategory": "OXID",
                "secondCategory": "FLAM",
                "rule": "FORBID",
            }
        ],
    }
    assert isinstance(body["reviewId"], str) and body["reviewId"]


def test_create_review_decision_matches_assess(client):
    items = [
        {"id": "Z9", "category": "TOX"},
        {"id": "A1", "category": "FLAM"},
        {"id": "M5", "category": "OXID"},
    ]
    body = make_review(client, items=items).json()
    assess_body = client.post(
        "/api/v1/stowage/assess", json={"hold": "H1", "items": items}
    ).json()
    assert body["decision"] == {
        "conclusion": assess_body["conclusion"],
        "evidence": assess_body["evidence"],
    }


def test_create_review_rejected_without_command_id(client):
    response = client.post(
        REVIEWS_URL,
        json={
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("command_id", ["", "   ", None, 9, ["x"]])
def test_create_review_rejects_invalid_command_id(client, command_id):
    payload = {
        "hold": "H1",
        "items": [
            {"id": "A1", "category": "FLAM"},
            {"id": "B2", "category": "GAS"},
        ],
    }
    if command_id is not None:
        payload["commandId"] = command_id
    response = client.post(REVIEWS_URL, json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"hold": "", "items": [{"id": "A", "category": "FLAM"}, {"id": "B", "category": "GAS"}]}, "EMPTY_HOLD"),
        ({"hold": "H1", "items": [{"id": "A", "category": "FLAM"}]}, "ITEM_COUNT_OUT_OF_RANGE"),
        ({"hold": "H1", "items": [{"id": "A", "category": "FLAM"}, {"id": "A", "category": "GAS"}]}, "DUPLICATE_ITEM_ID"),
        ({"hold": "H1", "items": [{"id": "A", "category": "FLAM"}, {"id": "B", "category": "RADIO"}]}, "UNKNOWN_CATEGORY"),
    ],
    ids=["empty-hold", "too-few-items", "duplicate-id", "unknown-category"],
)
def test_create_review_reuses_assess_validation(client, payload, code):
    payload = {"commandId": "v1", **payload}
    response = client.post(REVIEWS_URL, json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_create_review_malformed_json(client):
    response = client.post(
        REVIEWS_URL,
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


def test_create_review_body_must_be_object(client):
    response = client.post(REVIEWS_URL, json=["not", "an", "object"])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_future_expected_revision_also_conflicts(client):
    review_id = make_review(client).json()["reviewId"]
    response = client.post(
        commands_url(review_id),
        json={"commandId": "future", "expectedRevision": 99, "action": "CONFIRM"},
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "REVISION_CONFLICT"
    assert error["currentRevision"] == 1


# ---------- commandId 判重 ----------


def test_create_review_same_command_same_payload_replays_byte_identical(client):
    payload = {
        "commandId": "c1",
        "hold": "H1",
        "items": [
            {"id": "B2", "category": "FLAM"},
            {"id": "A1", "category": "OXID"},
        ],
    }
    first = client.post(REVIEWS_URL, json=payload)
    # 重试时货项次序不同，但规范化后内容相同：仍原样重放。
    retried = client.post(
        REVIEWS_URL,
        json={
            "commandId": "c1",
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "OXID"},
                {"id": "B2", "category": "FLAM"},
            ],
        },
    )
    assert retried.status_code == 201
    assert retried.content == first.content
    assert retried.json()["reviewId"] == first.json()["reviewId"]


def test_create_review_command_id_reused_with_different_payload(client):
    make_review(
        client,
        command_id="c1",
        items=[
            {"id": "A1", "category": "FLAM"},
            {"id": "B2", "category": "OXID"},
        ],
    )
    response = client.post(
        REVIEWS_URL,
        json={
            "commandId": "c1",
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_ID_REUSED"


def test_command_id_reused_check_happens_before_payload_validation(client):
    make_review(
        client,
        command_id="c1",
        items=[
            {"id": "A1", "category": "FLAM"},
            {"id": "B2", "category": "OXID"},
        ],
    )
    # 即使重试请求本身非法，也先判重：同标识不同内容一律 409 而非 400。
    response = client.post(
        REVIEWS_URL,
        json={"commandId": "c1", "hold": "H1", "items": [{"id": "A1", "category": "??"}]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_ID_REUSED"


# ---------- 替换货项 ----------


def test_replace_items_advances_revision_and_recomputes_decision(client):
    review_id = make_review(client).json()["reviewId"]
    response = client.post(
        commands_url(review_id),
        json={
            "commandId": "c2",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "WET"},
                {"id": "B2", "category": "WET"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 2
    assert body["status"] == "DRAFT"
    assert body["decision"]["conclusion"] == "ALLOW"
    assert body["decision"]["evidence"] == []
    assert [item["category"] for item in body["request"]["items"]] == ["WET", "WET"]


def test_replace_replay_is_byte_identical(client):
    review_id = make_review(client).json()["reviewId"]
    command = {
        "commandId": "c2",
        "expectedRevision": 1,
        "action": "REPLACE",
        "items": [
            {"id": "A1", "category": "WET"},
            {"id": "B2", "category": "WET"},
        ],
    }
    first = client.post(commands_url(review_id), json=command)
    for _ in range(3):
        retried = client.post(commands_url(review_id), json=command)
        assert retried.status_code == 200
        assert retried.content == first.content
    # 重放不再次递增版本。
    assert first.json()["revision"] == 2


def test_replace_invalid_items_rejected_without_partial_state(client):
    review_id = make_review(client).json()["reviewId"]
    response = client.post(
        commands_url(review_id),
        json={
            "commandId": "c-bad",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "A1", "category": "GAS"},
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DUPLICATE_ITEM_ID"
    # 失败命令不消耗 commandId、不推进版本：同 commandId 修正后仍可提交。
    retry = client.post(
        commands_url(review_id),
        json={
            "commandId": "c-bad",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    assert retry.status_code == 200
    assert retry.json()["revision"] == 2


def test_replace_unknown_category_preserves_revision(client):
    review_id = make_review(client).json()["reviewId"]
    response = client.post(
        commands_url(review_id),
        json={
            "commandId": "c-bad",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [{"id": "A1", "category": "RADIO"}],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ITEM_COUNT_OUT_OF_RANGE"
    # 版本仍是 1：原 expectedRevision=1 的确认可以成功。
    confirm = client.post(
        commands_url(review_id),
        json={"commandId": "c-ok", "expectedRevision": 1, "action": "CONFIRM"},
    )
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "FINALIZED"


# ---------- 确认与冻结 ----------


def test_confirm_freezes_snapshot(client):
    review_id = make_review(client).json()["reviewId"]
    response = client.post(
        commands_url(review_id),
        json={"commandId": "c2", "expectedRevision": 1, "action": "CONFIRM"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 1
    assert body["status"] == "FINALIZED"
    snapshot = body["snapshot"]
    assert snapshot["revision"] == 1
    assert snapshot["hold"] == "H1"
    assert [item["id"] for item in snapshot["request"]["items"]] == ["A1", "B2"]
    assert snapshot["decision"]["conclusion"] == "FORBID"


def test_confirm_after_replacements_freezes_current_revision(client):
    review_id = make_review(client).json()["reviewId"]
    client.post(
        commands_url(review_id),
        json={
            "commandId": "c2",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "WET"},
                {"id": "B2", "category": "WET"},
            ],
        },
    )
    confirm = client.post(
        commands_url(review_id),
        json={"commandId": "c3", "expectedRevision": 2, "action": "CONFIRM"},
    )
    assert confirm.status_code == 200
    body = confirm.json()
    assert body["status"] == "FINALIZED"
    assert body["snapshot"]["revision"] == 2
    assert body["snapshot"]["decision"]["conclusion"] == "ALLOW"


def test_confirm_replay_is_byte_identical(client):
    review_id = make_review(client).json()["reviewId"]
    command = {"commandId": "c2", "expectedRevision": 1, "action": "CONFIRM"}
    first = client.post(commands_url(review_id), json=command)
    for _ in range(3):
        retried = client.post(commands_url(review_id), json=command)
        assert retried.status_code == 200
        assert retried.content == first.content


def test_revision_advance_and_finalize_retries_all_byte_identical(client):
    """验收：版本推进或确认后的重试仍字节一致。"""
    review_id = make_review(client).json()["reviewId"]
    replace = {
        "commandId": "c2",
        "expectedRevision": 1,
        "action": "REPLACE",
        "items": [
            {"id": "A1", "category": "TOX"},
            {"id": "B2", "category": "FLAM"},
        ],
    }
    first_replace = client.post(commands_url(review_id), json=replace)
    assert first_replace.json()["revision"] == 2
    assert client.post(commands_url(review_id), json=replace).content == first_replace.content

    confirm = {"commandId": "c3", "expectedRevision": 2, "action": "CONFIRM"}
    first_confirm = client.post(commands_url(review_id), json=confirm)
    assert first_confirm.json()["status"] == "FINALIZED"
    assert client.post(commands_url(review_id), json=confirm).content == first_confirm.content


# ---------- 错误优先级 ----------


def test_command_against_unknown_review_returns_review_not_found(client):
    response = client.post(
        commands_url("does-not-exist"),
        json={"commandId": "x1", "expectedRevision": 1, "action": "CONFIRM"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_review_not_found_precedes_item_validation(client):
    # 评审不存在时，即便 items 非法也先报 REVIEW_NOT_FOUND。
    response = client.post(
        commands_url("missing"),
        json={
            "commandId": "x1",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [{"id": "A1", "category": "BOOM"}],
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_stale_expected_revision_returns_conflict_with_current_revision(client):
    review_id = make_review(client).json()["reviewId"]
    client.post(
        commands_url(review_id),
        json={
            "commandId": "c2",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "WET"},
                {"id": "B2", "category": "WET"},
            ],
        },
    )
    response = client.post(
        commands_url(review_id),
        json={
            "commandId": "c3",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "REVISION_CONFLICT"
    assert error["currentRevision"] == 2


def test_conflict_command_can_retry_against_new_revision(client):
    review_id = make_review(client).json()["reviewId"]
    # 先用 c2 把版本推到 2。
    client.post(
        commands_url(review_id),
        json={
            "commandId": "c2",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "WET"},
                {"id": "B2", "category": "WET"},
            ],
        },
    )
    # 争用失败的命令改按新版本重试即可成功（失败未消耗 commandId）。
    retry = client.post(
        commands_url(review_id),
        json={
            "commandId": "c3",
            "expectedRevision": 2,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    assert retry.status_code == 200
    assert retry.json()["revision"] == 3


def test_finalized_review_rejects_new_commands(client):
    review_id = make_review(client).json()["reviewId"]
    client.post(
        commands_url(review_id),
        json={"commandId": "c2", "expectedRevision": 1, "action": "CONFIRM"},
    )
    response = client.post(
        commands_url(review_id),
        json={
            "commandId": "c3",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "WET"},
                {"id": "B2", "category": "WET"},
            ],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_FINALIZED"


def test_revision_conflict_takes_precedence_over_finalized(client):
    review_id = make_review(client).json()["reviewId"]
    client.post(
        commands_url(review_id),
        json={
            "commandId": "c2",
            "expectedRevision": 1,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "WET"},
                {"id": "B2", "category": "WET"},
            ],
        },
    )
    client.post(
        commands_url(review_id),
        json={"commandId": "c3", "expectedRevision": 2, "action": "CONFIRM"},
    )
    # 已确认且 expectedRevision 过期：先报 REVISION_CONFLICT。
    response = client.post(
        commands_url(review_id),
        json={"commandId": "c4", "expectedRevision": 1, "action": "CONFIRM"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVISION_CONFLICT"


def test_command_id_reused_takes_precedence_over_state_errors(client):
    review_id = make_review(client).json()["reviewId"]
    client.post(
        commands_url(review_id),
        json={"commandId": "c2", "expectedRevision": 1, "action": "CONFIRM"},
    )
    # 已落地的确认命令 commandId 被拿去对不存在的评审发不同命令：仍先判重。
    response = client.post(
        commands_url("missing"),
        json={"commandId": "c2", "expectedRevision": 1, "action": "REPLACE",
              "items": [{"id": "A1", "category": "WET"}, {"id": "B2", "category": "WET"}]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_ID_REUSED"


def test_same_command_id_with_different_action_rejected(client):
    review_id = make_review(client).json()["reviewId"]
    client.post(
        commands_url(review_id),
        json={"commandId": "c2", "expectedRevision": 1, "action": "CONFIRM"},
    )
    response = client.post(
        commands_url(review_id),
        json={"commandId": "c2", "expectedRevision": 1, "action": "REPLACE",
              "items": [{"id": "A1", "category": "WET"}, {"id": "B2", "category": "WET"}]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_ID_REUSED"


def test_same_command_id_with_different_expected_revision_rejected(client):
    review_id = make_review(client).json()["reviewId"]
    client.post(
        commands_url(review_id),
        json={"commandId": "c2", "expectedRevision": 1, "action": "CONFIRM"},
    )
    response = client.post(
        commands_url(review_id),
        json={"commandId": "c2", "expectedRevision": 2, "action": "CONFIRM"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_ID_REUSED"


@pytest.mark.parametrize("payload", [
    {"commandId": "z1"},
    {"commandId": "z1", "expectedRevision": 1},
    {"commandId": "z1", "expectedRevision": 1, "action": "WHAT"},
    {"commandId": "z1", "expectedRevision": 0, "action": "CONFIRM"},
    {"commandId": "z1", "expectedRevision": -1, "action": "CONFIRM"},
    {"commandId": "z1", "expectedRevision": True, "action": "CONFIRM"},
    {"commandId": "z1", "expectedRevision": "1", "action": "CONFIRM"},
    {"commandId": "z1", "expectedRevision": 1, "action": "REPLACE"},
])
def test_malformed_command_rejected(client, payload):
    review_id = make_review(client).json()["reviewId"]
    response = client.post(commands_url(review_id), json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_command_malformed_json(client):
    review_id = make_review(client).json()["reviewId"]
    response = client.post(
        commands_url(review_id),
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


# ---------- 并发原子性 ----------


def test_concurrent_commands_on_same_revision_only_one_commits(client):
    """并发替换与确认争用同一版本：仅一个成功，失败者无部分状态。"""
    review_id = make_review(client, command_id="base").json()["reviewId"]
    contenders = [
        ("p0", "REPLACE", [
            {"id": "A1", "category": "WET"},
            {"id": "B2", "category": "WET"},
        ]),
        ("q0", "CONFIRM", None),
        ("p1", "REPLACE", [
            {"id": "A1", "category": "GAS"},
            {"id": "B2", "category": "GAS"},
        ]),
        ("q1", "CONFIRM", None),
        ("p2", "REPLACE", [
            {"id": "A1", "category": "TOX"},
            {"id": "B2", "category": "TOX"},
        ]),
    ]
    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(len(contenders))

    def fire(command_id, action, items):
        thread_client = TestClient(app)
        barrier.wait()
        payload = {
            "commandId": command_id,
            "expectedRevision": 1,
            "action": action,
        }
        if items is not None:
            payload["items"] = items
        response = thread_client.post(commands_url(review_id), json=payload)
        results.append((response.status_code, response.json()))

    threads = [
        threading.Thread(target=fire, args=args) for args in contenders
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [body for status, body in results if status == 200]
    failures = [
        body for status, body in results if status == 409
        and body["error"]["code"] in ("REVISION_CONFLICT", "REVIEW_FINALIZED")
    ]
    assert len(successes) == 1
    assert len(failures) == len(contenders) - 1

    winner = successes[0]
    if winner["status"] == "FINALIZED":
        # 确认胜出：快照冻结在 revision 1，版本不递增；其余命令因评审已
        # 冻结而失败（REVIEW_FINALIZED），且之后的任何替换都被拒绝。
        assert winner["snapshot"]["revision"] == 1
        assert all(
            body["error"]["code"] == "REVIEW_FINALIZED" for body in failures
        )
        follow_up = client.post(
            commands_url(review_id),
            json={
                "commandId": "late",
                "expectedRevision": 1,
                "action": "REPLACE",
                "items": [
                    {"id": "A1", "category": "WET"},
                    {"id": "B2", "category": "WET"},
                ],
            },
        )
        assert follow_up.status_code == 409
        assert follow_up.json()["error"]["code"] == "REVIEW_FINALIZED"
    else:
        # 替换胜出：版本恰好推进一次到 2，落后者全部 REVISION_CONFLICT。
        assert winner["revision"] == 2
        assert all(
            body["error"]["code"] == "REVISION_CONFLICT" for body in failures
        )
        categories = {item["category"] for item in winner["request"]["items"]}
        assert len(categories) == 1
        assert winner["decision"]["conclusion"] == "ALLOW"
        # 争用失败的确认可按新版本重试并成功冻结。
        confirm = client.post(
            commands_url(review_id),
            json={"commandId": "retry-confirm", "expectedRevision": 2,
                  "action": "CONFIRM"},
        )
        assert confirm.status_code == 200
        assert confirm.json()["status"] == "FINALIZED"
        assert confirm.json()["snapshot"]["decision"] == winner["decision"]


def test_concurrent_replaces_exactly_one_winner_retryable(client):
    review_id = make_review(client, command_id="base").json()["reviewId"]
    n = 10
    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(n)

    def fire(index):
        thread_client = TestClient(app)
        barrier.wait()
        response = thread_client.post(
            commands_url(review_id),
            json={
                "commandId": f"r{index}",
                "expectedRevision": 1,
                "action": "REPLACE",
                "items": [
                    {"id": "A1", "category": "WET"},
                    {"id": "B2", "category": "WET"},
                ],
            },
        )
        results.append((response.status_code, response.json()))

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for status, _ in results if status == 200) == 1
    assert sum(
        1 for status, body in results
        if status == 409 and body["error"]["code"] == "REVISION_CONFLICT"
    ) == n - 1
    # 失败者之一用新 commandId 按 revision 2 重试成功。
    retry = client.post(
        commands_url(review_id),
        json={
            "commandId": "r-retry",
            "expectedRevision": 2,
            "action": "REPLACE",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "GAS"},
            ],
        },
    )
    assert retry.status_code == 200
    assert retry.json()["revision"] == 3
