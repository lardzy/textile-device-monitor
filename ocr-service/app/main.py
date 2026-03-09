from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import os
from typing import Any
import mimetypes
import re

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
import requests
import pypdfium2 as pdfium

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


def _max_pdf_pages() -> int:
    return max(1, int(os.getenv("OCR_ADAPTER_MAX_PDF_PAGES", "30")))


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


def _parse_pdf_page_range(page_range: str | None, total_pages: int) -> list[int]:
    if total_pages <= 0:
        return []
    if not page_range or not page_range.strip():
        return list(range(total_pages))

    result: list[int] = []
    for part in page_range.split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", item)
            if not match:
                raise ValueError("invalid_page_range")
            start = int(match.group(1))
            end = int(match.group(2))
            if start <= 0 or end <= 0 or start > end:
                raise ValueError("invalid_page_range")
            for page in range(start, end + 1):
                if page <= total_pages:
                    idx = page - 1
                    if idx not in result:
                        result.append(idx)
        else:
            if not item.isdigit():
                raise ValueError("invalid_page_range")
            page = int(item)
            if page <= 0:
                raise ValueError("invalid_page_range")
            if page <= total_pages:
                idx = page - 1
                if idx not in result:
                    result.append(idx)

    if not result:
        raise ValueError("page_range_out_of_bounds")
    return sorted(result)


def _pdf_to_page_images(
    content: bytes, page_range: str | None
) -> list[tuple[int, str]]:
    try:
        document = pdfium.PdfDocument(io.BytesIO(content))
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError("invalid_pdf") from exc

    try:
        total_pages = len(document)
        if total_pages <= 0:
            raise ValueError("invalid_pdf")

        selected_pages = _parse_pdf_page_range(page_range, total_pages)
        if len(selected_pages) > _max_pdf_pages():
            raise ValueError("pdf_page_limit_exceeded")

        images: list[tuple[int, str]] = []
        for page_index in selected_pages:
            page = document[page_index]
            bitmap = page.render(scale=2.0)
            pil_image = bitmap.to_pil()
            output = io.BytesIO()
            pil_image.save(output, format="PNG", optimize=True)
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            images.append((page_index + 1, f"data:image/png;base64,{encoded}"))
            pil_image.close()
            page.close()
        if not images:
            raise ValueError("invalid_pdf")
        return images
    finally:
        document.close()


def _call_upstream(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    upstream_url = _upstream_url().strip()
    if not upstream_url:
        return None, _error_response(
            status_code=503,
            error="ocr_service_unreachable",
            message="GLM_OCR_UPSTREAM_URL is empty",
        )

    try:
        upstream_resp = requests.post(
            upstream_url,
            json=payload,
            timeout=_timeout_seconds(),
        )
    except requests.Timeout:
        return None, _error_response(504, "ocr_timeout", "Upstream OCR timeout")
    except requests.RequestException as exc:
        return None, _error_response(502, "ocr_service_unreachable", str(exc))

    if upstream_resp.status_code != 200:
        error_code = "ocr_inference_failed"
        error_message = f"Upstream OCR status: {upstream_resp.status_code}"
        try:
            upstream_payload = upstream_resp.json()
            if isinstance(upstream_payload, dict):
                raw_error = (
                    upstream_payload.get("error_code")
                    or upstream_payload.get("error")
                    or upstream_payload.get("detail")
                    or upstream_payload.get("message")
                )
                if raw_error:
                    if str(raw_error).lower() == "oom":
                        error_code = "oom"
                    error_message = str(raw_error)
        except ValueError:
            if upstream_resp.text:
                error_message = upstream_resp.text
        return None, _error_response(
            upstream_resp.status_code, error_code, error_message
        )

    try:
        upstream_payload = upstream_resp.json()
    except ValueError:
        return None, _error_response(
            502, "ocr_inference_failed", "Invalid upstream payload"
        )

    return _normalize_upstream_payload(upstream_payload), None


def _prefix_error_with_page(
    error_response: JSONResponse, page_number: int
) -> JSONResponse:
    try:
        payload = json.loads(bytes(error_response.body).decode("utf-8"))
    except Exception:
        return error_response

    if isinstance(payload, dict):
        message = str(
            payload.get("message") or payload.get("error") or "ocr_inference_failed"
        )
        payload["message"] = f"page_{page_number}: {message}"
        return JSONResponse(status_code=error_response.status_code, content=payload)
    return error_response


def _merge_page_results(
    page_results: list[tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    if len(page_results) == 1:
        _, result = page_results[0]
        return result

    markdown_parts = []
    json_pages = []
    for page_number, result in page_results:
        markdown_parts.append(result.get("markdown_text") or "")
        json_pages.append(
            {
                "page_number": page_number,
                "data": result.get("json_data"),
            }
        )

    merged_markdown = "\n\n".join(part for part in markdown_parts if part)
    return {
        "markdown_text": merged_markdown,
        "json_data": {"pages": json_pages},
    }


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

    payload: dict[str, Any]
    normalized: dict[str, Any] | None = None
    if extension == ".pdf":
        try:
            page_images = _pdf_to_page_images(content, page_range)
        except ValueError as exc:
            error = str(exc)
            if error in {
                "invalid_pdf",
                "invalid_page_range",
                "page_range_out_of_bounds",
            }:
                return _error_response(400, error, error)
            if error == "pdf_page_limit_exceeded":
                return _error_response(413, error, error)
            return _error_response(500, "pdf_processing_failed", error)
        except Exception:
            return _error_response(
                500, "pdf_processing_failed", "pdf_processing_failed"
            )

        page_results: list[tuple[int, dict[str, Any]]] = []
        for page_number, data_uri in page_images:
            payload = {"images": [data_uri]}
            if note:
                payload["note"] = note
            if output_format:
                payload["output_format"] = output_format
            page_result, error_response = _call_upstream(payload)
            if error_response is not None:
                return _prefix_error_with_page(error_response, page_number)
            if page_result is None:
                return _error_response(
                    500, "ocr_inference_failed", "ocr_inference_failed"
                )
            page_results.append((page_number, page_result))

        normalized = _merge_page_results(page_results)
    else:
        content_type = _resolve_content_type(filename, file.content_type)
        encoded = base64.b64encode(content).decode("ascii")
        data_uri = f"data:{content_type};base64,{encoded}"
        payload = {"images": [data_uri]}
        if note:
            payload["note"] = note
        if output_format:
            payload["output_format"] = output_format
        normalized, error_response = _call_upstream(payload)
        if error_response is not None:
            return error_response

    if normalized is None:
        return _error_response(500, "ocr_inference_failed", "ocr_inference_failed")

    normalized["meta"] = {"source": "glm-ocr-adapter"}
    return normalized
