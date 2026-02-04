from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
import requests
from urllib.parse import quote
from app.database import get_db
from app.crud import devices as device_crud
from app.config import settings
from threading import Lock, Event
from time import monotonic
from typing import Any

router = APIRouter(prefix="/results", tags=["results"])

_RECENT_CACHE_LOCK = Lock()
_RECENT_CACHE: dict[str, dict[str, Any]] = {}


def _get_recent_cache_key(device_id: int, limit: int) -> str:
    return f"recent:{device_id}:{limit}"


def _get_recent_cached_value(key: str) -> Any | None:
    now = monotonic()
    with _RECENT_CACHE_LOCK:
        entry = _RECENT_CACHE.get(key)
        if not entry:
            return None
        expires_at = entry.get("expires_at", 0)
        if expires_at and expires_at > now and "value" in entry:
            return entry["value"]
    return None


def _get_recent_stale_value(key: str) -> Any | None:
    now = monotonic()
    with _RECENT_CACHE_LOCK:
        entry = _RECENT_CACHE.get(key)
        if not entry:
            return None
        stale_expires_at = entry.get("stale_expires_at", 0)
        if stale_expires_at and stale_expires_at > now and "stale_value" in entry:
            return entry["stale_value"]
    return None


def _get_recent_inflight_state(key: str) -> tuple[Event | None, float | None]:
    with _RECENT_CACHE_LOCK:
        entry = _RECENT_CACHE.get(key)
        if not entry or not entry.get("in_flight"):
            return None, None
        event = entry.get("event")
        started_at = entry.get("started_at")
        if isinstance(event, Event):
            return event, started_at if isinstance(started_at, (int, float)) else None
    return None, None


def _mark_recent_inflight(key: str) -> Event:
    event = Event()
    with _RECENT_CACHE_LOCK:
        entry = _RECENT_CACHE.get(key, {})
        entry["in_flight"] = True
        entry["event"] = event
        entry["started_at"] = monotonic()
        _RECENT_CACHE[key] = entry
    return event


def _finish_recent_inflight(key: str, value: Any | None) -> None:
    now = monotonic()
    with _RECENT_CACHE_LOCK:
        entry = _RECENT_CACHE.get(key, {})
        if value is not None:
            entry["value"] = value
            entry["expires_at"] = now + settings.RESULTS_RECENT_CACHE_TTL
            entry["stale_value"] = value
            entry["stale_expires_at"] = now + settings.RESULTS_RECENT_CACHE_STALE_TTL
        entry["in_flight"] = False
        event = entry.get("event")
        _RECENT_CACHE[key] = entry
        if isinstance(event, Event):
            event.set()


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
    cache_key = _get_recent_cache_key(device_id, limit)
    cached = _get_recent_cached_value(cache_key)
    if cached is not None:
        return cached

    inflight_event, _ = _get_recent_inflight_state(cache_key)
    if inflight_event is not None:
        inflight_event.wait(timeout=settings.RESULTS_RECENT_INFLIGHT_WAIT_SECONDS)
        cached = _get_recent_cached_value(cache_key)
        if cached is not None:
            return cached
        stale = _get_recent_stale_value(cache_key)
        if stale is not None:
            return stale
        inflight_event, _ = _get_recent_inflight_state(cache_key)
        if inflight_event is not None:
            raise HTTPException(status_code=502, detail="Client unreachable")

    inflight_event = _mark_recent_inflight(cache_key)
    base_url = _get_client_base_url(db, device_id)
    try:
        resp = requests.get(
            f"{base_url}/client/results/recent",
            params={"limit": limit},
            timeout=10,
        )
    except requests.RequestException as exc:
        _finish_recent_inflight(cache_key, None)
        stale = _get_recent_stale_value(cache_key)
        if stale is not None:
            return stale
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        _finish_recent_inflight(cache_key, None)
        stale = _get_recent_stale_value(cache_key)
        if stale is not None:
            return stale
        raise HTTPException(status_code=resp.status_code, detail="Client error")
    payload = resp.json()
    _finish_recent_inflight(cache_key, payload)
    return payload


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


@router.get("/thumb/{filename}")
def get_thumbnail(
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
            f"{base_url}/client/results/thumb/{safe_filename}",
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Client unreachable") from exc
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code, detail=_extract_client_error(resp)
        )
    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "image/jpeg"),
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
