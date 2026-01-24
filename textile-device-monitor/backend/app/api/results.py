from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
import requests
from urllib.parse import quote
from app.database import get_db
from app.crud import devices as device_crud

router = APIRouter(prefix="/results", tags=["results"])


def _extract_client_error(resp: requests.Response) -> str:
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            return (
                payload.get("detail")
                or payload.get("error")
                or payload.get("message")
                or resp.text
            )
    except ValueError:
        if resp.text:
            return resp.text
    return "Client error"


def _get_client_base_url(db: Session, device_id: int) -> str:
    device = device_crud.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    base_url = device.client_base_url
    if base_url is None:
        raise HTTPException(status_code=404, detail="Client base URL not configured")
    if not str(base_url).strip():  # type: ignore[arg-type]
        raise HTTPException(status_code=404, detail="Client base URL not configured")
    return base_url.rstrip("/")


@router.get("/latest")
def get_latest(device_id: int = Query(...), db: Session = Depends(get_db)):
    base_url = _get_client_base_url(db, device_id)
    try:
        resp = requests.get(f"{base_url}/client/results/latest", timeout=10)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code, detail=_extract_client_error(resp)
        )
    return resp.json()


@router.get("/table")
def get_table(
    device_id: int = Query(...),
    folder: str | None = Query(None),
    db: Session = Depends(get_db),
):
    base_url = _get_client_base_url(db, device_id)
    try:
        params = {"folder": folder} if folder else None
        resp = requests.get(
            f"{base_url}/client/results/table",
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Client error")
    headers = {}
    content_disposition = resp.headers.get("Content-Disposition")
    if content_disposition:
        headers["Content-Disposition"] = content_disposition
    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "application/octet-stream"),
        headers=headers,
    )


@router.get("/table_preview")
def get_table_preview(
    device_id: int = Query(...),
    folder: str | None = Query(None),
    db: Session = Depends(get_db),
):
    base_url = _get_client_base_url(db, device_id)
    try:
        params = {"folder": folder} if folder else None
        resp = requests.get(
            f"{base_url}/client/results/table_preview",
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code, detail=_extract_client_error(resp)
        )
    headers = {}
    content_disposition = resp.headers.get("Content-Disposition")
    if content_disposition:
        headers["Content-Disposition"] = content_disposition
    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "application/octet-stream"),
        headers=headers,
    )


@router.get("/table_view")
def get_table_view(
    device_id: int = Query(...),
    folder: str | None = Query(None),
    db: Session = Depends(get_db),
):
    base_url = _get_client_base_url(db, device_id)
    try:
        params = {"folder": folder} if folder else None
        resp = requests.get(
            f"{base_url}/client/results/table_view",
            params=params,
            timeout=10,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code not in (200, 202):
        raise HTTPException(
            status_code=resp.status_code, detail=_extract_client_error(resp)
        )
    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "application/octet-stream"),
        status_code=resp.status_code,
    )


@router.get("/images")
def get_images(
    device_id: int = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    folder: str | None = Query(None),
    db: Session = Depends(get_db),
):
    base_url = _get_client_base_url(db, device_id)
    try:
        params: dict[str, int | str] = {"page": page, "page_size": page_size}
        if folder:
            params["folder"] = folder
        resp = requests.get(
            f"{base_url}/client/results/images",
            params=params,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Client error")
    return resp.json()


@router.get("/recent")
def get_recent(
    device_id: int = Query(...),
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
):
    base_url = _get_client_base_url(db, device_id)
    try:
        resp = requests.get(
            f"{base_url}/client/results/recent",
            params={"limit": limit},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Client error")
    return resp.json()


@router.get("/image/{filename}")
def get_image(
    filename: str,
    device_id: int = Query(...),
    folder: str | None = Query(None),
    db: Session = Depends(get_db),
):
    base_url = _get_client_base_url(db, device_id)
    safe_filename = quote(filename, safe="")
    params = {"folder": folder} if folder else None
    try:
        resp = requests.get(
            f"{base_url}/client/results/image/{safe_filename}",
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Client error")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "image/png"),
    )


@router.post("/cleanup")
def cleanup_images(
    device_id: int = Query(...),
    folder: str | None = Query(None),
    db: Session = Depends(get_db),
):
    base_url = _get_client_base_url(db, device_id)
    try:
        params = {"folder": folder} if folder else None
        resp = requests.post(
            f"{base_url}/client/results/cleanup",
            params=params,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        detail = "Client error"
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                detail = payload.get("error") or detail
        except ValueError:
            if resp.text:
                detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()
