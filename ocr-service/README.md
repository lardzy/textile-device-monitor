# OCR Adapter Service

This service provides a stable `/v1/ocr/parse` HTTP interface for the monitor backend.

It forwards file parsing requests to a GLM-OCR upstream service and normalizes response
payloads to:

```json
{
  "markdown_text": "...",
  "json_data": {}
}
```

## Environment

- `GLM_OCR_UPSTREAM_URL`: upstream endpoint, default `http://glm-ocr-runtime:5002/v1/ocr/parse`
- `OCR_ADAPTER_TIMEOUT_SECONDS`: request timeout, default `600`
- `OCR_ADAPTER_MAX_UPLOAD_MB`: max upload size, default `30`

## Run

```bash
docker build -t textile-ocr-adapter .
docker run --rm -p 5002:5002 \
  -e GLM_OCR_UPSTREAM_URL=http://glm-ocr-runtime:5002/v1/ocr/parse \
  textile-ocr-adapter
```
