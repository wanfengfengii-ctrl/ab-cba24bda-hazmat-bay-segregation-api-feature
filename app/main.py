"""港口危险品配载预审 API。"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.rules import assess, removal_impact
from app.validation import ApiError, validate_payload

app = FastAPI(title="Dangerous Goods Stowage Pre-review", version="1.0.0")


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


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
