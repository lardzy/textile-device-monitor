from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError
from app.execution.models import (
    ExecutionCredential,
    ExecutionLoginThrottle,
    ExecutionPermission,
    ExecutionRolePermission,
    ExecutionSession,
    ExecutionUser,
    ExecutionUserRole,
    new_id,
    utcnow,
)


SESSION_COOKIE = "execution_session"
PASSWORD_ITERATIONS = 600_000


def _setting(name: str, default):
    return getattr(settings, name, default)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


DUMMY_PASSWORD_HASH = hash_password(
    "execution-system-nonexistent-user-timing-placeholder"
)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = password_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(raw_digest.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_token_match(value: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(value), expected_hash)


def login_throttle_key(username: str, client_address: str) -> str:
    secret = str(
        _setting("EXECUTION_SESSION_SECRET", "") or settings.SECRET_KEY
    ).encode("utf-8")
    material = (
        f"{username.strip().casefold()}\0{client_address.strip()}"
    ).encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def require_login_not_throttled(db: Session, key_hash: str) -> None:
    record = db.get(ExecutionLoginThrottle, key_hash)
    blocked_until = _aware(record.blocked_until) if record is not None else None
    now = utcnow()
    if blocked_until is not None and blocked_until > now:
        retry_after = max(int((blocked_until - now).total_seconds()) + 1, 1)
        raise ExecutionApiError(
            429,
            "login_rate_limited",
            "登录尝试过于频繁，请稍后再试",
            details={"retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


def record_login_failure(db: Session, key_hash: str) -> None:
    record = (
        db.query(ExecutionLoginThrottle)
        .filter(ExecutionLoginThrottle.key_hash == key_hash)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if record is None:
        try:
            with db.begin_nested():
                record = ExecutionLoginThrottle(
                    key_hash=key_hash,
                    failure_count=0,
                )
                db.add(record)
                db.flush()
        except IntegrityError:
            record = (
                db.query(ExecutionLoginThrottle)
                .filter(ExecutionLoginThrottle.key_hash == key_hash)
                .populate_existing()
                .with_for_update()
                .one()
            )
    now = utcnow()
    last_failed_at = _aware(record.last_failed_at)
    if (
        last_failed_at is None
        or last_failed_at < now - timedelta(minutes=15)
    ):
        record.failure_count = 0
    record.failure_count += 1
    record.last_failed_at = now
    if record.failure_count >= 5:
        block_seconds = min(30 * (2 ** (record.failure_count - 5)), 900)
        record.blocked_until = now + timedelta(seconds=block_seconds)


def clear_login_failures(db: Session, key_hash: str) -> None:
    db.query(ExecutionLoginThrottle).filter(
        ExecutionLoginThrottle.key_hash == key_hash
    ).delete(synchronize_session=False)


def create_session(db: Session, user: ExecutionUser) -> tuple[ExecutionSession, str, str]:
    now = utcnow()
    db.query(ExecutionSession).filter(
        or_(
            ExecutionSession.expires_at < now,
            (
                ExecutionSession.revoked_at.is_not(None)
                & (
                    ExecutionSession.revoked_at
                    < now - timedelta(days=7)
                )
            ),
        )
    ).delete(synchronize_session=False)
    session_token = secrets.token_urlsafe(48)
    session_id = new_id()
    token_hash = hash_token(session_token)
    ttl_hours = int(_setting("EXECUTION_SESSION_TTL_HOURS", 12))
    session = ExecutionSession(
        id=session_id,
        user_id=user.id,
        token_hash=token_hash,
        csrf_hash="",
        expires_at=now + timedelta(hours=ttl_hours),
    )
    csrf_token = csrf_token_for_session(session)
    session.csrf_hash = hash_token(csrf_token)
    db.add(session)
    db.flush()
    return session, session_token, csrf_token


def csrf_token_for_session(session: ExecutionSession) -> str:
    secret = str(
        _setting("EXECUTION_SESSION_SECRET", "") or settings.SECRET_KEY
    ).encode("utf-8")
    material = f"execution-csrf:{session.id}:{session.token_hash}".encode("utf-8")
    return base64.urlsafe_b64encode(
        hmac.new(secret, material, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def resolve_session(
    db: Session,
    raw_token: Optional[str],
    *,
    touch: bool = True,
) -> tuple[ExecutionSession, ExecutionUser]:
    if not raw_token:
        raise ExecutionApiError(401, "authentication_required", "请先登录执行系统")
    session = (
        db.query(ExecutionSession)
        .filter(ExecutionSession.token_hash == hash_token(raw_token))
        .one_or_none()
    )
    now = utcnow()
    expires_at = session.expires_at if session is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        session is None
        or session.revoked_at is not None
        or expires_at <= now
        or not session.user.is_active
    ):
        raise ExecutionApiError(401, "session_invalid", "登录状态已失效，请重新登录")
    if touch:
        session.last_seen_at = now
    return session, session.user


def require_csrf(session: ExecutionSession, token: Optional[str]) -> None:
    if not token or not constant_time_token_match(token, session.csrf_hash):
        raise ExecutionApiError(403, "csrf_invalid", "请求校验失败，请刷新页面后重试")


def require_role(user: ExecutionUser, role: str) -> None:
    if role == "admin" and user.role != "admin":
        raise ExecutionApiError(403, "permission_denied", "该操作需要管理员权限")


def has_permission(db: Session, user: ExecutionUser, permission_key: str) -> bool:
    if not user.is_active:
        return False
    binding = (
        db.query(ExecutionRolePermission.id)
        .join(
            ExecutionUserRole,
            ExecutionUserRole.role_id == ExecutionRolePermission.role_id,
        )
        .join(
            ExecutionPermission,
            ExecutionPermission.id == ExecutionRolePermission.permission_id,
        )
        .filter(
            ExecutionUserRole.user_id == user.id,
            ExecutionPermission.key == permission_key,
        )
        .first()
    )
    if binding is not None:
        return True
    # Compatibility fallback for databases stamped before role bindings were
    # backfilled. The bootstrap path always creates explicit bindings.
    return user.role == "admin"


def require_permission(
    db: Session,
    user: ExecutionUser,
    permission_key: str,
) -> None:
    if not has_permission(db, user, permission_key):
        raise ExecutionApiError(403, "permission_denied", "您没有执行该操作的权限")


def _credential_fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise ExecutionApiError(
            503,
            "credential_crypto_unavailable",
            "凭据加密组件不可用",
        ) from exc
    raw_key = str(
        _setting("EXECUTION_CREDENTIAL_KEY", "")
        or _setting("EXECUTION_SESSION_SECRET", "")
        or settings.SECRET_KEY
    )
    if not raw_key or raw_key == "your-secret-key-change-in-production":
        raise ExecutionApiError(
            503,
            "credential_key_not_configured",
            "执行系统凭据加密密钥尚未配置",
        )
    derived = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_credential(value: str) -> str:
    return _credential_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_credential(record: ExecutionCredential) -> str:
    try:
        return _credential_fernet().decrypt(
            record.encrypted_secret.encode("ascii")
        ).decode("utf-8")
    except Exception as exc:
        if isinstance(exc, ExecutionApiError):
            raise
        raise ExecutionApiError(
            500,
            "credential_decryption_failed",
            "凭据无法解密，请重新配置",
        ) from exc
