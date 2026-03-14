from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.engine import InferServiceError, engine

app = FastAPI(title="area-infer", version="0.1.0")
logger = logging.getLogger("area-infer")


class WarmupRequest(BaseModel):
    model_name: str = Field(..., min_length=1)
    model_file: str = Field(..., min_length=1)


class InferRequest(BaseModel):
    model_name: str = Field(..., min_length=1)
    model_file: str = Field(..., min_length=1)
    image_bytes_b64: str = Field(..., min_length=1)
    inference_options: dict[str, Any] | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        return engine.health()
    except InferServiceError as exc:
        logger.exception("health_failed code=%s message=%s", exc.code, exc.message)
        raise HTTPException(status_code=503, detail=exc.code) from exc


@app.post("/v1/warmup")
def warmup(payload: WarmupRequest) -> dict[str, Any]:
    try:
        return engine.warmup(
            model_name=payload.model_name,
            model_file=payload.model_file,
        )
    except InferServiceError as exc:
        logger.exception("warmup_failed code=%s message=%s", exc.code, exc.message)
        status = 400 if exc.code == "infer_model_load_failed" else 503
        raise HTTPException(status_code=status, detail=exc.code) from exc


@app.post("/v1/infer")
def infer(payload: InferRequest) -> dict[str, Any]:
    try:
        return engine.infer(
            model_name=payload.model_name,
            model_file=payload.model_file,
            image_bytes_b64=payload.image_bytes_b64,
            inference_options=payload.inference_options,
        )
    except InferServiceError as exc:
        logger.exception("infer_failed code=%s message=%s", exc.code, exc.message)
        if exc.code == "infer_timeout":
            status = 504
        elif exc.code in {"infer_model_load_failed", "infer_bad_response"}:
            status = 400
        else:
            status = 503
        raise HTTPException(status_code=status, detail=exc.code) from exc
