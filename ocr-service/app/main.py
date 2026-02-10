from __future__ import annotations

import base64
from pathlib import Path
import os
from typing import Any
import mimetypes

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
import requests

app = FastAPI(
    title="OCR Adapter Service",
    description="Adapter service for GLM-OCR upstream runtime",
    version="1.0.0",
)

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}

EXTENSION_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _upstream_url() -> str:
    return os.getenv("GLM_OCR_UPSTREAM_URL", "http://glm-ocr-runtime:5002/glmocr/parse")


def _timeout_seconds() -> int:
    return int(os.getenv("OCR_ADAPTER_TIMEOUT_SECONDS", "600"))


def _max_upload_mb() -> int:
    return max(1, int(os.getenv("OCR_ADAPTER_MAX_UPLOAD_MB", "30")))


def _error_response(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "error_code": error, "message": message},
    )


def _resolve_content_type(filename: str, incoming_content_type: str | None) -> str:
    incoming = (incoming_content_type or "").split(";", 1)[0].strip().lower()
    if incoming and incoming != "application/octet-stream":
        return incoming

    ext = Path(filename).suffix.lower()
    mapped = EXTENSION_CONTENT_TYPES.get(ext)
    if mapped:
        return mapped

    guessed, _ = mimetypes.guess_type(filename)
    if guessed:
        return guessed

    return "application/octet-stream"


def _normalize_upstream_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"markdown_text": "", "json_data": {}}

    markdown_text = payload.get("markdown_text")
    if markdown_text is None:
        markdown_text = payload.get("markdown")
    if markdown_text is None:
        markdown_text = payload.get("markdown_result")
    if markdown_text is None:
        markdown_text = payload.get("text", "")
    if not isinstance(markdown_text, str):
        markdown_text = str(markdown_text)

    json_data = payload.get("json_data")
    if json_data is None:
        json_data = payload.get("json")
    if json_data is None:
        json_data = payload.get("json_result")
    if json_data is None:
        json_data = payload.get("structured")
    if json_data is None:
        json_data = {}

    return {
        "markdown_text": markdown_text,
        "json_data": json_data,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/ocr/parse")
async def parse_document(
    file: UploadFile = File(...),
    page_range: str | None = Form(None),
    note: str | None = Form(None),
    output_format: str | None = Form(None),
):
    filename = Path(file.filename or "upload").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="invalid_file_type")

    content = await file.read()
    await file.close()
    if len(content) > _max_upload_mb() * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file_too_large")

    upstream_url = _upstream_url().strip()
    if not upstream_url:
        return _error_response(
            status_code=503,
            error="ocr_service_unreachable",
            message="GLM_OCR_UPSTREAM_URL is empty",
        )

    content_type = _resolve_content_type(filename, file.content_type)
    encoded = base64.b64encode(content).decode("ascii")
    data_uri = f"data:{content_type};base64,{encoded}"

    payload: dict[str, Any] = {"images": [data_uri]}
    if page_range:
        payload["page_range"] = page_range
    if note:
        payload["note"] = note
    if output_format:
        payload["output_format"] = output_format

    try:
        upstream_resp = requests.post(
            upstream_url,
            json=payload,
            timeout=_timeout_seconds(),
        )
    except requests.Timeout:
        return _error_response(504, "ocr_timeout", "Upstream OCR timeout")
    except requests.RequestException as exc:
        return _error_response(502, "ocr_service_unreachable", str(exc))

    if upstream_resp.status_code != 200:
        error_code = "ocr_inference_failed"
        error_message = f"Upstream OCR status: {upstream_resp.status_code}"
        try:
            payload = upstream_resp.json()
            if isinstance(payload, dict):
                raw_error = (
                    payload.get("error_code")
                    or payload.get("error")
                    or payload.get("detail")
                )
                if raw_error:
                    if str(raw_error).lower() == "oom":
                        error_code = "oom"
                    error_message = str(raw_error)
        except ValueError:
            if upstream_resp.text:
                error_message = upstream_resp.text
        return _error_response(upstream_resp.status_code, error_code, error_message)

    try:
        payload = upstream_resp.json()
    except ValueError:
        return _error_response(502, "ocr_inference_failed", "Invalid upstream payload")

    normalized = _normalize_upstream_payload(payload)
    normalized["meta"] = {"source": "glm-ocr-adapter"}
    return normalized
