"""港口危险品配载预审 API。"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.rules import assess, removal_impact
from app.store import store
from app.validation import ApiError, validate_payload

app = FastAPI(title="Dangerous Goods Stowage Pre-review", version="1.0.0")

REVIEWS_URL = "/api/v1/stowage/reviews"


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    error = {"code": exc.code, "message": exc.message}
    if exc.extra:
        error.update(exc.extra)
    return JSONResponse(status_code=exc.status, content={"error": error})


def _require_command_id(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ApiError("INVALID_REQUEST", "Request body must be a JSON object.")
    command_id = payload.get("commandId")
    if not isinstance(command_id, str) or not command_id.strip():
        raise ApiError(
            "INVALID_REQUEST",
            "Field 'commandId' must be a non-empty string.",
        )
    return command_id


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/stowage/assess")
async def assess_stowage(request: Request) -> JSONResponse:
    try:
        payload = json.loads(await request.body())
    except (UnicodeDecodeError, ValueError):
        raise ApiError("INVALID_JSON", "Request body must be valid JSON.")

    hold, items = validate_payload(payload)
    result = assess(items)
    return JSONResponse(
        status_code=200,
        content={
            "hold": hold,
            "conclusion": result["conclusion"],
            "evidence": result["evidence"],
        },
    )


@app.post("/api/v1/stowage/removal-impact")
async def removal_impact_stowage(request: Request) -> JSONResponse:
    try:
        payload = json.loads(await request.body())
    except (UnicodeDecodeError, ValueError):
        raise ApiError("INVALID_JSON", "Request body must be valid JSON.")

    hold, items = validate_payload(payload)
    analysis = removal_impact(items)
    return JSONResponse(
        status_code=200,
        content={
            "hold": hold,
            "original": analysis["original"],
            "removals": analysis["removals"],
            "recommendations": analysis["recommendations"],
        },
    )


@app.post(REVIEWS_URL)
async def create_review(request: Request) -> JSONResponse:
    """以舱位、货项和 commandId 建立 revision=1 的草稿。

    保存规范化请求（货项按编号升序）及裁决；commandId 判重：同标识同
    内容原样重放，同标识不同内容返回 409 COMMAND_ID_REUSED。
    """
    try:
        payload = json.loads(await request.body())
    except (UnicodeDecodeError, ValueError):
        raise ApiError("INVALID_JSON", "Request body must be valid JSON.")

    command_id = _require_command_id(payload)
    _review_id, content, status = store.create_review(command_id, payload)
    return JSONResponse(status_code=status, content=content)


@app.post(f"{REVIEWS_URL}/{{review_id}}/commands")
async def review_command(review_id: str, request: Request) -> JSONResponse:
    """对草稿下达替换货项或确认命令。

    命令携带 commandId 与 expectedRevision：请求先按 commandId 判重（同标识
    同内容原样重放，否则 409 COMMAND_ID_REUSED）；新命令按 REVIEW_NOT_FOUND、
    REVISION_CONFLICT、REVIEW_FINALIZED 的顺序报错。争用同一版本的多个
    命令只有一个原子成功。
    """
    try:
        payload = json.loads(await request.body())
    except (UnicodeDecodeError, ValueError):
        raise ApiError("INVALID_JSON", "Request body must be valid JSON.")

    command_id = _require_command_id(payload)
    content, status = store.apply_command(review_id, command_id, payload)
    return JSONResponse(status_code=status, content=content)
