"""Enterprise authentication, authorization, and compliance controls."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import g, request, session
from werkzeug.security import check_password_hash, generate_password_hash


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_utc_datetime(value) -> datetime | None:
    """Parse SQLite/ISO timestamps as aware UTC datetimes."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = f"{raw[:-1]}+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class EnterpriseSecurity:
    """Server-side identity, RBAC, plant scope, CSRF, and signed audit events."""

    ROLES = {
        "GROUP_ADMIN": {
            "label": "集团系统管理员",
            "permissions": {"*"},
        },
        "GROUP_APPROVER": {
            "label": "集团审批与分发负责人",
            "permissions": {
                "data.read", "report.read", "ai.use", "review.decide",
                "lifecycle.review", "lifecycle.approve",
                "distribution.execute", "audit.read", "audit.verify", "compliance.read",
            },
        },
        "PLANT_STEWARD": {
            "label": "工厂主数据管理员",
            "permissions": {
                "data.read", "report.read", "data.ingest", "ai.use", "review.decide",
                "lifecycle.create", "lifecycle.review", "feedback.write",
            },
        },
        "AUDITOR": {
            "label": "安全合规审计员",
            "permissions": {
                "data.read", "report.read", "audit.read", "audit.verify", "compliance.read",
            },
        },
    }

    DEFAULT_ACCOUNTS = (
        ("group_admin", "集团系统管理员", "GROUP_ADMIN", "GROUP", "MDM_PASSWORD_GROUP_ADMIN"),
        ("group_approver", "集团审批负责人", "GROUP_APPROVER", "GROUP", "MDM_PASSWORD_GROUP_APPROVER"),
        ("shanghai_steward", "上海工厂主数据管理员", "PLANT_STEWARD", "SHANGHAI", "MDM_PASSWORD_SHANGHAI_STEWARD"),
        ("compliance_auditor", "安全合规审计员", "AUDITOR", "GROUP", "MDM_PASSWORD_COMPLIANCE_AUDITOR"),
    )

    DATA_CLASSIFICATIONS = {
        "PUBLIC": {"rank": 0, "label": "公开", "external_ai": True, "default_retention_days": 365},
        "INTERNAL": {"rank": 1, "label": "内部", "external_ai": True, "default_retention_days": 730},
        "CONFIDENTIAL": {"rank": 2, "label": "机密", "external_ai": False, "default_retention_days": 1095},
        "RESTRICTED": {"rank": 3, "label": "受限", "external_ai": False, "default_retention_days": 1825},
    }

    def __init__(self, app, db_connect, db_path: Path, logger, plants: dict[str, str]):
        self.app = app
        self.db_connect = db_connect
        self.db_path = Path(db_path)
        self.logger = logger
        self.plants = plants
        mode = os.environ.get("MDM_SECURITY_MODE", "enterprise").strip().lower()
        self.mode = mode if mode in {"open", "basic", "enterprise"} else "enterprise"
        configured_security_dir = os.environ.get("MDM_SECURITY_DIR", "").strip()
        default_security_dir = self.db_path.parent
        if not configured_security_dir:
            repository_root = next(
                (parent for parent in (self.db_path.parent, *self.db_path.parents) if (parent / ".git").is_dir()),
                None,
            )
            if repository_root:
                # Keep generated credentials and signing keys out of source-controlled application folders.
                default_security_dir = repository_root / "runtime" / "security"
        self.security_dir = Path(configured_security_dir or default_security_dir).expanduser()
        if not self.security_dir.is_absolute():
            self.security_dir = self.db_path.parent / self.security_dir
        self.security_dir.mkdir(parents=True, exist_ok=True)
        self._restrict_permissions(self.security_dir, 0o700)
        self.initial_credentials_path = self.security_dir / "initial-credentials.txt"
        if self.initial_credentials_path.is_file():
            self._restrict_permissions(self.initial_credentials_path, 0o600)
        self.signing_key = self._load_or_create_secret("security-signing.key")
        configured_session_secret = os.environ.get("MDM_SESSION_SECRET", "").strip()
        if configured_session_secret and len(configured_session_secret.encode("utf-8")) < 32:
            if os.environ.get("MDM_PRODUCTION") == "1":
                raise RuntimeError("MDM_SESSION_SECRET must contain at least 32 bytes in production")
            self.logger.warning("MDM_SESSION_SECRET is shorter than 32 bytes; deriving a fixed-length key")
            configured_session_secret = hashlib.sha256(configured_session_secret.encode("utf-8")).hexdigest()
        session_secret = configured_session_secret or self._load_or_create_secret("session-signing.key").hex()
        app.secret_key = session_secret
        public_url = os.environ.get("MDM_PUBLIC_URL", "").strip().lower()
        secure_cookie_default = "1" if public_url.startswith("https://") else "0"
        secure_cookie = os.environ.get("MDM_COOKIE_SECURE", secure_cookie_default) == "1"
        if self.mode == "enterprise" and os.environ.get("MDM_PRODUCTION") == "1" and not secure_cookie:
            self.logger.warning(
                "enterprise session cookie is not Secure; set MDM_PUBLIC_URL=https://... or MDM_COOKIE_SECURE=1 behind TLS"
            )
        app.config.update(
            SESSION_COOKIE_NAME="mai_session",
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Strict",
            SESSION_COOKIE_SECURE=secure_cookie,
            SESSION_COOKIE_PATH="/",
            SESSION_REFRESH_EACH_REQUEST=True,
            PERMANENT_SESSION_LIFETIME=timedelta(
                minutes=max(15, int(os.environ.get("MDM_SESSION_MINUTES", "480")))
            ),
        )
        self.max_failed_logins = max(3, int(os.environ.get("MDM_MAX_FAILED_LOGINS", "5")))
        self.lock_minutes = max(1, int(os.environ.get("MDM_LOGIN_LOCK_MINUTES", "15")))
        self.audit_write_retries = max(1, int(os.environ.get("MDM_AUDIT_WRITE_RETRIES", "5")))
        self.trusted_origins = {
            item.strip().rstrip("/").lower()
            for item in os.environ.get("MDM_TRUSTED_ORIGINS", "").split(",") if item.strip()
        }
        self._dummy_password_hash = generate_password_hash(secrets.token_urlsafe(24), method="scrypt")

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        if os.name != "nt":
            return
        username = os.environ.get("USERNAME", "").strip()
        domain = os.environ.get("USERDOMAIN", "").strip()
        if not username:
            return
        account = f"{domain}\\{username}" if domain else username
        permission = "(OI)(CI)(F)" if path.is_dir() else "(F)"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        grants = (account, "*S-1-5-18", "*S-1-5-32-544")
        try:
            results = [
                subprocess.run(
                    ["icacls", str(path), "/grant:r", f"{principal}:{permission}"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                for principal in grants
            ]
            if all(result.returncode == 0 for result in results):
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
        except OSError:
            # ACL hardening is best effort on restricted Windows hosts; chmod remains in effect.
            pass

    def _load_or_create_secret(self, filename: str) -> bytes:
        path = self.security_dir / filename
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            value = secrets.token_bytes(32)
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
                handle.write(value.hex())
                handle.flush()
                os.fsync(handle.fileno())
            self._restrict_permissions(path, 0o600)
            return value
        if path.is_symlink():
            raise RuntimeError(f"security secret must not be a symbolic link: {path}")
        self._restrict_permissions(path, 0o600)
        raw_value = ""
        for attempt in range(20):
            raw_value = path.read_text(encoding="ascii").strip()
            if raw_value:
                break
            time.sleep(0.01 * (attempt + 1))
        if not raw_value:
            raise RuntimeError(f"security secret is empty: {path}")
        try:
            value = bytes.fromhex(raw_value)
        except ValueError:
            value = hashlib.sha256(raw_value.encode("utf-8")).digest()
        if len(value) < 32:
            value = hashlib.sha256(value).digest()
        return value

    def init_schema(self, conn) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS security_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                plant_code TEXT NOT NULL DEFAULT 'GROUP',
                active INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                session_version INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                last_login_at TEXT,
                password_changed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS security_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                height INTEGER UNIQUE NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                role TEXT,
                plant_code TEXT,
                resource TEXT,
                outcome TEXT NOT NULL,
                details_json TEXT NOT NULL,
                trace_id TEXT,
                client_ip TEXT,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_security_events_type ON security_events(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_security_users_role ON security_users(role, plant_code, active);
            """
        )
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(security_users)")}
        if "must_change_password" not in user_columns:
            conn.execute(
                "ALTER TABLE security_users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
            )
        batch_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'batches'"
        ).fetchone()
        if batch_table:
            batch_columns = {row["name"] for row in conn.execute("PRAGMA table_info(batches)")}
            if "data_classification" not in batch_columns:
                conn.execute(
                    "ALTER TABLE batches ADD COLUMN data_classification TEXT NOT NULL DEFAULT 'INTERNAL'"
                )
            if "legal_hold" not in batch_columns:
                conn.execute("ALTER TABLE batches ADD COLUMN legal_hold INTEGER NOT NULL DEFAULT 0")
        self._bootstrap_accounts(conn)

    def _bootstrap_accounts(self, conn) -> None:
        if self.mode != "enterprise":
            return
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        now = _utc_now()
        generated_credentials = []
        for username, display_name, role, plant_code, env_name in self.DEFAULT_ACCOUNTS:
            if conn.execute("SELECT 1 FROM security_users WHERE username = ?", (username,)).fetchone():
                continue
            password = os.environ.get(env_name, "").strip()
            generated = not password
            if generated:
                password = f"Mai!{secrets.token_urlsafe(12)}"
            elif len(password) < 12 or not re_password_complexity(password):
                raise RuntimeError(f"{env_name} must contain at least 12 characters, letters, digits, and symbols")
            try:
                conn.execute(
                    """INSERT INTO security_users
                       (username, display_name, password_hash, role, plant_code, active,
                        must_change_password, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (username, display_name, generate_password_hash(password, method="scrypt"), role, plant_code,
                     int(generated), now, now),
                )
            except sqlite3.IntegrityError:
                # Another process may have completed the same bootstrap while this process waited for the write lock.
                continue
            if generated:
                generated_credentials.append((username, display_name, role, plant_code, password))
        if generated_credentials:
            self._write_initial_credentials(generated_credentials)

    def _write_initial_credentials(self, credentials: list[tuple[str, str, str, str, str]]) -> None:
        lines = [
            "M-AI Master enterprise initial accounts",
            "Contains newly generated one-time passwords only. Change them immediately and delete this file.",
            "",
        ]
        for username, display_name, role, plant_code, password in credentials:
            lines.extend((
                f"username={username}", f"password={password}", f"display_name={display_name}",
                f"role={role}", f"plant_code={plant_code}", "",
            ))
        content = "\n".join(lines).rstrip() + "\n"
        temporary = self.initial_credentials_path.with_name(
            f".{self.initial_credentials_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.initial_credentials_path)
            self._restrict_permissions(self.initial_credentials_path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
        self.logger.warning(
            "enterprise accounts created; generated credentials stored in protected file: %s",
            self.initial_credentials_path,
        )

    def _row_to_principal(self, row) -> dict | None:
        try:
            active = bool(row) and bool(int(row["active"]))
        except (TypeError, ValueError):
            active = False
        if not active:
            return None
        role = str(row["role"] or "").upper()
        plant_code = str(row["plant_code"] or "").upper()
        if role not in self.ROLES or plant_code not in self.plants:
            self.logger.error("invalid security identity metadata for user_id=%s", row["id"])
            return None
        if role in {"GROUP_ADMIN", "GROUP_APPROVER"} and plant_code != "GROUP":
            self.logger.error("group role has non-group plant scope for user_id=%s", row["id"])
            return None
        if role == "PLANT_STEWARD" and plant_code == "GROUP":
            self.logger.error("plant steward has group scope for user_id=%s", row["id"])
            return None
        permissions = sorted(self.ROLES[role]["permissions"])
        row_keys = set(row.keys())
        try:
            session_version = int(row["session_version"])
        except (TypeError, ValueError):
            self.logger.error("invalid session version for user_id=%s", row["id"])
            return None
        return {
            "user_id": int(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "role": role,
            "role_label": self.ROLES[role]["label"],
            "plant_code": plant_code,
            "plant_name": self.plants.get(plant_code, plant_code),
            "permissions": permissions,
            "session_version": session_version,
            "must_change_password": bool(row["must_change_password"]) if "must_change_password" in row_keys else False,
        }

    def open_principal(self, username="open-system", display_name="开放演示管理员") -> dict:
        """Return the explicit full-access identity used by open/basic compatibility modes."""
        return {
            "user_id": 0,
            "username": str(username or "open-system"),
            "display_name": str(display_name or "开放演示管理员"),
            "role": "GROUP_ADMIN",
            "role_label": self.ROLES["GROUP_ADMIN"]["label"],
            "plant_code": "GROUP",
            "plant_name": self.plants.get("GROUP", "GROUP"),
            "permissions": ["*"],
            "session_version": 0,
            "must_change_password": False,
        }

    def resolve_principal(self) -> dict | None:
        if self.mode == "open":
            return self.open_principal()
        if self.mode == "basic":
            auth = request.authorization
            expected_user = os.environ.get("MDM_AUTH_USER", "admin").strip() or "admin"
            expected_password = os.environ.get("MDM_AUTH_PASSWORD", "")
            valid_user = bool(auth) and hmac.compare_digest(auth.username or "", expected_user)
            valid_password = bool(auth) and bool(expected_password) and hmac.compare_digest(
                auth.password or "", expected_password
            )
            if valid_user and valid_password:
                return self.open_principal(expected_user, "Basic兼容管理员")
            return None
        user_id = session.get("user_id")
        if not user_id:
            return None
        try:
            user_id = int(user_id)
            session_version = int(session.get("session_version", -1))
        except (TypeError, ValueError):
            session.clear()
            return None
        with self.db_connect() as conn:
            row = conn.execute("SELECT * FROM security_users WHERE id = ?", (user_id,)).fetchone()
        principal = self._row_to_principal(row)
        if not principal or principal["session_version"] != session_version:
            session.clear()
            return None
        return principal

    def has_permission(self, principal: dict | None, permission: str) -> bool:
        if not principal:
            return False
        allowed = set(principal.get("permissions") or ())
        return "*" in allowed or permission in allowed

    def public_roles(self) -> dict[str, dict]:
        """Expose labels and sorted permissions without returning mutable role definitions."""
        return {
            role: {"label": config["label"], "permissions": sorted(config["permissions"])}
            for role, config in self.ROLES.items()
        }

    def effective_plant(self, requested=None, default: str = "GROUP") -> str:
        raw = str(requested or "").strip().upper()
        requested_code = raw if raw in self.plants else default
        principal = getattr(g, "principal", None)
        if self.mode == "enterprise" and principal and principal.get("plant_code") != "GROUP":
            return principal["plant_code"]
        return requested_code

    def validate_requested_scope(self, principal: dict | None) -> tuple[bool, str]:
        if self.mode != "enterprise":
            return True, ""
        if not principal:
            return False, "authentication is required"
        if principal.get("plant_code") == "GROUP":
            return True, ""
        values = []
        for key in ("plant_code", "target_plant", "source_plant", "view_plant_code"):
            if request.args.get(key):
                values.append(request.args.get(key))
            if request.form.get(key):
                values.append(request.form.get(key))
        payload = request.get_json(silent=True) if request.is_json else None
        if isinstance(payload, dict):
            for key in ("plant_code", "target_plant", "source_plant", "view_plant_code"):
                if payload.get(key):
                    values.append(payload.get(key))
            target_plants = payload.get("target_plants")
            if isinstance(target_plants, str):
                values.append(target_plants)
            elif isinstance(target_plants, list):
                values.extend(target_plants)
        own = principal["plant_code"]
        invalid = [str(value).strip().upper() for value in values if str(value).strip().upper() != own]
        return (not invalid, "" if not invalid else f"identity is restricted to plant {own}")

    def csrf_valid(self) -> bool:
        if self.mode != "enterprise" or request.method in {"GET", "HEAD", "OPTIONS"}:
            return True
        expected = str(session.get("csrf_token") or "")
        supplied = str(request.headers.get("X-CSRF-Token") or "")
        if not (expected and supplied and hmac.compare_digest(expected, supplied)):
            return False
        if request.headers.get("Sec-Fetch-Site", "").lower() in {"cross-site", "none"}:
            return False
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if not source:
            return True
        try:
            parsed = urlsplit(source)
            source_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            current = urlsplit(request.host_url)
            current_origin = f"{current.scheme.lower()}://{current.netloc.lower()}"
        except (TypeError, ValueError):
            return False
        allowed = {current_origin, *self.trusted_origins}
        return source_origin.rstrip("/") in allowed

    def authenticate(self, username: str, password: str) -> tuple[dict | None, str]:
        if self.mode != "enterprise":
            return None, "interactive login is available only in enterprise mode"
        now = datetime.now(timezone.utc)
        with self.db_connect() as conn:
            row = conn.execute("SELECT * FROM security_users WHERE username = ?", (username,)).fetchone()
            try:
                active = bool(row) and bool(int(row["active"]))
            except (TypeError, ValueError):
                active = False
            if not active:
                check_password_hash(self._dummy_password_hash, password or "")
                return None, "invalid username or password"
            locked_until = row["locked_until"]
            parsed_lock = None
            if locked_until:
                try:
                    parsed_lock = _parse_utc_datetime(locked_until)
                    if parsed_lock and parsed_lock > now:
                        return None, "account is temporarily locked"
                except (TypeError, ValueError):
                    self.logger.warning("invalid locked_until value for user_id=%s; clearing it", row["id"])
                conn.execute(
                    "UPDATE security_users SET failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
                    (_utc_now(), row["id"]),
                )
            try:
                password_valid = check_password_hash(row["password_hash"], password or "")
            except (TypeError, ValueError):
                password_valid = False
            if not password_valid:
                attempts = (0 if locked_until else int(row["failed_attempts"] or 0)) + 1
                lock_value = None
                if attempts >= self.max_failed_logins:
                    lock_value = (now + timedelta(minutes=self.lock_minutes)).isoformat(timespec="seconds")
                    attempts = self.max_failed_logins
                conn.execute(
                    "UPDATE security_users SET failed_attempts = ?, locked_until = ?, updated_at = ? WHERE id = ?",
                    (attempts, lock_value, _utc_now(), row["id"]),
                )
                return None, "invalid username or password"
            conn.execute(
                """UPDATE security_users SET failed_attempts = 0, locked_until = NULL,
                   last_login_at = ?, updated_at = ? WHERE id = ?""",
                (_utc_now(), _utc_now(), row["id"]),
            )
            refreshed = conn.execute("SELECT * FROM security_users WHERE id = ?", (row["id"],)).fetchone()
        return self._row_to_principal(refreshed), ""

    def start_session(self, principal: dict) -> str:
        if self.mode != "enterprise":
            return ""
        session.clear()
        session.permanent = True
        session["user_id"] = principal["user_id"]
        session["session_version"] = principal["session_version"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        session["issued_at"] = _utc_now()
        return session["csrf_token"]

    def change_password(self, principal: dict, current_password: str, new_password: str) -> tuple[bool, str]:
        if self.mode != "enterprise" or not principal or not principal.get("user_id"):
            return False, "authenticated enterprise identity is required"
        if len(new_password) < 12 or not re_password_complexity(new_password):
            return False, "new password must be at least 12 characters and include letters, digits, and symbols"
        with self.db_connect() as conn:
            row = conn.execute("SELECT * FROM security_users WHERE id = ?", (principal["user_id"],)).fetchone()
            try:
                current_valid = bool(row) and check_password_hash(row["password_hash"], current_password)
            except (TypeError, ValueError):
                current_valid = False
            if not current_valid:
                return False, "current password is incorrect"
            if check_password_hash(row["password_hash"], new_password):
                return False, "new password must be different from the current password"
            conn.execute(
                """UPDATE security_users SET password_hash = ?, password_changed_at = ?,
                   session_version = session_version + 1, must_change_password = 0,
                   updated_at = ? WHERE id = ?""",
                (generate_password_hash(new_password, method="scrypt"), _utc_now(), _utc_now(), principal["user_id"]),
            )
            new_version = conn.execute(
                "SELECT session_version FROM security_users WHERE id = ?", (principal["user_id"],)
            ).fetchone()[0]
            generated_passwords_remaining = conn.execute(
                "SELECT COUNT(*) FROM security_users WHERE must_change_password = 1"
            ).fetchone()[0]
        session["session_version"] = int(new_version)
        session["csrf_token"] = secrets.token_urlsafe(32)
        if not generated_passwords_remaining and self.initial_credentials_path.is_file():
            try:
                self.initial_credentials_path.unlink()
            except OSError as exc:
                self.logger.warning("initial credentials file could not be removed: %s", exc)
        return True, ""

    def list_users(self) -> list[dict]:
        with self.db_connect() as conn:
            rows = conn.execute(
                """SELECT id, username, display_name, role, plant_code, active, failed_attempts,
                          locked_until, must_change_password, last_login_at, password_changed_at,
                          created_at, updated_at
                   FROM security_users ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_user(self, payload: dict, actor: dict) -> tuple[dict | None, str]:
        if not actor or actor.get("role") != "GROUP_ADMIN":
            return None, "group administrator permission is required"
        username = str(payload.get("username") or "").strip()
        display_name = str(payload.get("display_name") or username).strip()
        password = str(payload.get("password") or "")
        role = str(payload.get("role") or "").strip().upper()
        plant_code = str(payload.get("plant_code") or "GROUP").strip().upper()
        if not username or not re_username(username):
            return None, "username must contain 3-40 letters, digits, dots, underscores, or hyphens"
        if role not in self.ROLES:
            return None, "unsupported role"
        if plant_code not in self.plants:
            return None, "unsupported plant_code"
        if role == "PLANT_STEWARD" and plant_code == "GROUP":
            return None, "plant steward must belong to a factory"
        if role in {"GROUP_ADMIN", "GROUP_APPROVER"} and plant_code != "GROUP":
            return None, "group roles must use plant_code GROUP"
        if len(password) < 12 or not re_password_complexity(password):
            return None, "password must be at least 12 characters and include letters, digits, and symbols"
        now = _utc_now()
        try:
            with self.db_connect() as conn:
                cursor = conn.execute(
                    """INSERT INTO security_users
                       (username, display_name, password_hash, role, plant_code, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                    (username, display_name, generate_password_hash(password, method="scrypt"), role, plant_code, now, now),
                )
                user_id = cursor.lastrowid
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                return None, "username already exists"
            raise
        return {"id": user_id, "username": username, "display_name": display_name,
                "role": role, "plant_code": plant_code, "created_by": actor.get("username", "group_admin")}, ""

    def record_event(
        self, event_type: str, outcome: str, resource: str = "", details: dict | None = None,
        principal: dict | None = None, trace_id: str = "", client_ip: str = "",
    ) -> dict:
        principal = principal or {}
        safe_details = sanitize_audit_details(details or {})
        created_at = _utc_now()
        event_type = str(event_type or "UNKNOWN")[:100]
        outcome = str(outcome or "UNKNOWN")[:40]
        resource = str(resource or "")[:500]
        trace_id = str(trace_id or "")[:128]
        client_ip = str(client_ip or "")[:128]
        last_error = None
        for attempt in range(self.audit_write_retries):
            try:
                with self.db_connect() as conn:
                    # Serialize height allocation and insertion across Waitress/Gunicorn threads and processes.
                    conn.execute("BEGIN IMMEDIATE")
                    previous = conn.execute(
                        "SELECT height, event_hash FROM security_events ORDER BY height DESC LIMIT 1"
                    ).fetchone()
                    height = int(previous["height"]) + 1 if previous else 1
                    previous_hash = previous["event_hash"] if previous else "0" * 64
                    header = {
                        "height": height,
                        "event_type": event_type,
                        "actor": str(principal.get("username") or "anonymous")[:100],
                        "role": str(principal.get("role") or "")[:50],
                        "plant_code": str(principal.get("plant_code") or "")[:50],
                        "resource": resource,
                        "outcome": outcome,
                        "details": safe_details,
                        "trace_id": trace_id,
                        "client_ip": client_ip,
                        "previous_hash": previous_hash,
                        "created_at": created_at,
                    }
                    event_hash = hmac.new(
                        self.signing_key, _canonical_json(header).encode("utf-8"), hashlib.sha256
                    ).hexdigest()
                    conn.execute(
                        """INSERT INTO security_events
                           (height, event_type, actor, role, plant_code, resource, outcome, details_json,
                            trace_id, client_ip, previous_hash, event_hash, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (height, event_type, header["actor"], header["role"], header["plant_code"], resource,
                         outcome, _canonical_json(safe_details), trace_id, client_ip, previous_hash,
                         event_hash, created_at),
                    )
                return {**header, "event_hash": event_hash}
            except sqlite3.OperationalError as exc:
                last_error = exc
                message = str(exc).lower()
                if not any(marker in message for marker in ("locked", "busy")) or attempt + 1 >= self.audit_write_retries:
                    raise
                time.sleep(min(0.25, 0.02 * (2 ** attempt)) + secrets.randbelow(10) / 1000)
        raise last_error or RuntimeError("security audit event could not be written")

    def verify_events(self) -> dict:
        with self.db_connect() as conn:
            rows = conn.execute("SELECT * FROM security_events ORDER BY height").fetchall()
        expected_previous = "0" * 64
        errors = []
        for expected_height, row in enumerate(rows, 1):
            try:
                details = json.loads(row["details_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                details = {}
                errors.append({"height": row["height"], "error": "details_json is invalid"})
            header = {
                "height": int(row["height"]), "event_type": row["event_type"], "actor": row["actor"],
                "role": row["role"] or "", "plant_code": row["plant_code"] or "",
                "resource": row["resource"] or "", "outcome": row["outcome"], "details": details,
                "trace_id": row["trace_id"] or "", "client_ip": row["client_ip"] or "",
                "previous_hash": row["previous_hash"], "created_at": row["created_at"],
            }
            calculated = hmac.new(
                self.signing_key, _canonical_json(header).encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if int(row["height"]) != expected_height:
                errors.append({"height": row["height"], "error": "height is not continuous"})
            if row["previous_hash"] != expected_previous:
                errors.append({"height": row["height"], "error": "previous hash mismatch"})
            if not hmac.compare_digest(calculated, str(row["event_hash"] or "")):
                errors.append({"height": row["height"], "error": "HMAC signature mismatch"})
            expected_previous = str(row["event_hash"] or "")
        return {
            "valid": not errors, "event_count": len(rows), "latest_hash": expected_previous if rows else None,
            "signature": "HMAC-SHA256", "external_anchor": False, "errors": errors,
        }

    def compliance_summary(self) -> dict:
        with self.db_connect() as conn:
            users = conn.execute("SELECT COUNT(*) FROM security_users WHERE active = 1").fetchone()[0]
            classifications = {
                row["data_classification"]: int(row["count"])
                for row in conn.execute(
                    """SELECT data_classification, COUNT(*) AS count FROM batches
                       GROUP BY data_classification"""
                ).fetchall()
            }
            legal_holds = conn.execute("SELECT COUNT(*) FROM batches WHERE legal_hold = 1").fetchone()[0]
        return {
            "mode": self.mode,
            "authenticated_users": users,
            "password_hash": "scrypt",
            "session_cookie": {"http_only": True, "same_site": "Strict", "secure": self.app.config["SESSION_COOKIE_SECURE"]},
            "csrf": self.mode == "enterprise",
            "rbac": self.mode == "enterprise",
            "plant_scope_bound_to_identity": self.mode == "enterprise",
            "classifications": self.DATA_CLASSIFICATIONS,
            "batch_counts_by_classification": classifications,
            "active_legal_holds": legal_holds,
            "security_audit": self.verify_events(),
            "compliance_statement": "Control evidence only; formal certification and legal assessment remain deployment responsibilities.",
        }


def re_username(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", value))


def re_password_complexity(value: str) -> bool:
    return any(char.isalpha() for char in value) and any(char.isdigit() for char in value) and any(
        not char.isalnum() for char in value
    )


def sanitize_audit_details(value, _depth=0, _seen=None):
    sensitive_markers = (
        "password", "authorization", "api_key", "apikey", "secret", "token", "cookie", "credential",
        "private_key", "privatekey",
    )
    if _depth > 8:
        return "[MAX_DEPTH]"
    if _seen is None:
        _seen = set()
    if isinstance(value, (dict, list, tuple, set)):
        identity = id(value)
        if identity in _seen:
            return "[CYCLE]"
        _seen.add(identity)
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            safe_key = str(key)[:100]
            normalized_key = safe_key.lower().replace("-", "_")
            result[safe_key] = (
                "[REDACTED]" if any(marker in normalized_key for marker in sensitive_markers)
                else sanitize_audit_details(item, _depth + 1, _seen)
            )
        _seen.discard(id(value))
        return result
    if isinstance(value, (list, tuple, set)):
        result = [sanitize_audit_details(item, _depth + 1, _seen) for item in list(value)[:100]]
        _seen.discard(id(value))
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return f"[BINARY sha256={hashlib.sha256(value).hexdigest()} bytes={len(value)}]"
    text = str(value)
    return text[:500]
