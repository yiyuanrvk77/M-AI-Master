"""M-AI Master Flask backend.

Flask and SQLite are the authoritative runtime. The browser keeps an IndexedDB
cache only so the existing single-page UI can render and export data quickly.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import binascii
from difflib import SequenceMatcher
import io
import importlib.util
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote

import chardet
import networkx as nx
import numpy as np
import pandas as pd
import requests
from flask import Flask, g, has_request_context, jsonify, request, send_from_directory, session
from sklearn.ensemble import IsolationForest

from enterprise_rag import EnterpriseRAG, RAGConfig
from enterprise_governance import EnterpriseGovernance
from enterprise_security import EnterpriseSecurity, sanitize_audit_details


BASE_DIR = Path(__file__).resolve().parent


def _load_local_env(path: Path) -> None:
    """Load optional local secrets without adding a dotenv dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and value:
            os.environ.setdefault(key, value)


_load_local_env(BASE_DIR / ".env")
PROJECT_DIR = BASE_DIR.parent
OCR_RUNTIME_DIR = PROJECT_DIR / ".venv-ocr"
OCR_RUNTIME_PYTHON = OCR_RUNTIME_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
OCR_READY_MARKER = PROJECT_DIR / "runtime" / "ocr-ready"
OCR_WORKER_PATH = BASE_DIR / "ocr_worker.py"
OCR_INSTALL_LOG = PROJECT_DIR / "runtime" / "ocr-install.log"
OCR_INSTALL_LOCK = PROJECT_DIR / "runtime" / "ocr-installing.lock"


def _external_ocr_ready() -> bool:
    return OCR_RUNTIME_PYTHON.is_file() and OCR_READY_MARKER.is_file() and OCR_WORKER_PATH.is_file()


def _config_path(name: str, default: Path) -> Path:
    path = Path(os.environ.get(name, str(default))).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


FRONTEND_DIR = _config_path("MDM_FRONTEND_DIR", BASE_DIR)
FRONTEND_FILE = os.environ.get("MDM_FRONTEND_FILE", "index.html")
DB_PATH = _config_path("MDM_DB_PATH", BASE_DIR / "mdm_data.db")
MAX_RECORDS = int(os.environ.get("MDM_MAX_RECORDS", "10000"))
APP_VERSION = "5.1"
AUTH_USER = os.environ.get("MDM_AUTH_USER", "admin").strip() or "admin"
AUTH_PASSWORD = os.environ.get("MDM_AUTH_PASSWORD", "")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MDM_MAX_UPLOAD_BYTES", 20 * 1024 * 1024))
app.json.ensure_ascii = False
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mai-master")
if os.environ.get("MDM_LOG_DIR"):
    log_dir = _config_path("MDM_LOG_DIR", BASE_DIR / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "mai-master.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)


def _required_permission(path: str, method: str) -> str | None:
    """Map the existing API surface to server-enforced enterprise permissions."""
    method = method.upper()
    if path in {"/api/live", "/api/health", "/api/auth/login"} or not path.startswith("/api/"):
        return None
    if path.startswith("/api/auth/"):
        return "data.read"
    if path.startswith("/api/admin/users"):
        return "security.manage"
    if path.startswith("/api/admin/standards") or path.startswith("/api/knowledge"):
        return "knowledge.manage" if method != "GET" else "data.read"
    if path.startswith("/api/security/audit/verify"):
        return "audit.verify"
    if path.startswith("/api/security/audit") or path.startswith("/api/blockchain"):
        return "audit.read"
    if path.startswith("/api/compliance"):
        return "compliance.manage" if method != "GET" else "compliance.read"
    if path.startswith("/api/governance/controls"):
        return "compliance.manage" if method != "GET" else "data.read"
    if path.startswith("/api/governance/issues"):
        return "review.decide" if method != "GET" else "data.read"
    if path.startswith("/api/governance/catalog"):
        return "data.read"
    if method == "DELETE":
        return "data.purge"
    if path == "/api/vectors/rebuild" or (path == "/api/ocr/install" and method != "GET"):
        return "knowledge.manage"
    if path == "/api/distribute":
        return "distribution.execute"
    if path == "/api/feedback":
        return "feedback.write"
    if path.startswith("/api/reviews") and method != "GET":
        return "review.decide"
    if path == "/api/lifecycle" and method == "POST":
        return "lifecycle.create"
    if path.startswith("/api/lifecycle/") and method == "PATCH":
        return None  # Transition-specific permission is checked after reading current state.
    if path in {"/api/batches", "/api/upload", "/api/govern", "/api/governance", "/api/demo/run", "/api/ocr"}:
        return "data.ingest"
    if method != "GET" and any(path.startswith(prefix) for prefix in (
        "/api/semantic", "/api/classify", "/api/agent", "/api/intent", "/api/explain",
        "/api/search", "/api/standards/search", "/api/vectors/search",
    )):
        return "ai.use"
    return "data.read"


def _security_response(message: str, status: int, code: str):
    response = jsonify({"error": message, "code": code, "trace_id": getattr(g, "trace_id", "")})
    response.status_code = status
    return response


@app.before_request
def attach_trace_context():
    g.trace_id = request.headers.get("X-Trace-Id") or f"TRC-{uuid.uuid4().hex[:16].upper()}"
    g.request_started = time.perf_counter()
    g.security_event_recorded = False
    g.data_classification = "INTERNAL"
    g.external_ai_allowed = True
    security = globals().get("enterprise_security")
    if security is None:
        return None
    if security.mode == "open":
        if AUTH_PASSWORD and request.path not in {"/api/live", "/api/health"}:
            auth = request.authorization
            valid_user = bool(auth) and hmac.compare_digest(auth.username or "", AUTH_USER)
            valid_password = bool(auth) and hmac.compare_digest(auth.password or "", AUTH_PASSWORD)
            if not (valid_user and valid_password):
                response = _security_response("authentication required", 401, "AUTH_REQUIRED")
                response.headers["WWW-Authenticate"] = 'Basic realm="M-AI Master", charset="UTF-8"'
                return response
        g.principal = security.resolve_principal()
        return None
    if security.mode == "basic":
        if not AUTH_PASSWORD or request.path in {"/api/live", "/api/health"} or not request.path.startswith("/api/"):
            g.principal = security.open_principal()
            return None
        auth = request.authorization
        valid_user = bool(auth) and hmac.compare_digest(auth.username or "", AUTH_USER)
        valid_password = bool(auth) and hmac.compare_digest(auth.password or "", AUTH_PASSWORD)
        if not (valid_user and valid_password):
            response = _security_response("authentication required", 401, "AUTH_REQUIRED")
            response.headers["WWW-Authenticate"] = 'Basic realm="M-AI Master", charset="UTF-8"'
            return response
        g.principal = security.open_principal(username=AUTH_USER, display_name="Legacy administrator")
        return None

    g.principal = security.resolve_principal()
    public = request.path in {"/api/live", "/api/health", "/api/auth/login"} or not request.path.startswith("/api/")
    if public:
        return None
    if not g.principal:
        return _security_response("please sign in", 401, "AUTH_REQUIRED")
    scope_valid, scope_error = security.validate_requested_scope(g.principal)
    if not scope_valid:
        security.record_event(
            "AUTHORIZATION_DENIED", "DENIED", request.path,
            {"reason": scope_error, "method": request.method}, g.principal, g.trace_id, request.remote_addr or "",
        )
        g.security_event_recorded = True
        return _security_response(scope_error, 403, "PLANT_SCOPE_DENIED")
    payload = request.get_json(silent=True) if request.is_json else {}
    payload = payload if isinstance(payload, dict) else {}
    requested_classification = _clean_value(
        payload.get("data_classification") or request.form.get("data_classification")
        or request.args.get("data_classification")
    ).upper()
    batch_id = _clean_value(payload.get("batch_id") or request.form.get("batch_id") or request.args.get("batch_id"))
    if batch_id and not requested_classification:
        try:
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT data_classification FROM batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
            requested_classification = row["data_classification"] if row else ""
        except sqlite3.Error:
            requested_classification = ""
    if requested_classification:
        if requested_classification not in EnterpriseSecurity.DATA_CLASSIFICATIONS:
            return _security_response("unsupported data_classification", 400, "CLASSIFICATION_INVALID")
        g.data_classification = requested_classification
    g.external_ai_allowed = bool(
        EnterpriseSecurity.DATA_CLASSIFICATIONS[g.data_classification]["external_ai"]
    )
    g.request_data_owner = _clean_value(payload.get("data_owner") or request.form.get("data_owner")) or g.principal["display_name"]
    g.request_processing_purpose = _clean_value(
        payload.get("processing_purpose") or request.form.get("processing_purpose")
    ) or "物料主数据治理"
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.path != "/api/auth/login":
        if not security.csrf_valid():
            security.record_event(
                "CSRF_REJECTED", "DENIED", request.path, {"method": request.method},
                g.principal, g.trace_id, request.remote_addr or "",
            )
            g.security_event_recorded = True
            return _security_response("missing or invalid CSRF token", 403, "CSRF_INVALID")
    permission = _required_permission(request.path, request.method)
    if permission and not security.has_permission(g.principal, permission):
        security.record_event(
            "AUTHORIZATION_DENIED", "DENIED", request.path,
            {"permission": permission, "method": request.method}, g.principal, g.trace_id,
            request.remote_addr or "",
        )
        g.security_event_recorded = True
        return _security_response(f"permission required: {permission}", 403, "PERMISSION_DENIED")
    return None


@app.after_request
def attach_trace_headers(response):
    elapsed_ms = round((time.perf_counter() - getattr(g, "request_started", time.perf_counter())) * 1000, 2)
    response.headers["X-Trace-Id"] = getattr(g, "trace_id", "")
    response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=(), payment=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Content-Security-Policy-Report-Only"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    )
    if request.is_secure or os.environ.get("MDM_FORCE_HTTPS") == "1":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path == "/":
        response.headers["Cache-Control"] = "no-store"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        logger.info(
            "%s %s status=%s duration_ms=%s trace_id=%s remote=%s",
            request.method, request.path, response.status_code, elapsed_ms,
            getattr(g, "trace_id", ""), request.remote_addr or "-",
        )
    security = globals().get("enterprise_security")
    if (
        security is not None and security.mode == "enterprise"
        and request.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.path != "/api/auth/login" and not getattr(g, "security_event_recorded", False)
        and getattr(g, "principal", None)
    ):
        try:
            security.record_event(
                "API_MUTATION", "SUCCESS" if response.status_code < 400 else "FAILED", request.path,
                {"method": request.method, "status": response.status_code}, g.principal,
                getattr(g, "trace_id", ""), request.remote_addr or "",
            )
        except Exception:
            logger.exception("failed to append enterprise security audit event")
    return response


FIELD_ALIASES = {
    "material_code": ["物料编码", "物料代码", "物料号", "零件号", "备件编码", "编码", "material_code", "material code", "item_code", "item code", "part_number", "part number", "sku", "code"],
    "system_source": ["系统来源", "来源系统", "数据来源", "数据源", "源系统", "来源", "system_source", "source_system", "source system", "system name", "system"],
    "material_name": ["物料名称", "物料名", "备件名称", "零件名称", "品名", "名称", "material_name", "material name", "item_name", "item name", "part_name", "part name", "title", "name"],
    "description": ["物料描述", "物料说明", "规格描述", "规格参数", "技术描述", "描述", "说明", "material_description", "material description", "item_desc", "item description", "specification", "description", "desc", "spec"],
    "category": ["物料分类", "物料类别", "物资分类", "物资类别", "备件分类", "备件类别", "大类", "分类", "类别", "品类", "material_category", "material category", "item_category", "item category", "category", "class"],
    "unit": ["计量单位", "基本单位", "物料单位", "单位", "unit_of_measure", "measure_unit", "unit of measure", "uom", "unit"],
    "create_time": ["创建时间", "创建日期", "录入时间", "导入时间", "时间", "日期", "created_at", "create_time", "create time", "created", "timestamp", "date"],
    "plant_code": ["工厂编码", "工厂代码", "工厂", "所属工厂", "plant_code", "plant", "site_code", "factory_code"],
}

PLANTS = {
    "GROUP": "集团总部",
    "SHANGHAI": "上海工厂",
    "BEIJING": "北京工厂",
}
STANDARD_KB = [
    {"reference_id": "SYT-MDM-48-SEAL", "category": "机械密封", "code_prefix": "MDM-48", "title": "泵用机械密封适配分类", "keywords": "机械密封 机封 端面密封 mechanical seal face seal 波纹管 集装式"},
    {"reference_id": "SYT-MDM-51-BEARING", "category": "深沟球轴承", "code_prefix": "MDM-51", "title": "滚动轴承适配分类", "keywords": "轴承 滚动轴承 深沟球轴承 bearing SKF FAG NSK NTN"},
    {"reference_id": "SYT-MDM-53-GATE", "category": "闸阀", "code_prefix": "MDM-53", "title": "工业闸阀适配分类", "keywords": "闸阀 闸板阀 gate valve Z41 Z941 法兰连接"},
    {"reference_id": "SYT-MDM-51-ORING", "category": "O型圈", "code_prefix": "MDM-51", "title": "O型密封圈适配分类", "keywords": "O型圈 O-Ring 密封圈 FKM NBR 氟橡胶 丁腈橡胶"},
    {"reference_id": "SYT-MDM-52-FLANGE", "category": "法兰", "code_prefix": "MDM-52", "title": "管法兰适配分类", "keywords": "法兰 flange 对焊法兰 平焊法兰 盲法兰 WN PL BL RF"},
    {"reference_id": "SYT-MDM-51-BOLT", "category": "螺栓", "code_prefix": "MDM-51", "title": "紧固件适配分类", "keywords": "螺栓 螺柱 双头螺栓 bolt stud bolt 紧固件"},
    {"reference_id": "SYT-MDM-51-OIL", "category": "润滑油", "code_prefix": "MDM-51", "title": "工业润滑介质适配分类", "keywords": "润滑油 润滑脂 液压油 齿轮油 柴油机油 lubricant grease oil"},
    {"reference_id": "SYT-MDM-51-FILTER", "category": "滤芯", "code_prefix": "MDM-51", "title": "工业过滤元件适配分类", "keywords": "滤芯 过滤器 滤材 filter element 液压油滤芯 空气滤芯"},
    {"reference_id": "SYT-MDM-58-PT", "category": "压力变送器", "code_prefix": "MDM-58", "title": "压力测量仪表适配分类", "keywords": "压力变送器 transmitter pressure transmitter 3051 EJA STG 4-20mA"},
    {"reference_id": "SYT-MDM-58-MOTOR", "category": "防爆电机", "code_prefix": "MDM-58", "title": "防爆驱动设备适配分类", "keywords": "防爆电机 隔爆电机 explosion-proof motor ex motor YB3 Exd"},
]
PLANT_ALIASES = {
    "集团": "GROUP", "集团总部": "GROUP", "总部": "GROUP", "group": "GROUP", "hq": "GROUP",
    "上海": "SHANGHAI", "上海厂": "SHANGHAI", "上海工厂": "SHANGHAI", "shanghai": "SHANGHAI", "sh": "SHANGHAI",
    "北京": "BEIJING", "北京厂": "BEIJING", "北京工厂": "BEIJING", "beijing": "BEIJING", "bj": "BEIJING",
}

SOURCE_CONNECTORS = {
    "SAP": {"id": "sap-odata", "label": "SAP ERP OData", "table": "MARA", "env": "MDM_SAP_SOURCE_URL"},
    "ERP": {"id": "erp-rest", "label": "ERP 主数据接口", "table": "MATERIAL_MASTER", "env": "MDM_ERP_SOURCE_URL"},
    "EAM": {"id": "eam-rest", "label": "EAM 设备物资接口", "table": "EAM_MATERIAL", "env": "MDM_EAM_SOURCE_URL"},
    "MES": {"id": "mes-rest", "label": "MES 物料接口", "table": "MES_MATERIAL", "env": "MDM_MES_SOURCE_URL"},
    "WMS": {"id": "wms-rest", "label": "WMS 库存物料接口", "table": "WMS_ITEM", "env": "MDM_WMS_SOURCE_URL"},
    "采购": {"id": "procurement-rest", "label": "采购平台物料接口", "table": "PURCHASE_ITEM", "env": "MDM_PROCUREMENT_SOURCE_URL"},
    "CSV": {"id": "file-import", "label": "文件接入适配器", "table": "CSV_ROW", "env": ""},
}

PROVENANCE_ALIASES = {
    "source_record_id": ("源记录id", "源记录编号", "原记录id", "业务主键", "source_record_id", "record_id", "object_id"),
    "source_table": ("源表", "来源表", "业务对象", "source_table", "table_name", "object_type"),
    "source_url": ("源记录链接", "来源链接", "source_url", "record_url", "deep_link"),
}

DISTRIBUTION_SYSTEM_ALIASES = {
    "SAP": ("sap", "sap系统"),
    "ERP": ("erp", "erp系统"),
    "EAM": ("eam", "eam系统", "设备系统", "资产系统"),
    "MES": ("mes", "mes系统", "制造系统", "生产系统"),
    "WMS": ("wms", "wms系统", "仓储系统", "库存系统", "仓库系统"),
    "PROCUREMENT": ("采购平台", "采购系统", "采购"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_value(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    return str(value).strip()


def _normalize_header(value) -> str:
    return re.sub(r"[\s_\-./\\()（）\[\]【】:：]+", "", _clean_value(value).lower())


def _normalize_plant_code(value, default: str = "GROUP") -> str:
    raw = _clean_value(value)
    if not raw:
        return default
    alias = PLANT_ALIASES.get(raw.lower())
    if alias:
        code = alias
    else:
        code = re.sub(r"[^A-Z0-9_-]", "", raw.upper()) or default
    return code


def _effective_plant_code(value=None, default: str = "GROUP") -> str:
    """Resolve a request plant without allowing a session identity to widen its scope."""
    code = _normalize_plant_code(value, default)
    security = globals().get("enterprise_security")
    if has_request_context() and security is not None and security.mode == "enterprise":
        principal = getattr(g, "principal", None)
        if principal and principal.get("plant_code") != "GROUP":
            return principal["plant_code"]
    return code


def _current_actor(fallback: str = "system") -> str:
    security = globals().get("enterprise_security")
    principal = getattr(g, "principal", None) if has_request_context() else None
    if security is not None and security.mode == "enterprise" and principal:
        return f"{principal['display_name']} ({principal['username']})"
    return _clean_value(fallback) or "system"


def _connector_profile(system_source: str) -> dict:
    source = _clean_value(system_source)
    upper = source.upper()
    key = next((name for name in ("SAP", "ERP", "EAM", "MES", "WMS") if name in upper), None)
    if not key and "采购" in source:
        key = "采购"
    if not key:
        key = "CSV"
    profile = dict(SOURCE_CONNECTORS[key])
    profile["system"] = source or "CSV导入"
    profile["base_url"] = os.environ.get(profile.get("env") or "", "").strip() if profile.get("env") else ""
    return profile


def _extra_value(extra: dict, aliases: tuple[str, ...]) -> str:
    normalized = {_normalize_header(key): _clean_value(value) for key, value in (extra or {}).items()}
    for alias in aliases:
        value = normalized.get(_normalize_header(alias))
        if value:
            return value
    return ""


def _record_quality_issues(record: dict, attributes: dict | None = None) -> list[dict]:
    attributes = attributes or {}
    issues = []
    required = (
        ("material_code", "物料编码", "HIGH"), ("material_name", "物料名称", "HIGH"),
        ("description", "物料描述", "MEDIUM"), ("category", "物料分类", "MEDIUM"),
        ("unit", "计量单位", "MEDIUM"), ("system_source", "来源系统", "HIGH"),
    )
    for field, label, severity in required:
        if not _clean_value(record.get(field)):
            issues.append({"code": f"MISSING_{field.upper()}", "field": field, "label": label,
                           "severity": severity, "message": f"{label}缺失"})
    category = _clean_value(record.get("category") or attributes.get("category"))
    if not category or category == "其他":
        issues.append({"code": "CATEGORY_UNRESOLVED", "field": "category", "label": "物料分类",
                       "severity": "MEDIUM", "message": "未匹配到明确标准品类"})
    return issues


def _build_source_provenance(
    record: dict, attributes: dict, batch_id: str, filename: str, row_number: int
) -> tuple[dict, list[dict]]:
    extra = record.get("_extra") if isinstance(record.get("_extra"), dict) else {}
    profile = _connector_profile(record.get("system_source", ""))
    source_record_id = _extra_value(extra, PROVENANCE_ALIASES["source_record_id"]) or _clean_value(record.get("material_code")) or f"ROW-{row_number:06d}"
    source_table = _extra_value(extra, PROVENANCE_ALIASES["source_table"]) or profile["table"]
    explicit_url = _extra_value(extra, PROVENANCE_ALIASES["source_url"])
    if explicit_url and not re.match(r"^https?://", explicit_url, flags=re.IGNORECASE):
        explicit_url = ""
    if explicit_url:
        source_url = explicit_url
        connector_status = "CONFIGURED"
    elif profile["base_url"]:
        source_url = f"{profile['base_url'].rstrip('/')}/{quote(source_table, safe='')}/{quote(source_record_id, safe='')}"
        connector_status = "CONFIGURED"
    else:
        source_url = f"demo://{profile['id']}/{quote(source_table, safe='')}/{quote(source_record_id, safe='')}"
        connector_status = "DEMO"
    snapshot = {
        key: record.get(key, "") for key in (
            "material_code", "system_source", "material_name", "description", "category", "unit",
            "create_time", "plant_code",
        )
    }
    snapshot["extra"] = extra
    record_hash = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    issues = _record_quality_issues(record, attributes)
    provenance = {
        "batch_id": batch_id, "source_system": record.get("system_source") or "CSV导入",
        "plant_code": _normalize_plant_code(record.get("plant_code")), "source_table": source_table,
        "source_record_id": source_record_id, "connector_id": profile["id"],
        "connector_name": profile["label"], "connector_status": connector_status,
        "source_url": source_url, "filename": filename, "row_number": row_number,
        "record_hash": record_hash, "imported_at": _utc_now(),
    }
    return provenance, issues


def map_fields(headers: list[str]) -> dict[str, str]:
    normalized_headers = [(_normalize_header(header), header, index) for index, header in enumerate(headers)]
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for field, aliases in FIELD_ALIASES.items():
        candidates = []
        for alias_index, alias in enumerate(aliases):
            normalized_alias = _normalize_header(alias)
            if not normalized_alias:
                continue
            for normalized_header, header, header_index in normalized_headers:
                if header in used:
                    continue
                if normalized_header == normalized_alias:
                    score = 1000 + len(normalized_alias)
                elif len(normalized_alias) >= (2 if re.search(r"[\u4e00-\u9fff]", normalized_alias) else 4) and (
                    normalized_header.startswith(normalized_alias) or normalized_header.endswith(normalized_alias)
                ):
                    score = 500 + len(normalized_alias)
                else:
                    continue
                candidates.append((-score, alias_index, header_index, header))
        if candidates:
            match = min(candidates)[3]
            mapping[field] = match
            used.add(match)
    return mapping


def normalize_records(
    raw_records: list[dict], explicit_mapping: dict | None = None, default_plant_code: str = "GROUP"
) -> list[dict]:
    if not isinstance(raw_records, list):
        raise ValueError("records must be an array")
    if len(raw_records) > MAX_RECORDS:
        raise ValueError(f"one batch may contain at most {MAX_RECORDS} records")
    if not raw_records:
        raise ValueError("no records provided")

    headers = list(raw_records[0].keys()) if isinstance(raw_records[0], dict) else []
    mapping = explicit_mapping or map_fields(headers)
    valid_headers = set(headers)
    clean_mapping, used_sources = {}, set()
    for field in FIELD_ALIASES:
        source = mapping.get(field)
        if source in valid_headers and source not in used_sources:
            clean_mapping[field] = source
            used_sources.add(source)
    mapping = clean_mapping
    standard_input = all(any(key == field for key in headers) for field in ("material_code", "material_name", "description"))
    imported_at = _utc_now()
    records = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        record = {}
        for field in FIELD_ALIASES:
            source = field if standard_input and field in raw else mapping.get(field)
            record[field] = _clean_value(raw.get(source, "")) if source else ""
        supplied_extra = raw.get("_extra") if isinstance(raw.get("_extra"), dict) else {}
        extra = {str(key): _clean_value(value) for key, value in supplied_extra.items() if _clean_value(value)}
        for key, value in raw.items():
            if key not in used_sources and key not in FIELD_ALIASES and not str(key).startswith("_"):
                cleaned = _clean_value(value)
                if cleaned:
                    extra[str(key)] = cleaned
        record["_extra"] = extra
        record["system_source"] = record["system_source"] or "CSV导入"
        record["create_time"] = record["create_time"] or imported_at
        record["plant_code"] = _effective_plant_code(record.get("plant_code"), default_plant_code)
        if not (record["material_code"] or record["material_name"] or record["description"]):
            continue
        records.append(record)
    if not records:
        raise ValueError("no valid material records after field mapping")
    return records


@contextmanager
def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_legacy_batches(conn: sqlite3.Connection) -> None:
    """Recover batch links from the unambiguous append-only layout used by v1/v2."""
    mapping_count = conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0]
    assigned_mapping_count = conn.execute("SELECT COUNT(*) FROM mappings WHERE batch_id IS NOT NULL").fetchone()[0]
    legacy_batches = conn.execute("SELECT batch_id, record_count FROM batches ORDER BY id").fetchall()
    if mapping_count and not assigned_mapping_count and sum(row["record_count"] for row in legacy_batches) == mapping_count:
        offset = 0
        for batch in legacy_batches:
            mapping_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM mappings ORDER BY id LIMIT ? OFFSET ?", (batch["record_count"], offset)
            )]
            conn.executemany(
                "UPDATE mappings SET batch_id = ? WHERE id = ?",
                [(batch["batch_id"], mapping_id) for mapping_id in mapping_ids],
            )
            offset += batch["record_count"]

    # Build immutable snapshots for legacy batches without re-running governance.
    for batch in legacy_batches:
        batch_id = batch["batch_id"]
        if conn.execute("SELECT 1 FROM batch_masters WHERE batch_id = ? LIMIT 1", (batch_id,)).fetchone():
            continue
        legacy_mappings = conn.execute(
            "SELECT * FROM mappings WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        grouped_mappings: dict[str, list[sqlite3.Row]] = {}
        for mapping in legacy_mappings:
            if mapping["mdm_code"]:
                grouped_mappings.setdefault(mapping["mdm_code"], []).append(mapping)
        for mdm_code, group in grouped_mappings.items():
            master = conn.execute("SELECT * FROM masters WHERE mdm_code = ?", (mdm_code,)).fetchone()
            if not master:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO batch_masters
                   (batch_id, mdm_code, standard_name, category, model, brand, dn, pressure, material,
                    source_count, source_systems, decision, confidence, code_prefix, anomaly_count, source_records)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id, mdm_code, group[0]["standard_name"] or master["standard_name"], master["category"],
                    master["model"], master["brand"], master["dn"], master["pressure"], master["material"],
                    len(group), ",".join(sorted({item["system_source"] for item in group if item["system_source"]})),
                    group[0]["decision"] or master["decision"],
                    round(sum(float(item["similarity"] or 0) for item in group) / len(group), 4),
                    master["code_prefix"], master["anomaly_count"],
                    ";".join(item["original_code"] for item in group if item["original_code"]),
                ),
            )

        if not conn.execute("SELECT 1 FROM quality_reports WHERE batch_id = ?", (batch_id,)).fetchone():
            legacy_records = [dict(row) for row in conn.execute(
                """SELECT material_code, system_source, material_name, description, category, unit, create_time
                   FROM records WHERE batch_id = ? ORDER BY id""",
                (batch_id,),
            )]
            if legacy_records:
                report = analyze_quality(legacy_records)
                conn.execute(
                    "INSERT INTO quality_reports (batch_id, report, generated_at) VALUES (?, ?, ?)",
                    (batch_id, json.dumps(report, ensure_ascii=False), _utc_now()),
                )


def _backfill_mapping_record_ids(conn: sqlite3.Connection) -> None:
    """Link legacy mapping rows to source rows without guessing across batches."""
    used = {row["record_id"] for row in conn.execute(
        "SELECT record_id FROM mappings WHERE record_id IS NOT NULL"
    ).fetchall()}
    pending = conn.execute(
        "SELECT * FROM mappings WHERE record_id IS NULL AND batch_id IS NOT NULL ORDER BY id"
    ).fetchall()
    for mapping in pending:
        candidates = conn.execute(
            """SELECT id FROM records WHERE batch_id = ? AND system_source = ? AND material_code = ?
               AND plant_code = ? AND material_name = ? ORDER BY id""",
            (mapping["batch_id"], mapping["system_source"], mapping["original_code"],
             mapping["plant_code"], mapping["original_name"]),
        ).fetchall()
        record_id = next((row["id"] for row in candidates if row["id"] not in used), None)
        if record_id is not None:
            conn.execute("UPDATE mappings SET record_id = ? WHERE id = ?", (record_id, mapping["id"]))
            used.add(record_id)


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL,
                encoding TEXT,
                record_count INTEGER NOT NULL DEFAULT 0,
                plant_code TEXT NOT NULL DEFAULT 'GROUP',
                semantic_method TEXT,
                semantic_model TEXT,
                semantic_dimension INTEGER,
                semantic_warning TEXT,
                data_classification TEXT NOT NULL DEFAULT 'INTERNAL',
                data_owner TEXT,
                processing_purpose TEXT,
                retention_until TEXT,
                legal_hold INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                material_code TEXT,
                system_source TEXT,
                material_name TEXT,
                description TEXT,
                category TEXT,
                unit TEXT,
                create_time TEXT,
                plant_code TEXT NOT NULL DEFAULT 'GROUP',
                ext TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS masters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mdm_code TEXT UNIQUE NOT NULL,
                standard_name TEXT,
                category TEXT,
                model TEXT,
                brand TEXT,
                dn TEXT,
                pressure TEXT,
                material TEXT,
                source_count INTEGER NOT NULL DEFAULT 0,
                source_systems TEXT,
                decision TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                code_prefix TEXT,
                anomaly_count INTEGER NOT NULL DEFAULT 0,
                plant_codes TEXT NOT NULL DEFAULT 'GROUP',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS batch_masters (
                batch_id TEXT NOT NULL,
                mdm_code TEXT NOT NULL,
                standard_name TEXT,
                category TEXT,
                model TEXT,
                brand TEXT,
                dn TEXT,
                pressure TEXT,
                material TEXT,
                source_count INTEGER NOT NULL DEFAULT 0,
                source_systems TEXT,
                decision TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                code_prefix TEXT,
                anomaly_count INTEGER NOT NULL DEFAULT 0,
                source_records TEXT,
                plant_codes TEXT NOT NULL DEFAULT 'GROUP',
                PRIMARY KEY (batch_id, mdm_code),
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                record_id INTEGER,
                system_source TEXT,
                original_code TEXT,
                original_name TEXT,
                mdm_code TEXT,
                standard_name TEXT,
                decision TEXT,
                similarity REAL NOT NULL DEFAULT 0,
                applied_rules TEXT,
                plant_code TEXT NOT NULL DEFAULT 'GROUP',
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE,
                FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE SET NULL,
                FOREIGN KEY (mdm_code) REFERENCES masters(mdm_code)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                mdm_code TEXT,
                standard_name TEXT,
                decision TEXT,
                reason TEXT,
                applied_rules TEXT,
                source_records TEXT,
                source_systems TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                category TEXT,
                attributes TEXT,
                candidates TEXT,
                plant_codes TEXT NOT NULL DEFAULT 'GROUP',
                status TEXT NOT NULL DEFAULT 'REVIEW',
                approved_action TEXT,
                approved_at TEXT,
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE,
                FOREIGN KEY (mdm_code) REFERENCES masters(mdm_code)
            );
            CREATE TABLE IF NOT EXISTS quality_reports (
                batch_id TEXT PRIMARY KEY,
                report TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                query TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS lifecycle (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                request_id TEXT,
                name TEXT,
                mdm_code TEXT,
                category TEXT,
                brand TEXT,
                model TEXT,
                description TEXT,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                creator TEXT,
                reviewer TEXT,
                reviewed_at TEXT,
                change_of TEXT,
                plant_code TEXT NOT NULL DEFAULT 'GROUP',
                archived_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS distribution_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                target_system TEXT,
                mdm_code TEXT,
                standard_name TEXT,
                sync_mode TEXT,
                sync_frequency TEXT,
                status TEXT,
                message TEXT,
                plant_code TEXT NOT NULL DEFAULT 'GROUP',
                instruction TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS vector_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL DEFAULT 'golden_master',
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                batch_id TEXT,
                plant_code TEXT NOT NULL DEFAULT 'GROUP',
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                vector BLOB NOT NULL,
                dimension INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                metadata TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(namespace, batch_id, entity_type, entity_id, provider, model),
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS workflow_steps (
                batch_id TEXT NOT NULL,
                step_code TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                metrics TEXT,
                action_endpoint TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (batch_id, step_code),
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS plant_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                plant_code TEXT NOT NULL,
                mdm_code TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 1,
                rating INTEGER NOT NULL DEFAULT 5,
                comment TEXT,
                actor TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS audit_blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                height INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                actor TEXT,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                merkle_root TEXT NOT NULL,
                block_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(batch_id, height),
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS retention_policies (
                data_classification TEXT PRIMARY KEY,
                retention_days INTEGER NOT NULL,
                external_ai_allowed INTEGER NOT NULL DEFAULT 0,
                description TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS legal_holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                released_by TEXT,
                released_at TEXT
            );
            """
        )

        # Non-destructive migration for databases shipped with earlier builds.
        for table, columns in {
            "batches": {
                "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'", "semantic_method": "TEXT",
                "semantic_model": "TEXT", "semantic_dimension": "INTEGER", "semantic_warning": "TEXT",
                "data_classification": "TEXT NOT NULL DEFAULT 'INTERNAL'", "data_owner": "TEXT",
                "processing_purpose": "TEXT", "retention_until": "TEXT",
                "legal_hold": "INTEGER NOT NULL DEFAULT 0",
            },
            "records": {"create_time": "TEXT", "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'"},
            "masters": {
                "anomaly_count": "INTEGER NOT NULL DEFAULT 0", "updated_at": "TEXT",
                "plant_codes": "TEXT NOT NULL DEFAULT 'GROUP'",
            },
            "batch_masters": {"plant_codes": "TEXT NOT NULL DEFAULT 'GROUP'"},
            "mappings": {"batch_id": "TEXT", "record_id": "INTEGER", "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'"},
            "reviews": {
                "batch_id": "TEXT", "source_systems": "TEXT", "category": "TEXT",
                "attributes": "TEXT", "candidates": "TEXT", "approved_action": "TEXT", "approved_at": "TEXT",
                "plant_codes": "TEXT NOT NULL DEFAULT 'GROUP'",
            },
            "search_history": {"batch_id": "TEXT"},
            "lifecycle": {
                "batch_id": "TEXT", "request_id": "TEXT", "brand": "TEXT", "model": "TEXT",
                "description": "TEXT", "creator": "TEXT", "reviewer": "TEXT", "reviewed_at": "TEXT",
                "change_of": "TEXT", "archived_at": "TEXT", "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'",
            },
            "distribution_logs": {
                "batch_id": "TEXT", "standard_name": "TEXT", "sync_mode": "TEXT", "sync_frequency": "TEXT",
                "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'", "instruction": "TEXT", "payload_json": "TEXT",
            },
        }.items():
            for column, definition in columns.items():
                _ensure_column(conn, table, column, definition)

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_records_batch ON records(batch_id);
            CREATE INDEX IF NOT EXISTS idx_batch_masters_batch ON batch_masters(batch_id);
            CREATE INDEX IF NOT EXISTS idx_mappings_batch ON mappings(batch_id);
            CREATE INDEX IF NOT EXISTS idx_mappings_master ON mappings(mdm_code);
            CREATE INDEX IF NOT EXISTS idx_mappings_record ON mappings(record_id);
            CREATE INDEX IF NOT EXISTS idx_reviews_batch_status ON reviews(batch_id, status);
            CREATE INDEX IF NOT EXISTS idx_distribution_batch ON distribution_logs(batch_id);
            CREATE INDEX IF NOT EXISTS idx_records_plant ON records(batch_id, plant_code);
            CREATE INDEX IF NOT EXISTS idx_mappings_plant ON mappings(batch_id, plant_code);
            CREATE INDEX IF NOT EXISTS idx_lifecycle_plant ON lifecycle(batch_id, plant_code);
            CREATE INDEX IF NOT EXISTS idx_distribution_plant ON distribution_logs(batch_id, plant_code);
            CREATE INDEX IF NOT EXISTS idx_vectors_batch ON vector_embeddings(batch_id, namespace, provider, model);
            CREATE INDEX IF NOT EXISTS idx_vectors_entity ON vector_embeddings(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS idx_feedback_batch ON plant_feedback(batch_id, plant_code);
            CREATE INDEX IF NOT EXISTS idx_audit_batch_height ON audit_blocks(batch_id, height);
            CREATE INDEX IF NOT EXISTS idx_batches_retention ON batches(legal_hold, retention_until);
            CREATE INDEX IF NOT EXISTS idx_legal_holds_batch ON legal_holds(batch_id, active);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vectors_global_unique
                ON vector_embeddings(namespace, IFNULL(batch_id, ''), entity_type, entity_id, provider, model);
            """
        )
        now = _utc_now()
        for level, policy in EnterpriseSecurity.DATA_CLASSIFICATIONS.items():
            conn.execute(
                """INSERT INTO retention_policies
                   (data_classification, retention_days, external_ai_allowed, description, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(data_classification) DO NOTHING""",
                (level, policy["default_retention_days"], int(policy["external_ai"]),
                 f"Default {level.lower()} data lifecycle policy", now),
            )
        enterprise_security.init_schema(conn)
        enterprise_governance.init_schema(conn)
        _migrate_legacy_batches(conn)
        conn.execute("UPDATE lifecycle SET status = UPPER(status) WHERE status IS NOT NULL")
        for table in ("batches", "records", "mappings", "lifecycle", "distribution_logs"):
            conn.execute(f"UPDATE {table} SET plant_code = 'GROUP' WHERE plant_code IS NULL OR plant_code = ''")
        for table in ("masters", "batch_masters", "reviews"):
            conn.execute(f"UPDATE {table} SET plant_codes = 'GROUP' WHERE plant_codes IS NULL OR plant_codes = ''")
        _backfill_mapping_record_ids(conn)

        # Product upgrades must make historical batches usable without forcing a CSV re-upload.
        for batch in conn.execute("SELECT batch_id, record_count, filename FROM batches ORDER BY id").fetchall():
            batch_id = batch["batch_id"]
            issue_meta = enterprise_governance.capture_batch_issues(conn, batch_id)
            quality_row = conn.execute("SELECT report FROM quality_reports WHERE batch_id = ?", (batch_id,)).fetchone()
            if quality_row:
                report = json.loads(quality_row["report"] or "{}")
                report["issue_workflow"] = issue_meta
                report.setdefault("measurement", {
                    "framework": "DAMA-DMBOK2 Rev 适配质量度量",
                    "sample_size": int(batch["record_count"] or 0),
                    "accuracy_is_proxy": True,
                    "accuracy_note": "accuracy 为结构异常代理指标，不等同于人工真值集准确率。",
                })
                conn.execute(
                    "UPDATE quality_reports SET report = ? WHERE batch_id = ?",
                    (json.dumps(report, ensure_ascii=False), batch_id),
                )
            if not conn.execute("SELECT 1 FROM workflow_steps WHERE batch_id = ? LIMIT 1", (batch_id,)).fetchone():
                _initialize_workflow(conn, batch_id)
                master_count = conn.execute("SELECT COUNT(*) FROM batch_masters WHERE batch_id = ?", (batch_id,)).fetchone()[0]
                pending_reviews = conn.execute(
                    "SELECT COUNT(*) FROM reviews WHERE batch_id = ? AND status = 'REVIEW'", (batch_id,)
                ).fetchone()[0]
                distribution_count = conn.execute(
                    "SELECT COUNT(*) FROM distribution_logs WHERE batch_id = ? AND status = 'SUCCESS'", (batch_id,)
                ).fetchone()[0]
                feedback_count = conn.execute("SELECT COUNT(*) FROM plant_feedback WHERE batch_id = ?", (batch_id,)).fetchone()[0]
                for code in ("INGEST", "STANDARDIZE", "GRAPH_DEDUPE", "QUALITY"):
                    _set_workflow_step(conn, batch_id, code, "COMPLETED", 100, {"migrated": True})
                _set_workflow_step(conn, batch_id, "REVIEW", "ACTION_REQUIRED" if pending_reviews else "COMPLETED",
                                   0 if pending_reviews else 100, {"pending": pending_reviews, "migrated": True})
                _set_workflow_step(conn, batch_id, "DISTRIBUTE", "COMPLETED" if distribution_count else "READY",
                                   100 if distribution_count else 0, {"success_count": distribution_count, "migrated": True})
                _set_workflow_step(conn, batch_id, "FEEDBACK", "COMPLETED" if feedback_count else ("ACTION_REQUIRED" if distribution_count else "WAITING"),
                                   100 if feedback_count else 0, {"feedback_count": feedback_count, "migrated": True})
                logger.info("backfilled workflow for historical batch %s", batch_id)
            if not conn.execute("SELECT 1 FROM vector_embeddings WHERE batch_id = ? LIMIT 1", (batch_id,)).fetchone():
                vector_meta = _index_batch_vectors(conn, batch_id, "local")
                _set_workflow_step(conn, batch_id, "VECTOR_INDEX", "COMPLETED", 100, {**vector_meta, "migrated": True})
                logger.info("backfilled vector index for historical batch %s", batch_id)
            if not conn.execute("SELECT 1 FROM audit_blocks WHERE batch_id = ? LIMIT 1", (batch_id,)).fetchone():
                _append_audit_block(conn, batch_id, "LEGACY_BATCH_MIGRATED", "BATCH", batch_id, {
                    "filename": batch["filename"], "record_count": batch["record_count"],
                    "message": "历史批次已升级到M-AI Master 4.0产品化运行模型",
                }, actor="升级迁移器")
        _seed_standard_kb(conn)


class AIGovernanceEngine:
    SYNONYMS = {
        "机械密封": ["机封", "端面密封", "密封", "mechanical seal", "face seal", "seal"],
        "轴承": ["bearing", "滚动轴承", "ball bearing", "roller bearing"],
        "闸阀": ["阀门", "gate valve", "闸伐", "闸板阀"],
        "O型圈": ["o-ring", "o ring", "密封圈", "o型密封圈", "o圈"],
        "法兰": ["flange", "法兰盖", "盲板", "盲法兰", "盲板法兰"],
        "螺栓": ["螺柱", "stud bolt", "hex bolt", "bolt", "双头螺栓", "六角螺栓", "紧固件"],
        "润滑油": ["润滑脂", "液压油", "齿轮油", "柴油机油", "lubricant", "lubricating oil", "grease", "oil"],
        "滤芯": ["滤材", "过滤器", "filter", "filter element", "滤清器"],
        "压力变送器": ["变送器", "transmitter", "pressure transmitter", "差压变送器"],
        "防爆电机": ["电机", "motor", "ex motor", "explosion-proof motor"],
        "304": ["304ss", "304不锈钢", "cf8", "sus304"],
        "304L": ["304l", "304l不锈钢", "cf3"],
        "316L": ["316ss", "316l不锈钢", "316l", "sus316l"],
        "316": ["316ss", "sus316"],
        "2205": ["2205", "双相钢", "duplex"],
        "C276": ["c276", "哈氏合金", "hastelloy"],
        "铸钢": ["铸钢", "碳钢", "wcb", "cast steel", "carbon steel"],
        "氟橡胶": ["氟橡胶", "fkm", "viton", "氟胶"],
        "丁腈橡胶": ["丁腈", "丁腈橡胶", "nbr", "nitrile"],
        "硅橡胶": ["硅胶", "硅橡胶", "vmq", "silicone"],
        "全氟醚": ["全氟醚", "ffkm", "kalrez", "perfluoroelastomer"],
        "碳化硅": ["sic", "碳化硅", "silicon carbide"],
        "钛合金": ["钛", "钛合金", "ti", "titanium"],
        "铬钼钢": ["铬钼钢", "crmo"],
        "Q235": ["q235", "q235b"],
        "35CrMoA": ["35crmo", "35crmoa"],
        "1.6MPa": ["1.6mpa", "pn16", "16公斤", "16bar", "class150", "150lb", "150#"],
        "2.5MPa": ["2.5mpa", "pn25", "25公斤", "25bar", "class300", "300#"],
        "4.0MPa": ["4.0mpa", "pn40", "40公斤", "40bar", "class600", "600#"],
        "10MPa": ["10mpa", "pn100", "100公斤", "100bar"],
        "0.6MPa": ["0.6mpa", "6bar", "class150", "150lb"],
        "1.0MPa": ["1.0mpa", "10bar", "pn10"],
        "6.0MPa": ["6.0mpa", "60bar", "class600"],
        "25MPa": ["25mpa", "250bar", "250#"],
    }

    BRANDS = [
        "SKF", "FAG", "NSK", "NTN", "Koyo", "INA", "Timken", "罗斯蒙特", "Rosemount",
        "横河", "Yokogawa", "霍尼韦尔", "Honeywell", "E+H", "Endress+Hauser", "西门子",
        "Siemens", "ABB", "Festo", "SMC", "壳牌", "Shell", "美孚", "Mobil", "长城",
        "Great Wall", "昆仑", "Kunlun",
    ]

    CATEGORY_PREFIX = {
        "机械密封": "MDM-48", "轴承": "MDM-51", "深沟球轴承": "MDM-51", "O型圈": "MDM-51", "滤芯": "MDM-51",
        "润滑油": "MDM-51", "螺栓": "MDM-51", "法兰": "MDM-52", "闸阀": "MDM-53",
        "压力变送器": "MDM-58", "防爆电机": "MDM-58",
    }

    CATEGORY_PATTERNS = [
        ("机械密封", re.compile(r"机封|密封|seal|端面密封|波纹管|集装", re.I)),
        ("深沟球轴承", re.compile(r"轴承|bearing|skf|fag|nsk|ntn|koyo", re.I)),
        ("闸阀", re.compile(r"闸阀|闸伐|gate valve|闸板", re.I)),
        ("O型圈", re.compile(r"o[ -]?ring|o\s*型圈|密封圈|oring", re.I)),
        ("法兰", re.compile(r"法兰|flange|盲板", re.I)),
        ("压力变送器", re.compile(r"变送器|transmitter|rosemount|横河|霍尼韦尔|西门子|3051|eja", re.I)),
        ("防爆电机", re.compile(r"防爆电机|电机|ex motor|yb3|motor", re.I)),
        ("滤芯", re.compile(r"滤芯|filter|滤清器", re.I)),
        ("润滑油", re.compile(r"润滑油|液压油|齿轮油|柴油机油|锂基脂|lubricant|oil|grease", re.I)),
        ("螺栓", re.compile(r"螺栓|螺柱|bolt|stud", re.I)),
    ]

    RAW_CATEGORY_MAP = {
        "轴承": "轴承", "密封圈": "O型圈", "阀门": "闸阀",
        "电机": "防爆电机", "润滑油": "润滑油",
    }

    # Keep the Flask and standalone browser editions on the same grouping policy.
    SIMILARITY_THRESHOLD = 0.55

    def standardize_text(self, text: str) -> str:
        value = _clean_value(text).lower()
        # JavaScript enumerates integer-like object keys first; mirror that order
        # so the Flask and standalone editions normalize overlapping aliases alike.
        entries = list(self.SYNONYMS.items())
        numeric = sorted((item for item in entries if item[0].isdigit()), key=lambda item: int(item[0]))
        named = [item for item in entries if not item[0].isdigit()]
        for standard, synonyms in numeric + named:
            for synonym in synonyms:
                value = re.sub(re.escape(synonym.lower()), standard, value, flags=re.I)
        return value

    def detect_category(self, name: str, raw_category: str = "") -> str:
        text = f"{name or ''} {raw_category or ''}".strip()
        mapped = self.RAW_CATEGORY_MAP.get(_clean_value(raw_category))
        if mapped:
            return mapped
        for category, pattern in self.CATEGORY_PATTERNS:
            if pattern.search(text):
                return category
        return _clean_value(raw_category) or "其他"

    def extract_brand(self, text: str) -> str:
        lower = _clean_value(text).lower()
        for brand in self.BRANDS:
            if brand.lower() in lower:
                return brand
        return ""

    def extract_model(self, text: str) -> str:
        patterns = [
            r"\bcm0?4b[-–—]?0?\d{2,3}\b", r"\b6[23]\d{2}[-]?\d?[a-z0-9]*\b",
            r"\bz9?4\d[hwp][-]?\d{1,3}[cp]?", r"\byb3[-–—]?\d{3}[mls][-]?\d",
            r"\beja\d{3}[ae]?\b", r"\b3051[a-z0-9]*\b", r"\bpmp\d{2}\b",
            r"\bhp\d{4}[a-z]*\d*\b", r"\bpaf[-–—]?\d[-–—]?0\.\d{2}\b",
            r"\bac[-–—]?\d{3}\b", r"\bjcg[-–—]?\d{3}\b", r"\blx[-–—]?\d{3}\b",
            r"\bsx[-–—]?\d{3}\b", r"\bcng[-–—]?\d{2}\b",
            r"\bm1[6-9]|m[2-3][0-9]?\b", r"\bp320\b", r"\bl-hm46\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, _clean_value(text), re.I | re.ASCII)
            if match:
                return re.sub(r"[-–—]", "-", match.group(0).upper())
        return ""

    def extract_dn(self, text: str) -> str:
        value = _clean_value(text)
        match = re.search(r"DN\s*(\d+)", value, re.I) or re.search(r"(\d+)\s*mm", value, re.I)
        if match:
            return match.group(1)
        inch = re.search(r"(\d+)\s*寸", value)
        if inch:
            return {"3": "80", "4": "100", "6": "150", "8": "200", "10": "250", "12": "300"}.get(inch.group(1), inch.group(1))
        return ""

    def extract_pressure(self, text: str) -> str:
        value = _clean_value(text).lower()
        patterns = [
            ("0.6MPa", r"0\.6mpa|6bar|class150|150lb|150#"),
            ("1.0MPa", r"1\.0mpa|10bar"),
            ("1.6MPa", r"1\.6mpa|pn16|16公斤|16bar|class150|150lb|150#"),
            ("2.5MPa", r"2\.5mpa|pn25|25公斤|25bar|class300|300#"),
            ("4.0MPa", r"4\.0mpa|pn40|40公斤|40bar|class600"),
            ("6.0MPa", r"6\.0mpa|60bar|class600"),
            ("10MPa", r"10mpa|pn100|100公斤|100bar"),
            ("25MPa", r"25mpa|250bar"),
        ]
        for standard, pattern in patterns:
            if re.search(pattern, value, re.I):
                return standard
        match = re.search(r"(\d+\.?\d*)\s*(mpa|bar|公斤)", value, re.I)
        return f"{match.group(1)}MPa" if match else ""

    def extract_material(self, text: str) -> str:
        value = _clean_value(text).lower()
        patterns = [
            ("全氟醚", r"全氟醚|ffkm|kalrez"), ("35CrMoA", r"35crmo"),
            ("丁腈橡胶", r"丁腈|nbr"), ("硅橡胶", r"硅橡胶|vmq|silicone"),
            ("氟橡胶", r"氟橡胶|fkm|viton|氟胶"), ("碳化硅", r"碳化硅|sic"),
            ("铬钼钢", r"铬钼钢|crmo"), ("哈氏合金", r"哈氏合金|c276|hastelloy"),
            ("钛合金", r"钛合金|钛材|titanium|(?<![a-z0-9])ti(?![a-z0-9])"), ("316L", r"316l|sus316l"),
            ("316", r"(316|316ss|sus316)(?!l)"), ("304L", r"304l|cf3"),
            ("304", r"(304|304ss|cf8|sus304)(?!l)"),
            ("2205", r"2205|双相钢|duplex"), ("Q235", r"q235|q235b"), ("铸钢", r"铸钢|碳钢|wcb"),
        ]
        for standard, pattern in patterns:
            if re.search(pattern, value, re.I):
                return standard
        return ""

    def enrich(self, record: dict) -> dict:
        extra = record.get("_extra") if isinstance(record.get("_extra"), dict) else {}
        semantic_extra = " ".join(
            _clean_value(value) for key, value in extra.items()
            if any(token in _normalize_header(key) for token in ("子类", "型号", "规格", "brand", "model", "material", "材质"))
        )
        text = f"{record.get('material_name', '')} {record.get('description', '')} {semantic_extra}"
        enriched = dict(record)
        enriched["_ext"] = {
            "model": self.extract_model(text), "brand": self.extract_brand(text), "dn": self.extract_dn(text),
            "pressure": self.extract_pressure(text), "material": self.extract_material(text),
            "raw_fields": extra,
        }
        enriched["_category"] = self.detect_category(record.get("material_name", ""), record.get("category", ""))
        return enriched

    def semantic_tokens(self, record: dict) -> list[str]:
        extra = record.get("_extra") if isinstance(record.get("_extra"), dict) else {}
        semantic_extra = " ".join(
            _clean_value(value) for key, value in extra.items()
            if any(token in _normalize_header(key) for token in ("子类", "型号", "规格", "brand", "model", "material", "材质"))
        )
        text = f"{record.get('material_name', '')} {record.get('description', '')} {semantic_extra}"
        standard = self.standardize_text(text)
        ext = record["_ext"]
        tokens = []
        if ext["model"]:
            tokens.extend([ext["model"]] * 3)
        for key in ("brand", "dn", "pressure", "material"):
            if ext[key]:
                token = f"DN{ext[key]}" if key == "dn" else ext[key]
                tokens.extend([token] * 2)
        tokens.extend(re.findall(r"[\u4e00-\u9fa5]{2,5}", standard)[:6])
        clean = re.sub(r"[^\u4e00-\u9fa5a-z0-9]", "", standard)[:400]
        for size in (2, 3):
            tokens.extend(clean[index:index + size] for index in range(max(0, len(clean) - size + 1)))
        return tokens or ["empty"]

    @staticmethod
    def term_frequency(tokens: list[str]) -> dict[str, float]:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        total = len(tokens) or 1
        return {token: count / total for token, count in counts.items()}

    @staticmethod
    def cosine(left: dict[str, float], right: dict[str, float]) -> float:
        dot = sum(value * right.get(token, 0.0) for token, value in left.items())
        left_norm = sum(value * value for value in left.values()) ** 0.5
        right_norm = sum(value * value for value in right.values()) ** 0.5
        if not left_norm or not right_norm:
            return 0.0
        return dot / (left_norm * right_norm)

    def detect_anomalies(self, records: list[dict]) -> set[int]:
        if len(records) < 10:
            return set()
        features = [[
            len(record.get("material_name", "")), len(record.get("description", "")),
            int(bool(record.get("system_source"))), int(bool(record.get("material_code"))),
        ] for record in records]
        model = IsolationForest(contamination=min(0.1, 10 / len(records)), random_state=42)
        return {index for index, prediction in enumerate(model.fit_predict(features)) if prediction == -1}

    @staticmethod
    def detect_conflicts(items: list[dict]) -> list[dict]:
        labels = {"model": "型号", "dn": "口径", "pressure": "压力", "material": "材质"}
        conflicts = []
        for field, label in labels.items():
            values = sorted({item["_ext"].get(field) for item in items if item["_ext"].get(field)})
            if len(values) > 1:
                conflicts.append({"field": label, "values": values})
        return conflicts

    @staticmethod
    def presentation_attributes(item: dict, category: str) -> dict:
        """Remove extraction artifacts from displayed golden data without changing grouping history."""
        attributes = dict(item.get("_ext") or {})
        text = f"{item.get('material_name', '')} {item.get('description', '')}"
        if "轴承" in category:
            model = _clean_value(attributes.get("model")).lower()
            material = _clean_value(attributes.get("material")).lower()
            if material and model and material in model:
                attributes["material"] = ""
            attributes["pressure"] = ""
            attributes["dn"] = ""
        if category == "润滑油":
            attributes["pressure"] = ""
            attributes["dn"] = ""
        inch = re.search(r"(\d+)\s*寸", text)
        if inch and category in {"闸阀", "法兰"}:
            attributes["dn"] = {
                "1": "25", "2": "50", "3": "80", "4": "100", "5": "125", "6": "150",
                "8": "200", "10": "250", "12": "300",
            }.get(inch.group(1), attributes.get("dn", ""))
        return attributes

    def generate_standard_name(self, items: list[dict], category: str) -> str:
        base = max(items, key=lambda item: len(item.get("material_name", "")))
        ext = self.presentation_attributes(base, category)
        parts = []
        if ext["brand"]:
            parts.append(ext["brand"])
        if ext["model"]:
            parts.append(ext["model"])
        parts.append(category)
        specs = []
        if ext["dn"]:
            specs.append(f"DN{ext['dn']}")
        if ext["pressure"]:
            specs.append(ext["pressure"])
        if ext["material"]:
            specs.append(ext["material"])
        parts.extend(specs)
        return " ".join(dict.fromkeys(part for part in parts if part))

    def govern(
        self, records: list[dict], semantic=None, preferred_model: str = "qwen",
        similarity_threshold: float | None = None,
    ) -> tuple[list[dict], list[dict], list[dict], dict]:
        threshold = self.SIMILARITY_THRESHOLD if similarity_threshold is None else float(similarity_threshold)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("similarity_threshold must be between 0 and 1")
        enriched = [self.enrich(record) for record in records]
        vectors = [self.term_frequency(self.semantic_tokens(record)) for record in enriched]
        semantic_texts = [
            self.standardize_text(
                f"{record.get('material_name', '')} {record.get('description', '')} {record['_category']}"
            )
            for record in enriched
        ]
        if semantic is not None:
            embedding_vectors, semantic_meta = semantic.resolve_embeddings(semantic_texts, preferred_model)
        else:
            embedding_vectors, semantic_meta = None, {
                "method": "规则增强 Jaccard 字符相似度（降级）", "model": "text-embedding-v3",
                "provider": "qwen", "dimension": None, "embedding_active": False,
                "warning": "语义引擎未启用，已使用确定性本地语义相似度。",
            }
        similarities = [
            [self.cosine(vectors[left], vectors[right]) for right in range(len(enriched))]
            for left in range(len(enriched))
        ]
        anomalies = self.detect_anomalies(enriched)

        parent = list(range(len(enriched)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[left_root] = right_root

        for left in range(len(enriched)):
            for right in range(left + 1, len(enriched)):
                a, b = enriched[left]["_ext"], enriched[right]["_ext"]
                if a["model"] and b["model"] and a["model"] != b["model"]:
                    continue
                if a["pressure"] and b["pressure"] and a["pressure"] != b["pressure"]:
                    continue
                if a["dn"] and b["dn"] and a["dn"] != b["dn"]:
                    continue
                local_score = float(similarities[left][right])
                semantic_score = (
                    semantic.cosine_similarity(embedding_vectors[left], embedding_vectors[right])
                    if embedding_vectors is not None else (
                        semantic.jaccard_similarity(semantic_texts[left], semantic_texts[right])
                        if semantic is not None else SemanticEngine.jaccard_similarity(semantic_texts[left], semantic_texts[right])
                    )
                )
                score = local_score
                if a["brand"] and a["brand"] == b["brand"]:
                    score += 0.15
                if a["material"] and a["material"] == b["material"]:
                    score += 0.10
                local_match = min(score, 1.0) >= threshold
                semantic_match = semantic_score >= 0.85
                if local_match or semantic_match:
                    union(left, right)

        groups: dict[int, list[int]] = {}
        for index in range(len(enriched)):
            groups.setdefault(find(index), []).append(index)

        masters, reviews, mappings = [], [], []
        for indices in groups.values():
            items = [enriched[index] for index in indices]
            category = items[0]["_category"]
            conflicts = self.detect_conflicts(items)
            decision = "NEW" if len(items) == 1 else ("REVIEW" if conflicts else "AUTO_MERGE")
            confidence = 0.60 if decision == "NEW" else (0.72 if decision == "REVIEW" else 0.92)
            standard_name = self.generate_standard_name(items, category)
            public_attributes = self.presentation_attributes(items[0], category)
            prefix = self.CATEGORY_PREFIX.get(category, "MDM-X")
            source_content_signature = "/".join(sorted({
                self.standardize_text(
                    f"{item.get('material_name', '')} {item.get('description', '')}"
                )
                for item in items
            }))
            signature = "|".join(
                [category, standard_name]
                + [items[0]["_ext"].get(key, "") for key in ("model", "brand", "dn", "pressure", "material")]
                + [source_content_signature]
            )
            mdm_code = f"{prefix}-{hashlib.sha1(signature.encode('utf-8')).hexdigest()[:8].upper()}"
            systems = sorted({item.get("system_source", "") for item in items if item.get("system_source")})
            plants = sorted({_normalize_plant_code(item.get("plant_code")) for item in items})
            source_codes = [item.get("material_code", "") for item in items if item.get("material_code")]
            anomaly_count = sum(1 for index in indices if index in anomalies)
            master = {
                "mdm_code": mdm_code, "standard_name": standard_name, "category": category,
                **public_attributes, "source_count": len(items), "source_systems": ",".join(systems),
                "decision": decision, "confidence": round(confidence, 4), "code_prefix": prefix,
                "source_records": ";".join(source_codes), "anomaly_count": anomaly_count,
                "plant_codes": ",".join(plants),
                "_ext": public_attributes,
            }
            masters.append(master)
            rules = "R007" if decision == "NEW" else ("R006/R008" if decision == "REVIEW" else "R001/R005")
            rules += "/R010-EMBEDDING" if embedding_vectors is not None else "/R010-JACCARD"
            if anomaly_count:
                rules += "/R009"
            for item in items:
                mappings.append({
                    "system_source": item.get("system_source", ""), "original_code": item.get("material_code", ""),
                    "original_name": item.get("material_name", ""), "mdm_code": mdm_code,
                    "standard_name": standard_name, "decision": decision, "similarity": round(confidence, 4),
                    "applied_rules": rules, "plant_code": _normalize_plant_code(item.get("plant_code")),
                })
            if decision == "REVIEW":
                reason = "；".join(f"{item['field']}冲突（{' / '.join(item['values'])}）" for item in conflicts)
                reviews.append({
                    "mdm_code": mdm_code, "standard_name": standard_name, "decision": decision,
                    "reason": reason or "中置信度记录需要人工确认", "applied_rules": rules,
                    "source_records": ";".join(source_codes), "source_systems": ",".join(systems),
                    "confidence": round(confidence, 4), "category": category, "_ext": public_attributes,
                    "candidates": [], "status": "REVIEW", "plant_codes": ",".join(plants),
                })
        semantic_meta = {**semantic_meta, "local_similarity_threshold": threshold}
        return masters, reviews, mappings, semantic_meta


engine = AIGovernanceEngine()


class SemanticEngine:
    """Multi-provider embeddings with Qwen as the primary competition model."""

    MODELS = {
        "qwen": {
            "name": "通义千问", "url": "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
            "model": "text-embedding-v3", "dimension": 1024, "env_key": "DASHSCOPE_API_KEY",
        },
        "zhipu": {
            "name": "智谱AI", "url": "https://open.bigmodel.cn/api/paas/v4/embeddings",
            "model": "embedding-3", "dimension": 2048, "env_key": "ZHIPU_API_KEY",
        },
        "openai": {
            "name": "OpenAI", "url": "https://api.openai.com/v1/embeddings",
            "model": "text-embedding-3-small", "dimension": 1536, "env_key": "OPENAI_API_KEY",
        },
    }
    API_URL = MODELS["qwen"]["url"]
    MODEL = MODELS["qwen"]["model"]
    DIMENSION = MODELS["qwen"]["dimension"]

    def __init__(self, timeout: tuple[float, float] = (3.05, 12.0)):
        self.timeout = timeout
        self.api_keys = {
            key: os.getenv(config["env_key"], "").strip() for key, config in self.MODELS.items()
        }
        self._cache: dict[tuple[str, str, str], list[float]] = {}

    def configured_models(self) -> list[str]:
        return [key for key in self.MODELS if self.api_keys.get(key)]

    def _request_embeddings(
        self, texts: list[str], model_key: str = "qwen", text_type: str = "document"
    ) -> list[list[float]] | None:
        if has_request_context() and not getattr(g, "external_ai_allowed", True):
            logger.info("external embedding blocked by data classification trace_id=%s", getattr(g, "trace_id", ""))
            return None
        config = self.MODELS.get(model_key)
        api_key = self.api_keys.get(model_key, "")
        if not config or not api_key or not texts:
            return None
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        values = [text[:2048] for text in texts]
        if model_key == "qwen":
            payload = {
                "model": config["model"], "input": {"texts": values},
                "parameters": {"dimension": config["dimension"], "text_type": text_type},
            }
        else:
            payload = {"model": config["model"], "input": values}
        try:
            response = requests.post(config["url"], headers=headers, json=payload, timeout=self.timeout)
            if response.status_code != 200:
                logger.warning("%s embedding returned HTTP %s", config["name"], response.status_code)
                return None
            data = response.json()
            if model_key == "qwen":
                items = sorted(data["output"]["embeddings"], key=lambda item: int(item.get("text_index", 0)))
            else:
                items = sorted(data["data"], key=lambda item: int(item.get("index", 0)))
            vectors = [[float(value) for value in item["embedding"]] for item in items]
            if len(vectors) != len(values) or any(len(vector) != config["dimension"] for vector in vectors):
                logger.warning("%s embedding response has an unexpected shape", config["name"])
                return None
            if any(not np.isfinite(np.asarray(vector, dtype=float)).all() for vector in vectors):
                logger.warning("%s embedding response contains non-finite values", config["name"])
                return None
            return vectors
        except (requests.Timeout, requests.RequestException, KeyError, TypeError, ValueError, IndexError) as exc:
            logger.warning("%s embedding unavailable: %s", config["name"], exc)
            return None

    def get_embedding(self, text: str, model_key: str = "qwen", text_type: str = "document") -> list[float] | None:
        vectors = self.get_embeddings([text], model_key=model_key, text_type=text_type)
        return vectors[0] if vectors else None

    def get_embeddings(
        self, texts: list[str], batch_size: int = 10, model_key: str = "qwen", text_type: str = "document"
    ) -> list[list[float]] | None:
        if model_key not in self.MODELS:
            return None
        values = [_clean_value(text) for text in texts]
        if not values or any(not value for value in values):
            return None
        missing = list(dict.fromkeys(value for value in values if (model_key, text_type, value) not in self._cache))
        for offset in range(0, len(missing), batch_size):
            chunk = missing[offset:offset + batch_size]
            vectors = self._request_embeddings(chunk, model_key=model_key, text_type=text_type)
            if not vectors:
                return None
            self._cache.update(((model_key, text_type, text), vector) for text, vector in zip(chunk, vectors))
        return [self._cache[(model_key, text_type, value)] for value in values]

    def resolve_embeddings(self, texts: list[str], preferred_model: str = "qwen") -> tuple[list[list[float]] | None, dict]:
        preferred = preferred_model if preferred_model in self.MODELS else "qwen"
        if has_request_context() and not getattr(g, "external_ai_allowed", True):
            requested = self.MODELS[preferred]
            return None, {
                "method": "规则增强 Jaccard 字符相似度（数据不出域）", "model": requested["model"],
                "provider": preferred, "dimension": None, "embedding_active": False,
                "warning": f"{getattr(g, 'data_classification', 'RESTRICTED')} 数据按策略禁止发送至外部Embedding服务。",
            }
        order = [preferred] + [key for key in ("qwen", "zhipu", "openai") if key != preferred]
        attempted = []
        for model_key in order:
            if not self.api_keys.get(model_key):
                continue
            attempted.append(model_key)
            vectors = self.get_embeddings(texts, model_key=model_key)
            if vectors is not None:
                config = self.MODELS[model_key]
                return vectors, {
                    "method": f"{config['name']} {config['model']} 向量余弦相似度",
                    "model": config["model"], "provider": model_key,
                    "dimension": config["dimension"], "warning": None, "embedding_active": True,
                }
        requested = self.MODELS[preferred]
        reason = "API Key未配置" if not attempted else "已配置模型调用失败"
        return None, {
            "method": "规则增强 Jaccard 字符相似度（降级）", "model": requested["model"],
            "provider": preferred, "dimension": None, "embedding_active": False,
            "warning": f"{requested['name']} {reason}，已使用确定性本地语义相似度。",
        }

    @staticmethod
    def local_embedding(text: str, dimension: int = 384) -> list[float]:
        """Deterministic feature-hash embedding used for an always-available vector index."""
        normalized = engine.standardize_text(_clean_value(text))
        chars = list(normalized)
        tokens = chars + [normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))]
        tokens += re.findall(r"[a-z0-9]+(?:[-./][a-z0-9]+)*", normalized)
        vector = np.zeros(dimension, dtype=np.float32)
        for token in tokens or ["<empty>"]:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % dimension
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = float(np.linalg.norm(vector))
        if norm:
            vector /= norm
        return vector.tolist()

    def resolve_vectorization(
        self, texts: list[str], preferred_model: str = "qwen", text_type: str = "document"
    ) -> tuple[list[list[float]], dict]:
        """Resolve embeddings for persistence; unlike pairwise demo similarity, this always returns vectors."""
        if has_request_context() and not getattr(g, "external_ai_allowed", True):
            return [self.local_embedding(text) for text in texts], {
                "provider": "local", "model": "feature-hash-v1", "dimension": 384,
                "method": "离线特征哈希向量（数据不出域）",
                "warning": f"{getattr(g, 'data_classification', 'RESTRICTED')} 数据按策略仅使用本地向量。",
                "remote": False,
            }
        if preferred_model == "local":
            return [self.local_embedding(text) for text in texts], {
                "provider": "local", "model": "feature-hash-v1", "dimension": 384,
                "method": "离线特征哈希向量", "warning": None, "remote": False,
            }
        if preferred_model in self.MODELS and self.api_keys.get(preferred_model):
            vectors = self.get_embeddings(texts, model_key=preferred_model, text_type=text_type)
            if vectors is not None:
                config = self.MODELS[preferred_model]
                return vectors, {
                    "provider": preferred_model, "model": config["model"], "dimension": config["dimension"],
                    "method": f"{config['name']} {config['model']}", "warning": None, "remote": True,
                }
        vectors = [self.local_embedding(text) for text in texts]
        requested = self.MODELS.get(preferred_model, self.MODELS["qwen"])
        return vectors, {
            "provider": "local", "model": "feature-hash-v1", "dimension": 384,
            "method": "离线特征哈希向量", "warning": f"{requested['name']}不可用，向量库已自动使用本地确定性向量。",
            "remote": False,
        }

    @staticmethod
    def cosine_similarity(left: list[float], right: list[float]) -> float:
        left_vector = np.asarray(left, dtype=float)
        right_vector = np.asarray(right, dtype=float)
        if left_vector.shape != right_vector.shape or left_vector.ndim != 1:
            return 0.0
        denominator = float(np.linalg.norm(left_vector) * np.linalg.norm(right_vector))
        if denominator == 0:
            return 0.0
        return max(-1.0, min(1.0, float(np.dot(left_vector, right_vector) / denominator)))

    @staticmethod
    def jaccard_similarity(left: str, right: str) -> float:
        left_chars = set(re.sub(r"\s+", "", _clean_value(left).lower()))
        right_chars = set(re.sub(r"\s+", "", _clean_value(right).lower()))
        union = left_chars | right_chars
        return len(left_chars & right_chars) / len(union) if union else 1.0


class OCREngine:
    """Lazy real-OCR adapter with cloud and deterministic fallbacks."""

    QWEN_VL_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    def __init__(self):
        self.paddle_enabled = os.getenv("MDM_PADDLEOCR_ENABLED", "1") != "0"
        self.paddle_lang = os.getenv("MDM_PADDLEOCR_LANG", "ch").strip() or "ch"
        self.qwen_model = os.getenv("QWEN_VL_MODEL", "qwen-vl-plus").strip() or "qwen-vl-plus"
        self.qwen_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self._paddle = None
        self._paddle_error = ""

    def status(self) -> dict:
        local_installed = importlib.util.find_spec("paddleocr") is not None
        external_ready = _external_ocr_ready()
        return {
            "paddle_enabled": self.paddle_enabled,
            "paddle_installed": local_installed or external_ready,
            "paddle_ready": bool(self._paddle) or external_ready,
            "paddle_mode": "isolated-python-3.11" if external_ready else ("in-process" if local_installed else "missing"),
            "external_runtime_ready": external_ready,
            "paddle_error": self._paddle_error or None,
            "qwen_vl_configured": bool(self.qwen_key),
            "qwen_vl_model": self.qwen_model,
            "fallback": "rule-parser",
        }

    @staticmethod
    def decode_data_image(value: str) -> tuple[bytes, str]:
        encoded = _clean_value(value)
        mime_type = "image/jpeg"
        if encoded.startswith("data:"):
            header, separator, encoded = encoded.partition(",")
            if not separator or ";base64" not in header.lower():
                raise ValueError("image data URI must use base64 encoding")
            mime_type = header[5:].split(";", 1)[0] or mime_type
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("image contains invalid base64 data") from exc
        return raw, mime_type

    def _load_paddle(self):
        if self._paddle is not None:
            return self._paddle
        if not self.paddle_enabled or importlib.util.find_spec("paddleocr") is None:
            return None
        try:
            from paddleocr import PaddleOCR

            try:
                self._paddle = PaddleOCR(
                    lang=self.paddle_lang, enable_mkldnn=False, use_doc_orientation_classify=False,
                    use_doc_unwarping=False, use_textline_orientation=False,
                )
            except TypeError:
                self._paddle = PaddleOCR(lang=self.paddle_lang, use_angle_cls=True, enable_mkldnn=False)
            self._paddle_error = ""
            return self._paddle
        except Exception as exc:
            self._paddle_error = f"{type(exc).__name__}: {exc}"
            logger.warning("PaddleOCR initialization failed: %s", exc)
            return None

    @staticmethod
    def _extract_lines(value) -> tuple[list[str], list[float]]:
        texts: list[str] = []
        scores: list[float] = []
        visited: set[int] = set()

        def add(text, score=None):
            clean = _clean_value(text)
            if clean and clean not in texts:
                texts.append(clean)
                try:
                    scores.append(float(score))
                except (TypeError, ValueError):
                    pass

        def walk(item):
            if item is None or isinstance(item, (str, bytes, int, float, bool)):
                return
            item_id = id(item)
            if item_id in visited:
                return
            visited.add(item_id)
            if hasattr(item, "json"):
                exported = item.json() if callable(item.json) else item.json
                if isinstance(exported, str):
                    try:
                        exported = json.loads(exported)
                    except json.JSONDecodeError:
                        exported = None
                if exported is not None:
                    walk(exported)
            if isinstance(item, dict):
                rec_texts = item.get("rec_texts")
                rec_scores = item.get("rec_scores") or []
                if isinstance(rec_texts, list):
                    for index, text in enumerate(rec_texts):
                        add(text, rec_scores[index] if index < len(rec_scores) else None)
                for key in ("res", "result", "data"):
                    if key in item:
                        walk(item[key])
                return
            if isinstance(item, (list, tuple)):
                if len(item) >= 2 and isinstance(item[0], str) and isinstance(item[1], (int, float)):
                    add(item[0], item[1])
                    return
                if len(item) >= 2 and isinstance(item[1], (list, tuple)) and item[1] and isinstance(item[1][0], str):
                    add(item[1][0], item[1][1] if len(item[1]) > 1 else None)
                    return
                for child in item:
                    walk(child)

        walk(value)
        return texts, scores

    def _paddle_recognize(self, image_bytes: bytes, filename: str) -> tuple[str, float | None]:
        external_ready = _external_ocr_ready()
        # The validated Python 3.11 worker is the primary Windows path. Loading
        # PaddleOCR in the Flask runtime can select an incompatible oneDNN build.
        paddle = None if external_ready else self._load_paddle()
        if paddle is None and not external_ready:
            return "", None
        suffix = Path(filename or "image.jpg").suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}:
            suffix = ".jpg"
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="mai-ocr-", suffix=suffix, delete=False) as temp_file:
                temp_file.write(image_bytes)
                temp_path = Path(temp_file.name)
            if paddle is not None:
                result = list(paddle.predict(str(temp_path))) if hasattr(paddle, "predict") else paddle.ocr(str(temp_path), cls=True)
                texts, scores = self._extract_lines(result)
                confidence = round(sum(scores) / len(scores), 4) if scores else None
                return "\n".join(texts), confidence
            worker = subprocess.run(
                [str(OCR_RUNTIME_PYTHON), str(OCR_WORKER_PATH), "--image", str(temp_path), "--lang", self.paddle_lang],
                cwd=str(PROJECT_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=int(os.getenv("MDM_OCR_WORKER_TIMEOUT", "180")),
                env={**os.environ, "PADDLE_PDX_MODEL_SOURCE": os.getenv("PADDLE_PDX_MODEL_SOURCE", "BOS")},
            )
            payload = None
            for line in reversed((worker.stdout or "").splitlines()):
                if line.startswith("MAI_OCR_RESULT="):
                    payload = json.loads(line.split("=", 1)[1])
                    break
            if worker.returncode or not payload or not payload.get("ready"):
                detail = (payload or {}).get("error") or (worker.stderr or worker.stdout or "worker failed")[-500:]
                raise RuntimeError(detail)
            return _clean_value(payload.get("text")), payload.get("confidence")
        except Exception as exc:
            self._paddle_error = f"{type(exc).__name__}: {exc}"
            logger.warning("PaddleOCR inference failed: %s", exc)
            return "", None
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

    def _qwen_vl_recognize(self, image_bytes: bytes, mime_type: str) -> str:
        if has_request_context() and not getattr(g, "external_ai_allowed", True):
            logger.info("external vision model blocked by data classification trace_id=%s", getattr(g, "trace_id", ""))
            return ""
        if not self.qwen_key:
            return ""
        data_uri = f"data:{mime_type or 'image/jpeg'};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        prompt = (
            "识别这张工业物料或铭牌图片中的全部可见文字。只输出原始文字，逐行分隔；"
            "不要推测图片中未出现的品牌、型号、压力、口径或材质。"
        )
        try:
            response = requests.post(
                self.QWEN_VL_URL,
                headers={"Authorization": f"Bearer {self.qwen_key}", "Content-Type": "application/json"},
                json={"model": self.qwen_model, "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ]}], "temperature": 0}, timeout=(3.05, 30),
            )
            if response.status_code != 200:
                logger.warning("Qwen-VL OCR returned HTTP %s", response.status_code)
                return ""
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "\n".join(_clean_value(item.get("text")) for item in content if isinstance(item, dict))
            return _clean_value(content)
        except (requests.RequestException, requests.Timeout, KeyError, TypeError, ValueError) as exc:
            logger.warning("Qwen-VL OCR unavailable: %s", exc)
            return ""

    def recognize(self, image_bytes: bytes, filename: str, mime_type: str, hint_text: str = "") -> dict:
        warnings = []
        raw_text, confidence = self._paddle_recognize(image_bytes, filename)
        provider = "paddleocr-local"
        if not raw_text:
            if self.paddle_enabled and self._paddle_error:
                warnings.append(f"PaddleOCR不可用：{self._paddle_error}")
            elif self.paddle_enabled and importlib.util.find_spec("paddleocr") is None:
                warnings.append("PaddleOCR未安装")
            raw_text = self._qwen_vl_recognize(image_bytes, mime_type)
            provider = "qwen-vl"
        if not raw_text:
            provider = "rule-fallback"
            raw_text = _clean_value(hint_text or filename)
            confidence = 0.5 if raw_text else 0.0
            warnings.append("真实OCR引擎不可用，已使用文件提示文字进行规则解析")
        return {
            "provider": provider, "raw_text": raw_text, "ocr_confidence": confidence,
            "real_ocr": provider in {"paddleocr-local", "qwen-vl"},
            "warning": "；".join(warnings) or None,
        }


class OCRInstallManager:
    """Runs the fixed Windows installer asynchronously and exposes bounded progress."""

    def __init__(self):
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._started_at: str | None = None
        self._exit_code: int | None = None

    @staticmethod
    def _log_tail() -> str:
        if not OCR_INSTALL_LOG.is_file():
            return ""
        try:
            text = OCR_INSTALL_LOG.read_text(encoding="utf-8", errors="replace")
            text = re.sub(r"(https?://)[^/\s:@]+:[^@\s/]+@", r"\1***:***@", text)
            text = re.sub(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{12,}\b", "***", text)
            return "\n".join(text.splitlines()[-30:])[-5000:]
        except OSError:
            return ""

    def status(self) -> dict:
        with self._lock:
            process = self._process
            if process is not None:
                code = process.poll()
                if code is not None:
                    self._exit_code = code
                    self._process = None
            else:
                code = self._exit_code
            lock_active = OCR_INSTALL_LOCK.is_dir()
            if lock_active and process is None:
                try:
                    max_lock_age = max(60, int(os.getenv("MDM_OCR_INSTALL_LOCK_SECONDS", "3600")))
                    if time.time() - OCR_INSTALL_LOCK.stat().st_mtime >= max_lock_age:
                        OCR_INSTALL_LOCK.rmdir()
                        lock_active = False
                except (OSError, ValueError):
                    pass
            running = (process is not None and process.poll() is None) or lock_active
            ready = _external_ocr_ready() or importlib.util.find_spec("paddleocr") is not None
            log_tail = self._log_tail()
            progress = 100 if ready else 0
            for marker, value in (("[1/4]", 10), ("[2/4]", 25), ("[3/4]", 45), ("[4/4]", 80)):
                if marker in log_tail:
                    progress = max(progress, value)
            if ready:
                state = "ready"
            elif running:
                state = "installing"
            elif code not in (None, 0):
                state = "failed"
            else:
                state = "missing"
            return {
                "state": state, "progress": progress, "runtime_ready": ready,
                "installing": running, "started_at": self._started_at, "exit_code": code,
                "log_tail": log_tail, "platform_supported": os.name == "nt",
                "auto_install_enabled": os.getenv("MDM_ALLOW_OCR_INSTALL", "1") == "1",
                "qwen_vl_configured": bool(os.getenv("DASHSCOPE_API_KEY", "").strip()),
            }

    def start(self) -> dict:
        with self._lock:
            if _external_ocr_ready() or importlib.util.find_spec("paddleocr") is not None:
                return self.status()
            if self._process is not None and self._process.poll() is None:
                return self.status()
            if OCR_INSTALL_LOCK.is_dir():
                age_seconds = time.time() - OCR_INSTALL_LOCK.stat().st_mtime
                if age_seconds < 3600:
                    return self.status()
                try:
                    OCR_INSTALL_LOCK.rmdir()
                except OSError:
                    return self.status()
            if os.name != "nt":
                raise RuntimeError("automatic OCR installation is supported only on Windows")
            script = PROJECT_DIR / "install-ocr.bat"
            if not script.is_file():
                raise RuntimeError("install-ocr.bat is missing")
            OCR_INSTALL_LOG.parent.mkdir(parents=True, exist_ok=True)
            OCR_READY_MARKER.unlink(missing_ok=True)
            self._exit_code = None
            self._started_at = _utc_now()
            log_file = OCR_INSTALL_LOG.open("w", encoding="utf-8", errors="replace")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self._process = subprocess.Popen(
                    ["cmd.exe", "/d", "/c", str(script), "--non-interactive"],
                    cwd=str(PROJECT_DIR), stdout=log_file, stderr=subprocess.STDOUT,
                    creationflags=flags, env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                )
            finally:
                log_file.close()
        return self.status()


class SemanticDemoGovernor:
    """Semantic greedy clustering used by the standalone governance API."""

    THRESHOLD = 0.85
    CATEGORIES = [
        ("机械密封", ["机械密封", "机封", "端面密封"]),
        ("轴承", ["轴承", "滚动轴承", "bearing"]),
        ("闸阀", ["闸阀", "闸板阀", "gate valve"]),
        ("O型圈", ["O型圈", "密封圈", "o-ring"]),
        ("法兰", ["法兰", "盲法兰", "flange"]),
        ("螺栓", ["螺栓", "双头螺柱", "stud bolt"]),
        ("润滑油", ["润滑油", "润滑脂", "lubricating oil"]),
        ("滤芯", ["滤芯", "过滤器滤材", "filter element"]),
        ("压力变送器", ["压力变送器", "变送器", "pressure transmitter"]),
        ("防爆电机", ["防爆电机", "隔爆电机", "explosion-proof motor"]),
    ]

    def __init__(self, semantic: SemanticEngine, rule_engine: AIGovernanceEngine):
        self.semantic = semantic
        self.rules = rule_engine

    def generate_demo_records(self) -> list[dict]:
        records: list[dict] = []
        sources = ["SAP", "EAM", "采购"]
        plants = ["GROUP", "SHANGHAI", "BEIJING"]
        materials = ["304", "316L", "Q235", "铸钢"]
        pressures = ["1.6MPa", "2.5MPa", "4.0MPa"]
        diameters = ["50", "80", "100", "150", "200"]
        for group_index in range(72):
            category, aliases = self.CATEGORIES[group_index % len(self.CATEGORIES)]
            model = f"MAI-{group_index + 1:03d}"
            pressure = pressures[group_index % len(pressures)]
            diameter = diameters[group_index % len(diameters)]
            material = materials[group_index % len(materials)]
            record_count = 5 if group_index < 12 else (4 if group_index < 50 else 1)
            for variant in range(record_count):
                alias = aliases[variant % len(aliases)]
                row_material = materials[(group_index + 1) % len(materials)] if group_index < 8 and variant == record_count - 1 else material
                records.append({
                    "material_code": f"{sources[variant % 3]}-{group_index + 1:03d}-{variant + 1}",
                    "system_source": sources[variant % 3],
                    "material_name": f"{alias} {model}",
                    "description": f"{model} DN{diameter} {pressure} 集团标准备品备件",
                    "category": category,
                    "unit": "件",
                    "create_time": "2026-07-30",
                    "model": model,
                    "brand": "M-AI",
                    "dn": diameter,
                    "pressure": pressure,
                    "material": row_material,
                    "plant_code": plants[variant % len(plants)],
                })
        return records

    def _prepare(self, record: dict) -> dict:
        prepared = self.rules.enrich(record)
        raw_category = _clean_value(record.get("category"))
        if raw_category:
            prepared["_category"] = self.rules.RAW_CATEGORY_MAP.get(raw_category, raw_category)
        for field in ("model", "brand", "dn", "pressure", "material"):
            direct_value = _clean_value(record.get(field))
            if direct_value:
                prepared["_ext"][field] = direct_value
        prepared["plant_code"] = _clean_value(record.get("plant_code")) or "GROUP"
        return prepared

    def _semantic_text(self, record: dict) -> str:
        ext = record["_ext"]
        category = record["_category"]
        return self.rules.standardize_text(
            f"{category} {ext['model']} {ext['brand']} DN{ext['dn']} {ext['pressure']}"
        )

    @staticmethod
    def _hard_conflict(left: dict, right: dict) -> bool:
        if left["_category"] and right["_category"] and left["_category"] != right["_category"]:
            return True
        for field in ("model", "dn", "pressure"):
            left_value, right_value = left["_ext"].get(field), right["_ext"].get(field)
            if left_value and right_value and left_value != right_value:
                return True
        return False

    def govern(self, records: list[dict], preferred_model: str = "qwen") -> dict:
        prepared = [self._prepare(record) for record in records]
        texts = [self._semantic_text(record) for record in prepared]
        vectors, semantic_meta = self.semantic.resolve_embeddings(texts, preferred_model)
        method = semantic_meta["method"]
        warning = semantic_meta["warning"]

        groups: list[list[int]] = []
        member_scores: dict[int, float] = {}
        for index, record in enumerate(prepared):
            assigned = False
            for group in groups:
                representative = group[0]
                if self._hard_conflict(record, prepared[representative]):
                    continue
                score = (
                    self.semantic.cosine_similarity(vectors[index], vectors[representative])
                    if vectors is not None
                    else self.semantic.jaccard_similarity(texts[index], texts[representative])
                )
                if score >= self.THRESHOLD:
                    group.append(index)
                    member_scores[index] = score
                    assigned = True
                    break
            if not assigned:
                groups.append([index])
                member_scores[index] = 1.0

        masters, mappings, reviews = [], [], []
        for group in groups:
            items = [prepared[index] for index in group]
            category = items[0]["_category"]
            conflicts = self.rules.detect_conflicts(items)
            decision = "NEW" if len(items) == 1 else ("REVIEW" if conflicts else "AUTO_MERGE")
            confidence = sum(member_scores[index] for index in group) / len(group)
            standard_name = self.rules.generate_standard_name(items, category)
            prefix = self.rules.CATEGORY_PREFIX.get(category, "MDM-X")
            signature = "|".join([category, standard_name, items[0]["_ext"].get("model", "")])
            mdm_code = f"{prefix}-{hashlib.sha1(signature.encode('utf-8')).hexdigest()[:8].upper()}"
            systems = sorted({item.get("system_source", "") for item in items if item.get("system_source")})
            plants = sorted({item.get("plant_code", "GROUP") for item in items})
            source_codes = [item.get("material_code", "") for item in items if item.get("material_code")]
            master = {
                "mdm_code": mdm_code,
                "standard_name": standard_name,
                "category": category,
                **items[0]["_ext"],
                "source_count": len(items),
                "source_systems": systems,
                "plant_codes": plants,
                "decision": decision,
                "confidence": round(confidence, 4),
            }
            masters.append(master)
            for index in group:
                item = prepared[index]
                mappings.append({
                    "system_source": item.get("system_source", ""),
                    "original_code": item.get("material_code", ""),
                    "original_name": item.get("material_name", ""),
                    "plant_code": item.get("plant_code", "GROUP"),
                    "mdm_code": mdm_code,
                    "standard_name": standard_name,
                    "decision": decision,
                    "similarity": round(member_scores[index], 4),
                })
            if decision == "REVIEW":
                reason = "；".join(f"{conflict['field']}冲突：{' / '.join(conflict['values'])}" for conflict in conflicts)
                reviews.append({
                    "mdm_code": mdm_code,
                    "standard_name": standard_name,
                    "reason": reason,
                    "source_records": source_codes,
                    "source_systems": systems,
                    "plant_codes": plants,
                    "confidence": round(confidence, 4),
                    "status": "REVIEW",
                })

        total = len(records)
        golden_count = len(masters)
        compression = round((1 - golden_count / total) * 100, 1) if total else 0.0
        decision_counts = {
            decision: sum(master["decision"] == decision for master in masters)
            for decision in ("AUTO_MERGE", "REVIEW", "NEW")
        }
        return {
            "method": method,
            "warning": warning,
            "threshold": self.THRESHOLD,
            "model": semantic_meta["model"],
            "provider": semantic_meta["provider"],
            "dimension": semantic_meta["dimension"],
            "total_records": total,
            "original_records": total,
            "golden_masters": golden_count,
            "golden_master_count": golden_count,
            "compression_rate": compression,
            "decision_counts": decision_counts,
            "masters": masters,
            "mappings": mappings,
            "reviews": reviews,
        }


semantic_engine = SemanticEngine()
semantic_governor = SemanticDemoGovernor(semantic_engine, engine)
ocr_engine = OCREngine()
ocr_installer = OCRInstallManager()


def _seed_standard_kb(conn: sqlite3.Connection) -> dict:
    """Build the legacy 10-entry namespace only when no legacy index exists."""
    existing = conn.execute(
        "SELECT COUNT(*) FROM vector_embeddings WHERE namespace = 'standard_kb' AND batch_id IS NULL"
    ).fetchone()[0]
    if existing:
        return {"namespace": "standard_kb", "indexed": existing, "provider": "local",
                "model": "feature-hash-v1", "dimension": 384, "idempotent": True}
    now = _utc_now()
    for item in STANDARD_KB:
        content = f"{item['category']} {item['title']} {item['keywords']} {item['code_prefix']}"
        vector = np.asarray(semantic_engine.local_embedding(content), dtype=np.float32)
        metadata = {**item, "source": "SY/T 5497-2018 适配分类知识库", "standard_version": "2018"}
        conn.execute(
            """INSERT INTO vector_embeddings
               (namespace, entity_type, entity_id, batch_id, plant_code, content, content_hash,
                vector, dimension, provider, model, metadata, created_at, updated_at)
               VALUES ('standard_kb', 'STANDARD_CATEGORY', ?, NULL, 'GROUP', ?, ?, ?, ?, 'local',
                       'feature-hash-v1', ?, ?, ?)""",
            (item["reference_id"], content, hashlib.sha256(content.encode("utf-8")).hexdigest(),
             vector.tobytes(), len(vector), json.dumps(metadata, ensure_ascii=False), now, now),
        )
    return {"namespace": "standard_kb", "indexed": len(STANDARD_KB), "provider": "local",
            "model": "feature-hash-v1", "dimension": 384}


def _search_standard_kb(conn: sqlite3.Connection, query: str, top_k: int = 3) -> list[dict]:
    rag = globals().get("enterprise_rag")
    if rag is not None:
        try:
            principal = getattr(g, "principal", None) if has_request_context() else None
            role = (principal or {}).get("role", "GROUP_ADMIN")
            clearance = {
                "GROUP_ADMIN": "RESTRICTED", "AUDITOR": "RESTRICTED",
                "GROUP_APPROVER": "CONFIDENTIAL", "PLANT_STEWARD": "INTERNAL",
            }.get(role, "INTERNAL")
            plant_code = (principal or {}).get("plant_code", "GROUP")
            payload = rag.search(
                query, top_k=top_k, plant_code=plant_code, clearance=clearance,
                trace_id=getattr(g, "trace_id", "") if has_request_context() else "",
                actor=(principal or {}).get("username", "system"),
            )
            return payload["results"]
        except (LookupError, PermissionError, ValueError) as exc:
            logger.warning("enterprise RAG unavailable; using legacy standard index: %s", exc)
    rows = conn.execute(
        """SELECT * FROM vector_embeddings WHERE namespace = 'standard_kb'
           AND batch_id IS NULL AND provider = 'local' AND model = 'feature-hash-v1'"""
    ).fetchall()
    if not rows:
        _seed_standard_kb(conn)
        rows = conn.execute(
            "SELECT * FROM vector_embeddings WHERE namespace = 'standard_kb' AND batch_id IS NULL"
        ).fetchall()
    query_vector = semantic_engine.local_embedding(engine.standardize_text(query))
    results = []
    for row in rows:
        vector = np.frombuffer(row["vector"], dtype=np.float32).tolist()
        metadata = json.loads(row["metadata"] or "{}")
        results.append({
            "reference_id": row["entity_id"], "score": round(semantic_engine.cosine_similarity(query_vector, vector), 6),
            "content": row["content"], **metadata,
        })
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:max(1, min(10, int(top_k)))]


WORKFLOW_DEFINITION = [
    ("INGEST", 1, "多源数据接入", "/api/upload"),
    ("STANDARDIZE", 2, "大模型标准识别", "/api/classify"),
    ("VECTOR_INDEX", 3, "Embedding向量入库", "/api/vectors/rebuild"),
    ("GRAPH_DEDUPE", 4, "图谱辅助查重", "/api/graph"),
    ("REVIEW", 5, "多主体人工审核", "/api/reviews"),
    ("QUALITY", 6, "质量评估与生命周期", "/api/lifecycle"),
    ("DISTRIBUTE", 7, "跨工厂智能分发", "/api/distribute"),
    ("FEEDBACK", 8, "工厂反馈与知识回流", "/api/feedback"),
]


def _set_workflow_step(
    conn: sqlite3.Connection, batch_id: str, step_code: str, status: str,
    progress: int, metrics: dict | None = None,
) -> None:
    definition = next((item for item in WORKFLOW_DEFINITION if item[0] == step_code), None)
    if not definition:
        raise ValueError(f"unknown workflow step: {step_code}")
    code, ordinal, name, endpoint = definition
    normalized_progress = max(0, min(100, int(progress)))
    normalized_metrics = metrics or {}
    previous = conn.execute(
        "SELECT status, progress, metrics FROM workflow_steps WHERE batch_id = ? AND step_code = ?",
        (batch_id, code),
    ).fetchone()
    conn.execute(
        """INSERT INTO workflow_steps
           (batch_id, step_code, ordinal, name, status, progress, metrics, action_endpoint, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(batch_id, step_code) DO UPDATE SET status=excluded.status,
            progress=excluded.progress, metrics=excluded.metrics, updated_at=excluded.updated_at""",
        (batch_id, code, ordinal, name, status, normalized_progress,
         json.dumps(normalized_metrics, ensure_ascii=False), endpoint, _utc_now()),
    )
    previous_metrics = json.loads(previous["metrics"] or "{}") if previous else None
    changed = (
        not previous or previous["status"] != status or int(previous["progress"] or 0) != normalized_progress
        or _canonical_json(previous_metrics) != _canonical_json(normalized_metrics)
    )
    if changed:
        _append_audit_block(
            conn, batch_id, "WORKFLOW_STEP_FINGERPRINT", "WORKFLOW_STEP", code,
            {
                "ordinal": ordinal, "name": name, "status": status, "progress": normalized_progress,
                "metrics": normalized_metrics, "action_endpoint": endpoint,
            }, actor="治理工作流引擎",
        )


def _initialize_workflow(conn: sqlite3.Connection, batch_id: str) -> None:
    for code, _ordinal, _name, _endpoint in WORKFLOW_DEFINITION:
        _set_workflow_step(conn, batch_id, code, "WAITING", 0)


def _workflow_payload(conn: sqlite3.Connection, batch_id: str) -> dict:
    rows = _rows(conn, "SELECT * FROM workflow_steps WHERE batch_id = ? ORDER BY ordinal", (batch_id,))
    for row in rows:
        row["metrics"] = json.loads(row.get("metrics") or "{}")
    completed = sum(row["status"] == "COMPLETED" for row in rows)
    return {
        "batch_id": batch_id,
        "steps": rows,
        "completed_steps": completed,
        "total_steps": len(WORKFLOW_DEFINITION),
        "progress": round(completed / len(WORKFLOW_DEFINITION) * 100, 1),
        "closed_loop": bool(rows) and completed == len(WORKFLOW_DEFINITION),
    }


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _append_audit_block(
    conn: sqlite3.Connection, batch_id: str, event_type: str, entity_type: str,
    entity_id: str, payload: dict, actor: str = "M-AI Agent",
) -> dict:
    previous = conn.execute(
        "SELECT height, block_hash FROM audit_blocks WHERE batch_id = ? ORDER BY height DESC LIMIT 1", (batch_id,)
    ).fetchone()
    height = int(previous["height"]) + 1 if previous else 1
    previous_hash = previous["block_hash"] if previous else "0" * 64
    payload_json = _canonical_json(payload)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    merkle_root = payload_hash
    created_at = _utc_now()
    header = {
        "batch_id": batch_id, "height": height, "event_type": event_type,
        "entity_type": entity_type, "entity_id": entity_id, "actor": actor,
        "payload_hash": payload_hash, "previous_hash": previous_hash,
        "merkle_root": merkle_root, "created_at": created_at,
    }
    block_hash = hashlib.sha256(_canonical_json(header).encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT INTO audit_blocks
           (batch_id, height, event_type, entity_type, entity_id, actor, payload_json,
            payload_hash, previous_hash, merkle_root, block_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, height, event_type, entity_type, entity_id, actor, payload_json,
         payload_hash, previous_hash, merkle_root, block_hash, created_at),
    )
    return {**header, "block_hash": block_hash}


def _verify_audit_chain(conn: sqlite3.Connection, batch_id: str) -> dict:
    blocks = _rows(conn, "SELECT * FROM audit_blocks WHERE batch_id = ? ORDER BY height", (batch_id,))
    expected_previous = "0" * 64
    errors = []
    latest_step_blocks = {}
    for expected_height, block in enumerate(blocks, 1):
        payload_hash = hashlib.sha256((block["payload_json"] or "").encode("utf-8")).hexdigest()
        header = {
            "batch_id": block["batch_id"], "height": block["height"], "event_type": block["event_type"],
            "entity_type": block["entity_type"], "entity_id": block["entity_id"], "actor": block["actor"],
            "payload_hash": block["payload_hash"], "previous_hash": block["previous_hash"],
            "merkle_root": block["merkle_root"], "created_at": block["created_at"],
        }
        calculated_hash = hashlib.sha256(_canonical_json(header).encode("utf-8")).hexdigest()
        if int(block["height"]) != expected_height:
            errors.append({"height": block["height"], "error": "区块高度不连续"})
        if block["previous_hash"] != expected_previous:
            errors.append({"height": block["height"], "error": "前序哈希不匹配"})
        if payload_hash != block["payload_hash"] or block["merkle_root"] != block["payload_hash"]:
            errors.append({"height": block["height"], "error": "业务载荷哈希不匹配"})
        if calculated_hash != block["block_hash"]:
            errors.append({"height": block["height"], "error": "区块哈希不匹配"})
        if block["entity_type"] == "WORKFLOW_STEP" and block["event_type"] == "WORKFLOW_STEP_FINGERPRINT":
            latest_step_blocks[block["entity_id"]] = block
        expected_previous = block["block_hash"]
    workflow_rows = _rows(
        conn, "SELECT * FROM workflow_steps WHERE batch_id = ? ORDER BY ordinal", (batch_id,)
    )
    for step in workflow_rows:
        block = latest_step_blocks.get(step["step_code"])
        if not block:
            errors.append({"step_code": step["step_code"], "error": "当前工作流步骤缺少审计指纹"})
            continue
        expected_payload = {
            "ordinal": step["ordinal"], "name": step["name"], "status": step["status"],
            "progress": int(step["progress"] or 0), "metrics": json.loads(step.get("metrics") or "{}"),
            "action_endpoint": step.get("action_endpoint"),
        }
        try:
            fingerprint_payload = json.loads(block["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            fingerprint_payload = None
        if fingerprint_payload is None or _canonical_json(fingerprint_payload) != _canonical_json(expected_payload):
            errors.append({
                "height": block["height"], "step_code": step["step_code"],
                "error": "工作流当前状态与最新审计指纹不一致",
            })
    return {
        "batch_id": batch_id, "valid": not errors, "block_count": len(blocks),
        "latest_hash": expected_previous if blocks else None, "errors": errors,
        "chain_type": "防篡改SHA-256链式审计账本", "public_chain": False,
    }


def _master_vector_content(master: dict | sqlite3.Row) -> str:
    return " ".join(_clean_value(master[key]) for key in (
        "standard_name", "category", "brand", "model", "dn", "pressure", "material", "source_systems"
    ) if key in master.keys() and _clean_value(master[key]))


def _index_batch_vectors(
    conn: sqlite3.Connection, batch_id: str, preferred_model: str = "local", namespace: str = "golden_master"
) -> dict:
    rows = conn.execute("SELECT * FROM batch_masters WHERE batch_id = ? ORDER BY rowid", (batch_id,)).fetchall()
    if not rows:
        return {"batch_id": batch_id, "indexed": 0, "namespace": namespace, "provider": "local",
                "model": "feature-hash-v1", "dimension": 384, "warning": "当前批次没有黄金主数据。"}
    feedback_rows = {
        row["mdm_code"]: dict(row) for row in conn.execute(
            """SELECT mdm_code, AVG(rating) AS feedback_rating, AVG(accepted) AS acceptance_rate,
                      GROUP_CONCAT(comment, ' ') AS feedback_text, COUNT(*) AS feedback_count
               FROM plant_feedback WHERE batch_id = ? GROUP BY mdm_code""", (batch_id,)
        ).fetchall()
    }
    contents = []
    for row in rows:
        feedback_item = feedback_rows.get(row["mdm_code"], {})
        feedback_text = _clean_value(feedback_item.get("feedback_text"))
        content = _master_vector_content(row)
        contents.append(f"{content} 工厂反馈 {feedback_text}".strip() if feedback_text else content)
    vectors, metadata = semantic_engine.resolve_vectorization(contents, preferred_model, "document")
    now = _utc_now()
    for row, content, vector in zip(rows, contents, vectors):
        vector_array = np.asarray(vector, dtype=np.float32)
        item_metadata = {
            "standard_name": row["standard_name"], "category": row["category"],
            "brand": row["brand"], "model": row["model"], "dn": row["dn"],
            "pressure": row["pressure"], "material": row["material"],
            "decision": row["decision"], "plant_codes": row["plant_codes"],
            "feedback_count": int(feedback_rows.get(row["mdm_code"], {}).get("feedback_count") or 0),
            "feedback_rating": round(float(feedback_rows.get(row["mdm_code"], {}).get("feedback_rating") or 0), 2),
            "acceptance_rate": round(float(feedback_rows.get(row["mdm_code"], {}).get("acceptance_rate") or 0), 4),
        }
        conn.execute(
            """INSERT INTO vector_embeddings
               (namespace, entity_type, entity_id, batch_id, plant_code, content, content_hash,
                vector, dimension, provider, model, metadata, created_at, updated_at)
               VALUES (?, 'MASTER', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(namespace, batch_id, entity_type, entity_id, provider, model) DO UPDATE SET
                plant_code=excluded.plant_code, content=excluded.content, content_hash=excluded.content_hash,
                vector=excluded.vector, dimension=excluded.dimension, metadata=excluded.metadata,
                updated_at=excluded.updated_at""",
            (namespace, row["mdm_code"], batch_id, row["plant_codes"] or "GROUP", content,
             hashlib.sha256(content.encode("utf-8")).hexdigest(), vector_array.tobytes(), len(vector_array),
             metadata["provider"], metadata["model"], json.dumps(item_metadata, ensure_ascii=False), now, now),
        )
    return {"batch_id": batch_id, "namespace": namespace, "indexed": len(rows), **metadata}


def _search_vectors(
    conn: sqlite3.Connection, query: str, batch_id: str, preferred_model: str,
    top_k: int, plant_code: str = "GROUP", namespace: str = "golden_master",
) -> dict:
    requested = preferred_model if preferred_model in {*SemanticEngine.MODELS, "local"} else "local"
    provider = requested
    model_name = SemanticEngine.MODELS[requested]["model"] if requested in SemanticEngine.MODELS else "feature-hash-v1"
    count = conn.execute(
        """SELECT COUNT(*) FROM vector_embeddings
           WHERE batch_id = ? AND namespace = ? AND provider = ? AND model = ?""",
        (batch_id, namespace, provider, model_name),
    ).fetchone()[0]
    index_meta = None
    if not count:
        index_meta = _index_batch_vectors(conn, batch_id, requested, namespace)
        provider, model_name = index_meta["provider"], index_meta["model"]
    if provider == "local":
        query_vector = np.asarray(semantic_engine.local_embedding(query), dtype=np.float32)
    else:
        vectors = semantic_engine.get_embeddings([query], model_key=provider, text_type="query")
        if vectors is None:
            index_meta = _index_batch_vectors(conn, batch_id, "local", namespace)
            provider, model_name = "local", "feature-hash-v1"
            query_vector = np.asarray(semantic_engine.local_embedding(query), dtype=np.float32)
        else:
            query_vector = np.asarray(vectors[0], dtype=np.float32)
    rows = conn.execute(
        """SELECT * FROM vector_embeddings
           WHERE batch_id = ? AND namespace = ? AND provider = ? AND model = ?""",
        (batch_id, namespace, provider, model_name),
    ).fetchall()
    results = []
    for row in rows:
        if plant_code != "GROUP" and not _plant_visible(row["plant_code"], plant_code):
            continue
        vector = np.frombuffer(row["vector"], dtype=np.float32)
        score = SemanticEngine.cosine_similarity(query_vector.tolist(), vector.tolist())
        metadata = json.loads(row["metadata"] or "{}")
        results.append({
            "entity_type": row["entity_type"], "entity_id": row["entity_id"], "content": row["content"],
            "score": round(score, 6), "metadata": metadata,
        })
    results.sort(key=lambda item: item["score"], reverse=True)
    warning = (index_meta or {}).get("warning")
    return {
        "query": query, "batch_id": batch_id, "namespace": namespace, "provider": provider,
        "model": model_name, "dimension": len(query_vector), "warning": warning,
        "results": results[:top_k], "total": len(results), "top_k": top_k,
    }


class QwenGovernanceAgent:
    API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    def __init__(self):
        self.api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self.model = os.getenv("QWEN_CHAT_MODEL", "qwen-plus").strip() or "qwen-plus"
        self._explanation_cache: dict[str, str] = {}

    @staticmethod
    def fallback_plan(task: str, context: dict) -> dict:
        return {
            "objective": task,
            "summary": "采用规则、向量检索、知识图谱和人工审核协同完成治理，并以工厂反馈回流闭环。",
            "steps": [
                {"step": ordinal, "code": code, "name": name, "action": endpoint}
                for code, ordinal, name, endpoint in WORKFLOW_DEFINITION
            ],
            "risk_controls": ["关键属性冲突强制人工复核", "按工厂主体控制数据可见与分发范围", "全流程哈希链存证"],
            "context": context,
        }

    def plan(self, task: str, context: dict) -> tuple[dict, dict]:
        fallback = self.fallback_plan(task, context)
        if has_request_context() and not getattr(g, "external_ai_allowed", True):
            return fallback, {
                "active": False, "model": self.model, "method": "确定性Agent编排（数据不出域）",
                "warning": f"{getattr(g, 'data_classification', 'RESTRICTED')} 数据禁止发送至外部大模型。",
            }
        if not self.api_key:
            return fallback, {"active": False, "model": self.model, "method": "确定性Agent编排（降级）",
                              "warning": "DASHSCOPE_API_KEY未配置，已使用本地可解释编排。"}
        system_prompt = (
            "你是大型制造集团主数据治理Agent。只返回JSON对象，字段必须包含objective、summary、steps、"
            "risk_controls。steps需要覆盖接入、标准识别、向量入库、图谱查重、审核、质量、分发、反馈回流，"
            "不得绕过人工审核和工厂数据边界。"
        )
        try:
            response = requests.post(
                self.API_URL,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _canonical_json({"task": task, "context": context})},
                ], "temperature": 0.1}, timeout=(3.05, 20),
            )
            if response.status_code != 200:
                raise requests.RequestException(f"HTTP {response.status_code}")
            content = response.json()["choices"][0]["message"]["content"].strip()
            match = re.search(r"\{.*\}", content, re.S)
            plan = json.loads(match.group(0) if match else content)
            if not isinstance(plan.get("steps"), list) or not plan["steps"]:
                raise ValueError("LLM response does not contain steps")
            return plan, {"active": True, "model": self.model, "method": "通义千问大模型Agent", "warning": None}
        except (requests.RequestException, requests.Timeout, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Qwen agent unavailable: %s", exc)
            return fallback, {"active": False, "model": self.model, "method": "确定性Agent编排（降级）",
                              "warning": f"通义千问调用失败，已自动降级：{exc}"}

    @staticmethod
    def fallback_explanation(evidence: dict) -> str:
        master = evidence["master"]
        attributes = evidence["attributes"]
        source_count = int(master.get("source_count") or 0)
        parts = [
            f"系统将 {source_count} 条来源记录归入黄金编码 {master['mdm_code']}。",
            f"治理决策为{evidence['decision_label']}，置信度 {float(master.get('confidence') or 0):.2f}。",
        ]
        matched = [f"{label}一致" for key, label in (("brand", "品牌"), ("model", "型号"), ("dn", "口径"), ("pressure", "压力")) if attributes.get(key)]
        if matched:
            parts.append("主要依据：" + "、".join(matched) + "。")
        if evidence["conflicts"]:
            conflict_text = "；".join(f"{item['field']}存在冲突（{' / '.join(item['values'])}）" for item in evidence["conflicts"])
            parts.append(f"风险提示：{conflict_text}，因此需要人工复核。")
        elif source_count > 1:
            parts.append("关键属性未发现硬冲突，可按当前规则建议归并。")
        else:
            parts.append("未检索到可安全归并的同物记录，建议作为新增候选。")
        return "".join(parts)

    def explain(self, evidence: dict, use_llm: bool = True) -> tuple[str, dict]:
        fallback = self.fallback_explanation(evidence)
        evidence_hash = hashlib.sha256(_canonical_json(evidence).encode("utf-8")).hexdigest()
        if has_request_context() and not getattr(g, "external_ai_allowed", True):
            return fallback, {
                "active": False, "model": self.model, "method": "事实模板解释（数据不出域）",
                "warning": f"{getattr(g, 'data_classification', 'RESTRICTED')} 数据禁止发送至外部大模型。",
                "cached": False, "evidence_hash": evidence_hash,
            }
        if not use_llm or not self.api_key:
            return fallback, {
                "active": False, "model": self.model, "method": "事实模板解释（降级）",
                "warning": None if not use_llm else "DASHSCOPE_API_KEY未配置，已使用事实模板。",
                "cached": False, "evidence_hash": evidence_hash,
            }
        if evidence_hash in self._explanation_cache:
            return self._explanation_cache[evidence_hash], {
                "active": True, "model": self.model, "method": "通义千问事实约束解释",
                "warning": None, "cached": True, "evidence_hash": evidence_hash,
            }
        system_prompt = (
            "你是制造集团主数据治理解释Agent。仅依据输入JSON中的事实，用不超过180字的中文说明归并或新增原因、"
            "关键一致项、冲突项和是否需要人工审核。不得编造相似度、字段或标准条款，不得改变系统决策。"
        )
        try:
            response = requests.post(
                self.API_URL,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _canonical_json(evidence)},
                ], "temperature": 0.1}, timeout=(3.05, 20),
            )
            if response.status_code != 200:
                raise requests.RequestException(f"HTTP {response.status_code}")
            content = _clean_value(response.json()["choices"][0]["message"]["content"])
            if not content:
                raise ValueError("empty explanation")
            self._explanation_cache[evidence_hash] = content
            return content, {
                "active": True, "model": self.model, "method": "通义千问事实约束解释",
                "warning": None, "cached": False, "evidence_hash": evidence_hash,
            }
        except (requests.RequestException, requests.Timeout, KeyError, ValueError, TypeError) as exc:
            logger.warning("Qwen explanation unavailable: %s", exc)
            return fallback, {
                "active": False, "model": self.model, "method": "事实模板解释（降级）",
                "warning": f"通义千问解释调用失败，已自动降级：{exc}",
                "cached": False, "evidence_hash": evidence_hash,
            }


qwen_agent = QwenGovernanceAgent()


def _build_governance_graph(
    conn: sqlite3.Connection, batch_id: str, plant_code: str = "GROUP", raw_limit: int = 80
) -> tuple[nx.Graph, dict]:
    graph = nx.Graph(batch_id=batch_id)
    batch = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    batch_id_node = f"batch:{batch_id}"
    graph.add_node(batch_id_node, label=(batch["filename"] if batch else batch_id), node_type="BATCH",
                   entity_id=batch_id)
    workflow_rows = _rows(
        conn, "SELECT * FROM workflow_steps WHERE batch_id = ? ORDER BY ordinal", (batch_id,)
    )
    previous_step_id = None
    for step in workflow_rows:
        step_id = f"workflow:{step['step_code']}"
        graph.add_node(
            step_id, label=f"{step['ordinal']}. {step['name']}", node_type="WORKFLOW_STEP",
            entity_id=step["step_code"], status=step["status"], progress=int(step["progress"] or 0),
            action_endpoint=step.get("action_endpoint"), fingerprint_count=0,
            latest_block_hash="", latest_payload_hash="", latest_fingerprint_height=0,
        )
        graph.add_edge(batch_id_node, step_id, relation="CONTAINS_STEP")
        if previous_step_id:
            graph.add_edge(previous_step_id, step_id, relation="NEXT_STEP")
        previous_step_id = step_id
    masters = conn.execute("SELECT * FROM batch_masters WHERE batch_id = ? ORDER BY rowid", (batch_id,)).fetchall()
    visible_codes = set()
    for master in masters:
        if plant_code != "GROUP" and not _plant_visible(master["plant_codes"], plant_code):
            continue
        master_id = f"master:{master['mdm_code']}"
        visible_codes.add(master["mdm_code"])
        graph.add_node(master_id, label=master["standard_name"] or master["mdm_code"], node_type="MASTER",
                       entity_id=master["mdm_code"], decision=master["decision"])
        if master["category"]:
            category_id = f"category:{master['category']}"
            graph.add_node(category_id, label=master["category"], node_type="CATEGORY", entity_id=master["category"])
            graph.add_edge(master_id, category_id, relation="CLASSIFIED_AS")
        for plant in filter(None, _clean_value(master["plant_codes"]).split(",")):
            plant = _normalize_plant_code(plant)
            if plant_code != "GROUP" and plant not in {"GROUP", plant_code}:
                continue
            plant_id = f"plant:{plant}"
            graph.add_node(plant_id, label=PLANTS.get(plant, plant), node_type="PLANT", entity_id=plant)
            graph.add_edge(master_id, plant_id, relation="AVAILABLE_TO")
    record_rows = _rows(conn, "SELECT * FROM records WHERE batch_id = ? ORDER BY id", (batch_id,))
    record_by_id = {row["id"]: row for row in record_rows}
    legacy_record_lookup = {}
    for row in record_rows:
        key = (_clean_value(row.get("system_source")), _clean_value(row.get("material_code")),
               _normalize_plant_code(row.get("plant_code")), _clean_value(row.get("material_name")))
        legacy_record_lookup.setdefault(key, []).append(row)
    mappings = _rows(
        conn, "SELECT * FROM mappings WHERE batch_id = ? ORDER BY id LIMIT ?", (batch_id, max(0, raw_limit))
    )
    for mapping in mappings:
        if mapping["mdm_code"] not in visible_codes:
            continue
        if plant_code != "GROUP" and not _plant_visible(mapping.get("plant_code"), plant_code):
            continue
        record = record_by_id.get(mapping.get("record_id"))
        if not record:
            key = (_clean_value(mapping.get("system_source")), _clean_value(mapping.get("original_code")),
                   _normalize_plant_code(mapping.get("plant_code")), _clean_value(mapping.get("original_name")))
            candidates = legacy_record_lookup.get(key) or []
            record = candidates[0] if candidates else None
        attributes = json.loads((record or {}).get("ext") or "{}")
        provenance = attributes.get("_provenance") if isinstance(attributes.get("_provenance"), dict) else {}
        if record and not provenance:
            provenance, fallback_issues = _build_source_provenance(
                record, attributes, batch_id, batch["filename"] if batch else "历史批次", int(record.get("id") or 0)
            )
            attributes["_quality_issues"] = fallback_issues
        record_db_id = (record or {}).get("id")
        raw_id = f"record:{record_db_id}" if record_db_id else f"raw:{mapping['id']}"
        graph.add_node(
            raw_id, label=mapping["original_name"] or mapping["original_code"] or raw_id,
            node_type="RAW", entity_id=provenance.get("source_record_id") or mapping["original_code"],
            record_id=record_db_id, material_code=mapping["original_code"],
            source_system=mapping["system_source"], plant_code=mapping["plant_code"],
            source_table=provenance.get("source_table", ""), source_url=provenance.get("source_url", ""),
            connector_status=provenance.get("connector_status", "DEMO"),
            record_hash=provenance.get("record_hash", ""),
        )
        graph.add_edge(batch_id_node, raw_id, relation="INGESTED_RECORD")
        source = mapping.get("system_source") or "CSV导入"
        source_id = f"system:{source}"
        profile = _connector_profile(source)
        graph.add_node(source_id, label=source, node_type="SYSTEM", entity_id=source,
                       connector_id=profile["id"], connector_name=profile["label"],
                       connector_status="CONFIGURED" if profile["base_url"] else "DEMO")
        graph.add_edge(source_id, raw_id, relation="PROVIDES_RECORD")
        source_plant = _normalize_plant_code(mapping.get("plant_code"))
        source_plant_id = f"plant:{source_plant}"
        graph.add_node(source_plant_id, label=PLANTS.get(source_plant, source_plant), node_type="PLANT",
                       entity_id=source_plant)
        graph.add_edge(raw_id, source_plant_id, relation="OWNED_BY")
        graph.add_edge(raw_id, f"master:{mapping['mdm_code']}", relation="MAPPED_TO",
                       similarity=round(float(mapping["similarity"] or 0), 4))
        issues = list(attributes.get("_quality_issues") or [])
        if mapping.get("decision") == "REVIEW" and not any(item.get("code") == "REVIEW_REQUIRED" for item in issues):
            issues.append({"code": "REVIEW_REQUIRED", "field": "semantic_match", "severity": "HIGH",
                           "message": "存在冲突或低置信度匹配，需人工审核"})
        for issue_index, issue in enumerate(issues):
            issue_id = f"issue:{mapping['id']}:{issue.get('code') or issue_index}"
            graph.add_node(issue_id, label=issue.get("message") or issue.get("code") or "数据质量问题",
                           node_type="ISSUE", entity_id=issue.get("code") or str(issue_index),
                           field=issue.get("field"), severity=issue.get("severity", "MEDIUM"))
            graph.add_edge(raw_id, issue_id, relation="HAS_QUALITY_ISSUE")

    reviews = _rows(conn, "SELECT * FROM reviews WHERE batch_id = ? AND status = 'REVIEW' ORDER BY id", (batch_id,))
    if plant_code != "GROUP":
        reviews = [item for item in reviews if _plant_visible(item.get("plant_codes"), plant_code)]
    for review in reviews:
        master_id = f"master:{review['mdm_code']}"
        if master_id not in graph:
            continue
        review_id = f"review:{review['id']}"
        graph.add_node(review_id, label=review.get("reason") or "人工审核", node_type="REVIEW",
                       entity_id=str(review["id"]), status=review.get("status"), confidence=review.get("confidence"))
        graph.add_edge(master_id, review_id, relation="REQUIRES_REVIEW")

    if plant_code == "GROUP":
        distributions = _rows(conn, "SELECT * FROM distribution_logs WHERE batch_id = ? ORDER BY id", (batch_id,))
    else:
        distributions = _rows(
            conn, "SELECT * FROM distribution_logs WHERE batch_id = ? AND plant_code = ? ORDER BY id",
            (batch_id, plant_code),
        )
    for item in distributions:
        master_id = f"master:{item['mdm_code']}"
        if master_id not in graph:
            continue
        target_id = f"target:{item['target_system']}:{item['plant_code']}"
        graph.add_node(target_id, label=f"{item['target_system']} · {PLANTS.get(item['plant_code'], item['plant_code'])}",
                       node_type="TARGET_SYSTEM", entity_id=item["target_system"], status=item.get("status"),
                       plant_code=item.get("plant_code"))
        graph.add_edge(master_id, target_id, relation="DISTRIBUTED_TO", status=item.get("status"))
        plant_id = f"plant:{item['plant_code']}"
        graph.add_node(plant_id, label=PLANTS.get(item["plant_code"], item["plant_code"]), node_type="PLANT",
                       entity_id=item["plant_code"])
        graph.add_edge(target_id, plant_id, relation="DELIVERED_TO")

    if plant_code == "GROUP":
        feedback_rows = _rows(conn, "SELECT * FROM plant_feedback WHERE batch_id = ? ORDER BY id", (batch_id,))
    else:
        feedback_rows = _rows(
            conn, "SELECT * FROM plant_feedback WHERE batch_id = ? AND plant_code = ? ORDER BY id",
            (batch_id, plant_code),
        )
    for item in feedback_rows:
        master_id = f"master:{item['mdm_code']}"
        if master_id not in graph:
            continue
        feedback_id = f"feedback:{item['id']}"
        graph.add_node(feedback_id, label=f"{PLANTS.get(item['plant_code'], item['plant_code'])}反馈",
                       node_type="FEEDBACK", entity_id=str(item["id"]), accepted=bool(item.get("accepted")),
                       rating=item.get("rating"), comment=item.get("comment"))
        graph.add_edge(master_id, feedback_id, relation="VALIDATED_BY")
        plant_id = f"plant:{item['plant_code']}"
        graph.add_node(plant_id, label=PLANTS.get(item["plant_code"], item["plant_code"]), node_type="PLANT",
                       entity_id=item["plant_code"])
        graph.add_edge(feedback_id, plant_id, relation="SUBMITTED_BY")

    audit_verification = _verify_audit_chain(conn, batch_id)
    audit_rows = _rows(conn, "SELECT * FROM audit_blocks WHERE batch_id = ? ORDER BY height", (batch_id,))
    previous_audit_id = None
    for block in audit_rows:
        audit_id = f"audit:{block['height']}"
        graph.add_node(audit_id, label=f"#{block['height']} {block['event_type']}", node_type="AUDIT",
                       entity_id=str(block["height"]), block_hash=block.get("block_hash"),
                       payload_hash=block.get("payload_hash"), previous_hash=block.get("previous_hash"),
                       merkle_root=block.get("merkle_root"), created_at=block.get("created_at"),
                       verified=bool(audit_verification["valid"]))
        if previous_audit_id:
            graph.add_edge(previous_audit_id, audit_id, relation="PREVIOUS_HASH")
        previous_audit_id = audit_id
        entity_type = _clean_value(block.get("entity_type")).upper()
        entity_id = _clean_value(block.get("entity_id"))
        if entity_type == "BATCH":
            entity_node = batch_id_node
        elif entity_type == "MASTER":
            entity_node = f"master:{entity_id}"
        elif entity_type == "DISTRIBUTION" and entity_id in PLANTS:
            if plant_code != "GROUP" and entity_id != plant_code:
                continue
            entity_node = f"plant:{entity_id}"
        elif entity_type == "WORKFLOW_STEP":
            entity_node = f"workflow:{entity_id}"
        else:
            entity_node = f"entity:{entity_type}:{entity_id}"
            if entity_node not in graph:
                graph.add_node(entity_node, label=f"{entity_type} · {entity_id}", node_type="ENTITY", entity_id=entity_id)
        if entity_node in graph:
            graph.add_edge(audit_id, entity_node, relation="FINGERPRINTS")
            if entity_type == "WORKFLOW_STEP":
                step_node = graph.nodes[entity_node]
                step_node["fingerprint_count"] = int(step_node.get("fingerprint_count") or 0) + 1
                step_node["latest_block_hash"] = block.get("block_hash") or ""
                step_node["latest_payload_hash"] = block.get("payload_hash") or ""
                step_node["latest_fingerprint_height"] = int(block.get("height") or 0)
                step_node["fingerprint_created_at"] = block.get("created_at") or ""
                step_node["chain_valid"] = bool(audit_verification["valid"])
    stats = {
        "nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
        "connected_components": nx.number_connected_components(graph) if graph.number_of_nodes() else 0,
        "types": {},
        "audit": audit_verification,
    }
    for _node, data in graph.nodes(data=True):
        stats["types"][data["node_type"]] = stats["types"].get(data["node_type"], 0) + 1
    return graph, stats


def _quality_metrics(records: list[dict], anomaly_count: int = 0) -> dict:
    if not records:
        return {
            "completeness": 0, "accuracy": 0, "consistency": 0,
            "uniqueness": 0, "standardization": 0, "score": 0, "recordCount": 0,
        }
    fields = ("material_code", "system_source", "material_name", "description", "category", "unit")
    completeness = sum(bool(record.get(field)) for record in records for field in fields) / (len(records) * len(fields)) * 100
    code_values = [record.get("material_code", "") for record in records if record.get("material_code")]
    uniqueness = len(set(code_values)) / max(1, len(code_values)) * 100
    standardization = sum(engine.detect_category(record.get("material_name", ""), record.get("category", "")) != "其他" for record in records) / len(records) * 100
    accuracy = max(0.0, 100 - anomaly_count / len(records) * 100)
    consistency = sum(bool(record.get("material_name")) and bool(record.get("category")) for record in records) / len(records) * 100
    metrics = {
        "completeness": round(completeness, 1), "accuracy": round(accuracy, 1),
        "consistency": round(consistency, 1), "uniqueness": round(uniqueness, 1),
        "standardization": round(standardization, 1),
    }
    metrics["score"] = round(sum(metrics.values()) / len(metrics), 1)
    metrics["recordCount"] = len(records)
    return metrics


def analyze_quality(records: list[dict], anomaly_count: int = 0) -> dict:
    if not records:
        return {"overall": _quality_metrics([]), "systems": {}, "issues": [], "suggestions": []}
    metrics = _quality_metrics(records, anomaly_count)
    completeness = metrics["completeness"]
    systems = {}
    for system in sorted({record.get("system_source") or "未分类" for record in records}):
        subset = [record for record in records if (record.get("system_source") or "未分类") == system]
        systems[system] = _quality_metrics(subset)
    issues = []
    if completeness < 90:
        issues.append({"level": "high", "system": "全部系统", "text": f"关键字段完整率为 {completeness:.1f}%", "action": "补齐物料编码、名称、描述、分类和计量单位。"})
    if anomaly_count:
        issues.append({"level": "mid", "system": "全部系统", "text": f"Isolation Forest 识别 {anomaly_count} 条结构异常记录", "action": "在人工审核中复核异常记录。"})
    return {"overall": metrics, "systems": systems, "issues": issues, "suggestions": list(dict.fromkeys(item["action"] for item in issues))}


def _batch_id() -> str:
    return f"BATCH-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"


def persist_batch(filename: str, encoding: str, records: list[dict], preferred_model: str = "qwen") -> dict:
    batch_id = _batch_id()
    created_at = _utc_now()
    classification = getattr(g, "data_classification", "INTERNAL") if has_request_context() else "INTERNAL"
    policy = EnterpriseSecurity.DATA_CLASSIFICATIONS.get(
        classification, EnterpriseSecurity.DATA_CLASSIFICATIONS["INTERNAL"]
    )
    retention_until = (datetime.now(timezone.utc) + timedelta(
        days=int(policy["default_retention_days"])
    )).isoformat(timespec="seconds")
    data_owner = getattr(g, "request_data_owner", "M-AI Master") if has_request_context() else "M-AI Master"
    processing_purpose = (
        getattr(g, "request_processing_purpose", "物料主数据治理")
        if has_request_context() else "物料主数据治理"
    )
    masters, reviews, mappings, semantic_meta = engine.govern(records, semantic_engine, preferred_model)
    plant_codes = sorted({_normalize_plant_code(record.get("plant_code")) for record in records})
    batch_plant = plant_codes[0] if len(plant_codes) == 1 else "MULTI"
    mapping_meta = {}
    for item in mappings:
        key = (
            _clean_value(item.get("system_source")), _clean_value(item.get("original_code")),
            _normalize_plant_code(item.get("plant_code")), _clean_value(item.get("original_name")),
        )
        mapping_meta.setdefault(key, []).append(item)
    record_attributes = []
    source_issues = []
    for index, record in enumerate(records, start=1):
        attributes = dict(engine.enrich(record)["_ext"])
        provenance, issues = _build_source_provenance(record, attributes, batch_id, filename or "uploaded.csv", index)
        key = (
            _clean_value(record.get("system_source")), _clean_value(record.get("material_code")),
            _normalize_plant_code(record.get("plant_code")), _clean_value(record.get("material_name")),
        )
        decision_rows = mapping_meta.get(key) or []
        if decision_rows:
            decision = decision_rows[0]
            if decision.get("decision") == "REVIEW":
                issues.append({"code": "REVIEW_REQUIRED", "field": "semantic_match", "label": "语义匹配",
                               "severity": "HIGH", "message": "存在冲突或低置信度匹配，需人工审核"})
            similarity = float(decision.get("similarity") or 0)
            if similarity < engine.SIMILARITY_THRESHOLD:
                issues.append({"code": "LOW_SIMILARITY", "field": "semantic_match", "label": "语义相似度",
                               "severity": "MEDIUM", "message": f"相似度 {similarity:.2%} 低于治理阈值"})
        attributes["_provenance"] = provenance
        attributes["_quality_issues"] = issues
        record_attributes.append(attributes)
        for issue in issues:
            source_issues.append({
                **issue, "material_code": record.get("material_code"), "material_name": record.get("material_name"),
                "system_source": record.get("system_source"), "plant_code": record.get("plant_code"),
                "source_record_id": provenance["source_record_id"], "source_table": provenance["source_table"],
                "connector_status": provenance["connector_status"], "source_url": provenance["source_url"],
                "record_hash": provenance["record_hash"],
            })
    anomaly_count = sum(master.get("anomaly_count", 0) for master in masters)
    quality_report = analyze_quality(records, anomaly_count)
    source_hashes = sorted(item["_provenance"]["record_hash"] for item in record_attributes)
    source_fingerprint_root = hashlib.sha256("".join(source_hashes).encode("ascii")).hexdigest()
    quality_report["source_issues"] = source_issues
    quality_report["lineage"] = {
        "traceable_records": len(record_attributes),
        "problem_records": len({(item["system_source"], item["source_record_id"]) for item in source_issues}),
        "source_systems": sorted({record.get("system_source") or "CSV导入" for record in records}),
        "source_fingerprint_root": source_fingerprint_root,
    }
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO batches
               (batch_id, filename, created_at, encoding, record_count, plant_code,
                semantic_method, semantic_model, semantic_dimension, semantic_warning,
                data_classification, data_owner, processing_purpose, retention_until, legal_hold)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (batch_id, filename or "uploaded.csv", created_at, encoding or "utf-8", len(records), batch_plant,
             semantic_meta["method"], semantic_meta["model"], semantic_meta["dimension"], semantic_meta["warning"],
             classification, data_owner, processing_purpose, retention_until),
        )
        record_ids = []
        record_lookup = {}
        for record, attributes in zip(records, record_attributes):
            cursor = conn.execute(
                """INSERT INTO records
                   (batch_id, material_code, system_source, material_name, description, category, unit, create_time, plant_code, ext)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, record["material_code"], record["system_source"], record["material_name"], record["description"],
                 record["category"], record["unit"], record["create_time"], record["plant_code"],
                 json.dumps(attributes, ensure_ascii=False)),
            )
            record_id = cursor.lastrowid
            record_ids.append(record_id)
            key = (
                _clean_value(record.get("system_source")), _clean_value(record.get("material_code")),
                _normalize_plant_code(record.get("plant_code")), _clean_value(record.get("material_name")),
            )
            record_lookup.setdefault(key, []).append(record_id)
        for master in masters:
            conn.execute(
                """INSERT INTO masters
                   (mdm_code, standard_name, category, model, brand, dn, pressure, material, source_count,
                    source_systems, decision, confidence, code_prefix, anomaly_count, plant_codes, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mdm_code) DO UPDATE SET
                    standard_name=excluded.standard_name, category=excluded.category, model=excluded.model,
                    brand=excluded.brand, dn=excluded.dn, pressure=excluded.pressure, material=excluded.material,
                    source_count=excluded.source_count, source_systems=excluded.source_systems,
                    decision=excluded.decision, confidence=excluded.confidence, code_prefix=excluded.code_prefix,
                    anomaly_count=excluded.anomaly_count, plant_codes=excluded.plant_codes, updated_at=excluded.updated_at""",
                (master["mdm_code"], master["standard_name"], master["category"], master["model"], master["brand"],
                 master["dn"], master["pressure"], master["material"], master["source_count"], master["source_systems"],
                 master["decision"], master["confidence"], master["code_prefix"], master["anomaly_count"],
                 master["plant_codes"], created_at),
            )
            conn.execute(
                """INSERT INTO batch_masters
                   (batch_id, mdm_code, standard_name, category, model, brand, dn, pressure, material,
                    source_count, source_systems, decision, confidence, code_prefix, anomaly_count, source_records, plant_codes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, master["mdm_code"], master["standard_name"], master["category"], master["model"],
                 master["brand"], master["dn"], master["pressure"], master["material"], master["source_count"],
                 master["source_systems"], master["decision"], master["confidence"], master["code_prefix"],
                 master["anomaly_count"], master["source_records"], master["plant_codes"]),
            )
        used_record_ids = set()
        for item in mappings:
            key = (
                _clean_value(item.get("system_source")), _clean_value(item.get("original_code")),
                _normalize_plant_code(item.get("plant_code")), _clean_value(item.get("original_name")),
            )
            candidates = record_lookup.get(key) or []
            record_id = candidates.pop(0) if candidates else next(
                (candidate for candidate in record_ids if candidate not in used_record_ids), None
            )
            if record_id is not None:
                used_record_ids.add(record_id)
            conn.execute(
                """INSERT INTO mappings
                   (batch_id, record_id, system_source, original_code, original_name, mdm_code, standard_name, decision,
                    similarity, applied_rules, plant_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, record_id, item["system_source"], item["original_code"], item["original_name"], item["mdm_code"],
                 item["standard_name"], item["decision"], item["similarity"], item["applied_rules"], item["plant_code"]),
            )
        for review in reviews:
            cursor = conn.execute(
                """INSERT INTO reviews
                   (batch_id, mdm_code, standard_name, decision, reason, applied_rules, source_records,
                    source_systems, confidence, category, attributes, candidates, plant_codes, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, review["mdm_code"], review["standard_name"], review["decision"], review["reason"],
                 review["applied_rules"], review["source_records"], review["source_systems"], review["confidence"],
                 review["category"], json.dumps(review["_ext"], ensure_ascii=False),
                 json.dumps(review["candidates"], ensure_ascii=False), review["plant_codes"], review["status"]),
            )
            review["id"] = cursor.lastrowid
        governance_issue_meta = enterprise_governance.capture_batch_issues(conn, batch_id)
        quality_report["issue_workflow"] = governance_issue_meta
        quality_report["measurement"] = {
            "framework": "DAMA-DMBOK2 Rev 适配质量度量",
            "measured_at": created_at,
            "sample_size": len(records),
            "accuracy_is_proxy": True,
            "accuracy_note": "accuracy 为结构异常代理指标，不等同于人工真值集准确率。",
        }
        conn.execute(
            "INSERT INTO quality_reports (batch_id, report, generated_at) VALUES (?, ?, ?)",
            (batch_id, json.dumps(quality_report, ensure_ascii=False), created_at),
        )
        _initialize_workflow(conn, batch_id)
        _set_workflow_step(conn, batch_id, "INGEST", "COMPLETED", 100, {
            "records": len(records), "systems": len({item.get("system_source") for item in records if item.get("system_source")}),
            "plants": plant_codes,
        })
        _set_workflow_step(conn, batch_id, "STANDARDIZE", "COMPLETED", 100, {
            "method": semantic_meta["method"], "model": semantic_meta["model"],
            "embedding_active": semantic_meta.get("embedding_active", False),
        })
        vector_meta = _index_batch_vectors(conn, batch_id, "local")
        _set_workflow_step(conn, batch_id, "VECTOR_INDEX", "COMPLETED", 100, vector_meta)
        graph, graph_meta = _build_governance_graph(conn, batch_id)
        _set_workflow_step(conn, batch_id, "GRAPH_DEDUPE", "COMPLETED", 100, {
            **graph_meta, "golden_masters": len(masters), "source_records": len(mappings),
        })
        _set_workflow_step(
            conn, batch_id, "REVIEW", "ACTION_REQUIRED" if reviews else "COMPLETED",
            0 if reviews else 100, {"pending": len(reviews)},
        )
        _set_workflow_step(conn, batch_id, "QUALITY", "COMPLETED", 100, {
            "score": quality_report.get("overall", {}).get("score", 0), "anomalies": anomaly_count,
        })
        _set_workflow_step(conn, batch_id, "DISTRIBUTE", "READY", 0, {"approved_masters": len(masters) - len(reviews)})
        _set_workflow_step(conn, batch_id, "FEEDBACK", "WAITING", 0, {"feedback_count": 0})
        _append_audit_block(conn, batch_id, "BATCH_GOVERNED", "BATCH", batch_id, {
            "filename": filename, "record_count": len(records), "golden_master_count": len(masters),
            "review_count": len(reviews), "quality_score": quality_report.get("overall", {}).get("score", 0),
            "vector_index": vector_meta, "graph": graph_meta,
            "source_fingerprint_root": source_fingerprint_root, "source_record_count": len(source_hashes),
        })
    return get_batch_state(batch_id)


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _latest_batch_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT batch_id FROM batches ORDER BY id DESC LIMIT 1").fetchone()
    return row["batch_id"] if row else None


def _plant_visible(value, requested_plant: str | None) -> bool:
    if not requested_plant or requested_plant in {"GROUP", "ALL"}:
        return True
    plants = {_normalize_plant_code(item) for item in _clean_value(value).split(",") if item}
    return requested_plant in plants


def _master_distributable_to_plant(value, requested_plant: str) -> bool:
    """Group golden data is reusable by sites; site-owned data remains site-scoped."""
    if requested_plant == "GROUP":
        return True
    plants = {_normalize_plant_code(item) for item in _clean_value(value).split(",") if item}
    return "GROUP" in plants or requested_plant in plants


def get_batch_state(batch_id: str | None = None, plant_code: str | None = None) -> dict:
    requested_plant = _effective_plant_code(plant_code, "") if plant_code else _effective_plant_code("", "")
    with db_connect() as conn:
        batch_id = batch_id or _latest_batch_id(conn)
        if not batch_id:
            return {"batch": None, "records": [], "masters": [], "mappings": [], "reviews": [], "quality_report": None,
                    "summary": {"record_count": 0, "master_count": 0, "review_count": 0,
                                "auto_merge_count": 0, "new_count": 0, "compression_rate": 0.0},
                    "lifecycle": [], "distribution_logs": [], "search_history": [], "feedback": [],
                    "workflow": {"batch_id": None, "steps": [], "completed_steps": 0,
                                 "total_steps": len(WORKFLOW_DEFINITION), "progress": 0, "closed_loop": False},
                    "vector_index": {"count": 0}, "audit_chain": {"valid": True, "block_count": 0}}
        batch_row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if not batch_row:
            raise LookupError("batch not found")
        all_records = _rows(conn, "SELECT * FROM records WHERE batch_id = ? ORDER BY id", (batch_id,))
        records = list(all_records)
        if requested_plant and requested_plant != "GROUP":
            records = [item for item in records if _plant_visible(item.get("plant_code"), requested_plant)]
        for row_number, record in enumerate(records, start=1):
            attributes = json.loads(record.pop("ext") or "{}")
            if not isinstance(attributes.get("_provenance"), dict):
                provenance, issues = _build_source_provenance(
                    record, attributes, batch_id, batch_row["filename"], row_number
                )
                attributes["_provenance"] = provenance
                attributes["_quality_issues"] = issues
            record["_ext"] = attributes
        mappings = _rows(conn, "SELECT * FROM mappings WHERE batch_id = ? ORDER BY id", (batch_id,))
        if requested_plant and requested_plant != "GROUP":
            mappings = [item for item in mappings if _plant_visible(item.get("plant_code"), requested_plant)]
        masters = _rows(conn, "SELECT * FROM batch_masters WHERE batch_id = ? ORDER BY rowid", (batch_id,))
        if not masters:
            # Databases from builds before batch snapshots can still be inspected.
            codes = list(dict.fromkeys(item["mdm_code"] for item in mappings if item.get("mdm_code")))
            if codes:
                placeholders = ",".join("?" for _ in codes)
                masters = _rows(conn, f"SELECT * FROM masters WHERE mdm_code IN ({placeholders}) ORDER BY id", codes)
        if requested_plant and requested_plant != "GROUP":
            masters = [item for item in masters if _master_distributable_to_plant(item.get("plant_codes"), requested_plant)]
        for master in masters:
            master["_ext"] = {key: master.get(key) or "" for key in ("model", "brand", "dn", "pressure", "material")}
            if not master.get("source_records"):
                master["source_records"] = ";".join(item["original_code"] for item in mappings if item["mdm_code"] == master["mdm_code"] and item.get("original_code"))
        reviews = _rows(conn, "SELECT * FROM reviews WHERE batch_id = ? AND status = 'REVIEW' ORDER BY id", (batch_id,))
        if requested_plant and requested_plant != "GROUP":
            reviews = [item for item in reviews if _plant_visible(item.get("plant_codes"), requested_plant)]
        for review in reviews:
            review["_ext"] = json.loads(review.pop("attributes") or "{}")
            review["candidates"] = json.loads(review.get("candidates") or "[]")
        quality_row = conn.execute("SELECT report FROM quality_reports WHERE batch_id = ?", (batch_id,)).fetchone()
        quality = json.loads(quality_row["report"]) if quality_row else None
        if quality and any(
            "completeness" not in item for item in quality.get("systems", {}).values()
        ):
            quality = analyze_quality(all_records)
            conn.execute(
                "UPDATE quality_reports SET report = ?, generated_at = ? WHERE batch_id = ?",
                (json.dumps(quality, ensure_ascii=False), _utc_now(), batch_id),
            )
        if quality is not None and "source_issues" not in quality:
            quality["source_issues"] = [
                {
                    **issue, "record_id": record.get("id"), "material_code": record.get("material_code"),
                    "material_name": record.get("material_name"), "system_source": record.get("system_source"),
                    "plant_code": record.get("plant_code"),
                    **{key: record.get("_ext", {}).get("_provenance", {}).get(key) for key in (
                        "source_record_id", "source_table", "connector_status", "source_url", "record_hash"
                    )},
                }
                for record in records for issue in record.get("_ext", {}).get("_quality_issues", [])
            ]
            fingerprints = sorted(
                record.get("_ext", {}).get("_provenance", {}).get("record_hash", "") for record in records
                if record.get("_ext", {}).get("_provenance", {}).get("record_hash")
            )
            quality["lineage"] = {
                "traceable_records": len(records),
                "problem_records": len({(item.get("system_source"), item.get("source_record_id")) for item in quality["source_issues"]}),
                "source_systems": sorted({record.get("system_source") or "CSV导入" for record in records}),
                "source_fingerprint_root": hashlib.sha256("".join(fingerprints).encode("ascii")).hexdigest() if fingerprints else "",
            }
        lifecycle = _rows(conn, "SELECT * FROM lifecycle WHERE batch_id = ? ORDER BY id DESC", (batch_id,))
        if requested_plant and requested_plant != "GROUP":
            lifecycle = [item for item in lifecycle if _plant_visible(item.get("plant_code"), requested_plant)]
        for item in lifecycle:
            item["status"] = _clean_value(item.get("status")).upper() or "PENDING"
        decision_counts = {
            decision: sum(master.get("decision") == decision for master in masters)
            for decision in ("AUTO_MERGE", "REVIEW", "NEW")
        }
        record_count = len(records)
        master_count = len(masters)
        summary = {
            "record_count": record_count,
            "master_count": master_count,
            "review_count": len(reviews),
            "auto_merge_count": decision_counts["AUTO_MERGE"],
            "new_count": decision_counts["NEW"],
            "compression_rate": round((1 - master_count / record_count) * 100, 1) if record_count else 0.0,
        }
        distribution_logs = _rows(conn, "SELECT * FROM distribution_logs WHERE batch_id = ? ORDER BY id DESC", (batch_id,))
        for item in distribution_logs:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        if requested_plant and requested_plant != "GROUP":
            distribution_logs = [item for item in distribution_logs if _plant_visible(item.get("plant_code"), requested_plant)]
        batch = dict(batch_row)
        batch["view_plant_code"] = requested_plant or "GROUP"
        governance = {
            "method": batch.get("semantic_method") or "历史批次：规则相似度",
            "model": batch.get("semantic_model") or "local",
            "dimension": batch.get("semantic_dimension"),
            "warning": batch.get("semantic_warning"),
            "embedding_active": bool(batch.get("semantic_dimension")),
        }
        vector_group = conn.execute(
            """SELECT provider, model, dimension, COUNT(*) AS count, MAX(updated_at) AS updated_at
               FROM vector_embeddings WHERE batch_id = ? GROUP BY provider, model, dimension
               ORDER BY count DESC""", (batch_id,),
        ).fetchall()
        vector_index = {
            "count": sum(int(row["count"]) for row in vector_group),
            "indexes": [dict(row) for row in vector_group],
        }
        feedback = _rows(conn, "SELECT * FROM plant_feedback WHERE batch_id = ? ORDER BY id DESC", (batch_id,))
        if requested_plant and requested_plant != "GROUP":
            feedback = [item for item in feedback if item.get("plant_code") == requested_plant]
        return {
            "batch": batch, "records": records, "masters": masters, "mappings": mappings,
            "reviews": reviews, "quality_report": quality, "summary": summary,
            "governance": governance,
            "lifecycle": lifecycle,
            "distribution_logs": distribution_logs,
            "search_history": _rows(conn, "SELECT * FROM search_history WHERE batch_id = ? ORDER BY id DESC LIMIT 50", (batch_id,)),
            "workflow": _workflow_payload(conn, batch_id),
            "feedback": feedback,
            "vector_index": vector_index,
            "audit_chain": _verify_audit_chain(conn, batch_id),
        }


@app.get("/api/live")
def live_check():
    """Public liveness endpoint with no infrastructure or tenant details."""
    return jsonify({"status": "ok", "version": APP_VERSION})


@app.post("/api/auth/login")
def auth_login():
    payload = request.get_json(silent=True) or {}
    username = _clean_value(payload.get("username"))
    password = str(payload.get("password") or "")
    if not username or not password:
        return jsonify({"error": "username and password are required", "code": "LOGIN_INPUT_REQUIRED"}), 400
    if enterprise_security.mode == "open":
        principal = enterprise_security.open_principal()
        csrf_token = enterprise_security.start_session(principal)
        return jsonify({"principal": principal, "csrf_token": csrf_token, "security_mode": "open"})
    if enterprise_security.mode == "basic":
        valid = hmac.compare_digest(username, AUTH_USER) and bool(AUTH_PASSWORD) and hmac.compare_digest(password, AUTH_PASSWORD)
        principal = enterprise_security.open_principal(username=AUTH_USER, display_name="Legacy administrator") if valid else None
        error = "" if valid else "invalid username or password"
    else:
        principal, error = enterprise_security.authenticate(username, password)
    if not principal:
        enterprise_security.record_event(
            "LOGIN_FAILED", "DENIED", "/api/auth/login", {"username": username, "reason": error},
            None, g.trace_id, request.remote_addr or "",
        )
        g.security_event_recorded = True
        return jsonify({"error": error or "invalid username or password", "code": "LOGIN_FAILED"}), 401
    csrf_token = enterprise_security.start_session(principal)
    enterprise_security.record_event(
        "LOGIN_SUCCEEDED", "SUCCESS", "/api/auth/login", {}, principal, g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    return jsonify({
        "principal": principal, "csrf_token": csrf_token, "security_mode": enterprise_security.mode,
        "password_change_recommended": enterprise_security.initial_credentials_path.is_file(),
    })


@app.get("/api/auth/me")
def auth_me():
    principal = getattr(g, "principal", None) or enterprise_security.resolve_principal()
    if not principal:
        return jsonify({"error": "please sign in", "code": "AUTH_REQUIRED"}), 401
    allowed_plants = PLANTS if principal["plant_code"] == "GROUP" else {
        principal["plant_code"]: PLANTS.get(principal["plant_code"], principal["plant_code"])
    }
    return jsonify({
        "principal": principal, "csrf_token": session.get("csrf_token", ""),
        "security_mode": enterprise_security.mode, "allowed_plants": allowed_plants,
        "password_change_recommended": enterprise_security.initial_credentials_path.is_file(),
    })


@app.post("/api/auth/logout")
def auth_logout():
    principal = getattr(g, "principal", None)
    enterprise_security.record_event(
        "LOGOUT", "SUCCESS", "/api/auth/logout", {}, principal, g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    session.clear()
    return jsonify({"signed_out": True})


@app.post("/api/auth/change-password")
def auth_change_password():
    if enterprise_security.mode != "enterprise":
        return jsonify({"error": "password changes require enterprise security mode"}), 409
    payload = request.get_json(silent=True) or {}
    ok, error = enterprise_security.change_password(
        g.principal, str(payload.get("current_password") or ""), str(payload.get("new_password") or ""),
    )
    if not ok:
        return jsonify({"error": error}), 400
    enterprise_security.record_event(
        "PASSWORD_CHANGED", "SUCCESS", "/api/auth/change-password", {}, g.principal,
        g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    return jsonify({"changed": True, "csrf_token": session.get("csrf_token", "")})


@app.route("/api/admin/users", methods=["GET", "POST"])
def admin_users():
    if request.method == "GET":
        return jsonify({"users": enterprise_security.list_users(), "roles": enterprise_security.public_roles()})
    payload = request.get_json(silent=True) or {}
    user, error = enterprise_security.create_user(payload, g.principal)
    if not user:
        return jsonify({"error": error}), 400
    enterprise_security.record_event(
        "USER_CREATED", "SUCCESS", f"security_user:{user['id']}",
        {key: value for key, value in user.items() if key != "created_by"}, g.principal,
        g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    return jsonify(user), 201


@app.get("/api/security/audit")
def security_audit_events():
    try:
        limit = min(500, max(1, int(request.args.get("limit", 100))))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    with db_connect() as conn:
        rows = _rows(
            conn,
            """SELECT height, event_type, actor, role, plant_code, resource, outcome, details_json,
                      trace_id, client_ip, previous_hash, event_hash, created_at
                 FROM security_events ORDER BY height DESC LIMIT ?""",
            (limit,),
        )
    for row in rows:
        row["details"] = json.loads(row.pop("details_json") or "{}")
        if g.principal.get("role") != "GROUP_ADMIN":
            row["client_ip"] = "masked"
    return jsonify({"events": rows, "count": len(rows), "verification": enterprise_security.verify_events()})


@app.get("/api/security/audit/verify")
def security_audit_verify():
    return jsonify(enterprise_security.verify_events())


@app.get("/api/governance/catalog")
def governance_catalog():
    """Return the DMBOK control map, business metadata, quality rules, and RACI."""
    with db_connect() as conn:
        catalog = enterprise_governance.catalog(conn)
        if request.args.get("include_issues") == "1":
            catalog["issue_summary"] = enterprise_governance.issue_summary(
                conn,
                _clean_value(request.args.get("batch_id")) or None,
                _effective_plant_code(request.args.get("plant_code")),
            )
        else:
            catalog["issue_summary"] = {
                "issues": [], "count": 0, "status_counts": {},
                "dimension_counts": {}, "overdue_count": 0,
            }
    catalog["trace_id"] = g.trace_id
    return jsonify(catalog)


@app.get("/api/governance/issues")
def governance_issues():
    batch_id = _clean_value(request.args.get("batch_id")) or None
    requested_plant = _effective_plant_code(request.args.get("plant_code"))
    with db_connect() as conn:
        result = enterprise_governance.issue_summary(conn, batch_id, requested_plant)
    result.update({"batch_id": batch_id, "plant_code": requested_plant, "trace_id": g.trace_id})
    return jsonify(result)


@app.patch("/api/governance/issues/<issue_id>")
def update_governance_issue(issue_id: str):
    payload = request.get_json(silent=True) or {}
    status = _clean_value(payload.get("status")).upper()
    if not status:
        return jsonify({"error": "status is required"}), 400
    try:
        with db_connect() as conn:
            issue = enterprise_governance.update_issue(
                conn, issue_id, status, _clean_value(payload.get("resolution")),
                _current_actor(), _effective_plant_code(payload.get("plant_code")),
            )
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    if not issue:
        return jsonify({"error": "quality issue not found"}), 404
    enterprise_security.record_event(
        "QUALITY_ISSUE_UPDATED", "SUCCESS", f"quality-issue:{issue_id}",
        {"status": status, "batch_id": issue["batch_id"], "record_id": issue["record_id"]},
        g.principal, g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    return jsonify({"issue": issue, "trace_id": g.trace_id})


@app.patch("/api/governance/controls/<control_code>")
def assess_governance_control(control_code: str):
    payload = request.get_json(silent=True) or {}
    try:
        with db_connect() as conn:
            control = enterprise_governance.assess_control(
                conn, control_code.upper(), payload, g.principal["username"]
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not control:
        return jsonify({"error": "governance control not found"}), 404
    enterprise_security.record_event(
        "GOVERNANCE_CONTROL_ASSESSED", "SUCCESS", f"control:{control_code.upper()}",
        {"status": control["status"], "maturity_level": control["maturity_level"]},
        g.principal, g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    return jsonify({"control": control, "trace_id": g.trace_id})


@app.get("/api/compliance/status")
def compliance_status():
    summary = enterprise_security.compliance_summary()
    with db_connect() as conn:
        summary["retention_policies"] = _rows(conn, "SELECT * FROM retention_policies ORDER BY retention_days")
        catalog = enterprise_governance.catalog(conn)
        summary["dmbok_control_domains"] = catalog["domains"]
        summary["metadata_element_count"] = len(catalog["metadata_catalog"])
        summary["active_quality_rule_count"] = sum(1 for rule in catalog["quality_rules"] if rule["active"])
        issue_summary = enterprise_governance.issue_summary(
            conn, plant_code=_effective_plant_code(request.args.get("plant_code"))
        )
        issue_summary.pop("issues", None)
        summary["quality_issue_summary"] = issue_summary
    summary["ai_egress_policy"] = {
        level: {"external_ai_allowed": policy["external_ai"]}
        for level, policy in EnterpriseSecurity.DATA_CLASSIFICATIONS.items()
    }
    summary["claim"] = "已形成安全控制与审计证据；不等同于通过等保、数据合规或法律认证。"
    return jsonify(summary)


@app.patch("/api/compliance/batches/<batch_id>")
def update_batch_compliance(batch_id: str):
    payload = request.get_json(silent=True) or {}
    classification = _clean_value(payload.get("data_classification")).upper()
    if classification and classification not in EnterpriseSecurity.DATA_CLASSIFICATIONS:
        return jsonify({"error": "unsupported data_classification",
                        "supported": list(EnterpriseSecurity.DATA_CLASSIFICATIONS)}), 400
    retention_until = _clean_value(payload.get("retention_until"))
    if retention_until:
        try:
            datetime.fromisoformat(retention_until.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"error": "retention_until must be an ISO-8601 timestamp"}), 400
    legal_hold_value = payload.get("legal_hold")
    if legal_hold_value is not None and not isinstance(legal_hold_value, bool):
        return jsonify({"error": "legal_hold must be a JSON boolean"}), 400
    with db_connect() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if not batch:
            return jsonify({"error": "batch not found"}), 404
        if g.principal["plant_code"] != "GROUP" and batch["plant_code"] != g.principal["plant_code"]:
            return jsonify({"error": "batch is outside the identity plant scope"}), 403
        values = {
            "data_classification": classification or batch["data_classification"],
            "data_owner": _clean_value(payload.get("data_owner")) if "data_owner" in payload else batch["data_owner"],
            "processing_purpose": _clean_value(payload.get("processing_purpose")) if "processing_purpose" in payload else batch["processing_purpose"],
            "retention_until": retention_until if "retention_until" in payload else batch["retention_until"],
            "legal_hold": int(legal_hold_value) if legal_hold_value is not None else int(batch["legal_hold"] or 0),
        }
        conn.execute(
            """UPDATE batches SET data_classification = ?, data_owner = ?, processing_purpose = ?,
                      retention_until = ?, legal_hold = ? WHERE batch_id = ?""",
            (*values.values(), batch_id),
        )
        if legal_hold_value is True and not int(batch["legal_hold"] or 0):
            reason = _clean_value(payload.get("legal_hold_reason")) or "合规调查或业务保全"
            conn.execute(
                """INSERT INTO legal_holds (batch_id, reason, active, created_by, created_at)
                   VALUES (?, ?, 1, ?, ?)""",
                (batch_id, reason, g.principal["username"], _utc_now()),
            )
        elif legal_hold_value is False and int(batch["legal_hold"] or 0):
            conn.execute(
                """UPDATE legal_holds SET active = 0, released_by = ?, released_at = ?
                   WHERE batch_id = ? AND active = 1""",
                (g.principal["username"], _utc_now(), batch_id),
            )
    enterprise_security.record_event(
        "BATCH_COMPLIANCE_UPDATED", "SUCCESS", f"batch:{batch_id}", values,
        g.principal, g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    return jsonify({"batch_id": batch_id, **values})


@app.post("/api/compliance/retention/run")
def run_retention_policy():
    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return jsonify({"error": "dry_run must be a JSON boolean"}), 400
    now = _utc_now()
    with db_connect() as conn:
        expired = _rows(
            conn,
            """SELECT batch_id, filename, data_classification, retention_until
                 FROM batches WHERE legal_hold = 0 AND retention_until IS NOT NULL
                  AND retention_until != '' AND retention_until <= ? ORDER BY retention_until""",
            (now,),
        )
        held = _rows(
            conn,
            """SELECT batch_id, filename, data_classification, retention_until
                 FROM batches WHERE legal_hold = 1 AND retention_until IS NOT NULL
                  AND retention_until != '' AND retention_until <= ? ORDER BY retention_until""",
            (now,),
        )
        if not dry_run:
            if payload.get("confirm") != "DELETE_EXPIRED_BATCHES":
                return jsonify({"error": "confirmation token is required"}), 400
            for item in expired:
                batch = item["batch_id"]
                for table in (
                    "plant_feedback", "workflow_steps", "audit_blocks", "distribution_logs", "lifecycle",
                    "search_history", "quality_reports", "reviews", "mappings", "batch_masters",
                    "vector_embeddings", "records",
                ):
                    conn.execute(f"DELETE FROM {table} WHERE batch_id = ?", (batch,))
                conn.execute("DELETE FROM batches WHERE batch_id = ?", (batch,))
    enterprise_security.record_event(
        "RETENTION_DRY_RUN" if dry_run else "RETENTION_EXECUTED", "SUCCESS", "retention-policy",
        {"expired_batch_ids": [item["batch_id"] for item in expired],
         "held_batch_ids": [item["batch_id"] for item in held], "deleted": 0 if dry_run else len(expired)},
        g.principal, g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    return jsonify({
        "dry_run": dry_run, "expired": expired, "blocked_by_legal_hold": held,
        "eligible_count": len(expired), "deleted_count": 0 if dry_run else len(expired),
    })


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, FRONTEND_FILE)


@app.get("/api/health")
def health_check():
    database_error = ""
    standard_kb_count = 0
    try:
        with db_connect() as conn:
            conn.execute("SELECT 1").fetchone()
            standard_kb_count = enterprise_rag.stats().get("count", 0)
        database_ready = True
    except Exception as exc:
        database_ready = False
        database_error = type(exc).__name__
        logger.exception("database readiness check failed")
    principal = getattr(g, "principal", None)
    if enterprise_security.mode == "enterprise" and not principal:
        return jsonify({
            "status": "ok" if database_ready else "degraded", "ready": database_ready,
            "version": APP_VERSION, "authentication": True, "security_mode": "enterprise",
            "database_error": database_error, "trace_id": g.trace_id,
        }), 200 if database_ready else 503
    payload = {
        "status": "ok" if database_ready else "degraded", "ready": database_ready,
        "version": APP_VERSION, "storage": "sqlite", "database": DB_PATH.name,
        "deployment": "production" if os.environ.get("MDM_PRODUCTION") == "1" else "development",
        "authentication": enterprise_security.mode != "open", "security_mode": enterprise_security.mode,
        "database_error": database_error,
        "semantic": {
            "primary": "qwen", "model": SemanticEngine.MODELS["qwen"]["model"],
            "dimension": SemanticEngine.MODELS["qwen"]["dimension"],
            "configured_models": semantic_engine.configured_models(),
        },
        "plants": PLANTS,
        "capabilities": {
            "llm_agent": bool(qwen_agent.api_key), "vector_store": True, "knowledge_graph": True,
            "audit_blockchain": False, "tamper_evident_audit_chain": True,
            "closed_loop_workflow": True, "real_ocr": ocr_engine.status(),
            "source_lineage": True, "governance_reports": True,
            "natural_language_distribution": True, "explainable_governance": True,
            "standard_rag": {"namespace": "standard_kb", "count": standard_kb_count},
            "enterprise_security": {
                "rbac": enterprise_security.mode == "enterprise", "csrf": enterprise_security.mode == "enterprise",
                "signed_audit": "HMAC-SHA256", "data_classification": True,
                "retention_and_legal_hold": True,
            },
        },
        "trace_id": g.trace_id,
    }
    return jsonify(payload), 200 if database_ready else 503


@app.get("/api/state/latest")
def latest_state():
    return jsonify(get_batch_state(plant_code=request.args.get("plant_code")))


@app.get("/api/batches/<batch_id>")
def batch_state(batch_id: str):
    try:
        return jsonify(get_batch_state(batch_id, request.args.get("plant_code")))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404


def _collaboration_summary(state: dict) -> dict:
    masters = state.get("masters", [])
    approved = [
        master for master in masters
        if master.get("decision") in {"AUTO_MERGE", "NEW", "CONFIRMED_NEW"}
    ]
    site_codes = {code for code in PLANTS if code != "GROUP"}
    shared = [master for master in masters if len({p for p in _clean_value(master.get("plant_codes")).split(",") if p}) > 1]
    cross_plant_reviews = [
        review for review in state.get("reviews", [])
        if len({p for p in _clean_value(review.get("plant_codes")).split(",") if p}) > 1
    ]
    avoided_duplicates = sum(max(0, int(master.get("source_count") or 0) - 1) for master in shared)
    total_sources = sum(int(master.get("source_count") or 0) for master in masters)
    shared_sources = sum(int(master.get("source_count") or 0) for master in shared)
    potential_plant_deliveries = 0
    for master in approved:
        plants = {_normalize_plant_code(item) for item in _clean_value(master.get("plant_codes")).split(",") if item}
        potential_plant_deliveries += len(site_codes) if "GROUP" in plants else len(plants & site_codes)
    successful_logs = [item for item in state.get("distribution_logs", []) if item.get("status") == "SUCCESS"]
    return {
        "approved_golden_masters": len(approved),
        "available_site_count": len(site_codes),
        "potential_plant_deliveries": potential_plant_deliveries,
        "successful_distribution_tasks": len(successful_logs),
        "active_distribution_plants": len({item.get("plant_code") for item in successful_logs if item.get("plant_code") in site_codes}),
        "shared_golden_masters": len(shared),
        "shared_source_records": shared_sources,
        "shared_master_rate": round(len(shared) / len(masters) * 100, 1) if masters else 0.0,
        "cross_plant_reviews": len(cross_plant_reviews),
        "avoided_duplicate_codes": avoided_duplicates,
        "avoided_duplicate_rate": round(avoided_duplicates / total_sources * 100, 1) if total_sources else 0.0,
        "estimated_manual_hours_saved": round(avoided_duplicates * 0.5, 1),
        "group_compression_rate": state.get("summary", {}).get("compression_rate", 0.0),
        "governance_model": state.get("governance", {}),
    }


@app.get("/api/plants")
def plant_collaboration():
    batch_id = request.args.get("batch_id")
    try:
        group_state = get_batch_state(batch_id, "GROUP")
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    if not group_state.get("batch"):
        return jsonify({"batch": None, "plants": [], "collaboration": _collaboration_summary(group_state)})
    batch_id = group_state["batch"]["batch_id"]
    site_codes = [code for code in PLANTS if code != "GROUP"]
    plant_rows = [{
        "plant_code": "GROUP", "plant_name": PLANTS["GROUP"], "scope": "GROUP_ALL",
        "roles": ["集团数据管理员", "集团审批人"], **group_state["summary"],
        "lifecycle_count": len(group_state.get("lifecycle", [])),
    }]
    for code in site_codes:
        site_state = get_batch_state(batch_id, code)
        plant_rows.append({
            "plant_code": code, "plant_name": PLANTS.get(code, code), "scope": "PLANT_ONLY",
            "roles": ["工厂申请人", "工厂数据管理员"], **site_state["summary"],
            "lifecycle_count": len(site_state.get("lifecycle", [])),
        })
    return jsonify({
        "batch": group_state["batch"], "plants": plant_rows,
        "collaboration": _collaboration_summary(group_state),
    })


@app.post("/api/batches")
def create_batch():
    payload = request.get_json(silent=True) or {}
    try:
        plant_code = _effective_plant_code(payload.get("plant_code"))
        records = normalize_records(payload.get("records", []), payload.get("mapping"), plant_code)
        return jsonify(persist_batch(
            _clean_value(payload.get("filename")) or "uploaded.csv",
            _clean_value(payload.get("encoding")) or "utf-8", records,
            _clean_value(payload.get("model")) or "qwen",
        )), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/upload")
def upload_csv():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "CSV file is required"}), 400
    raw = uploaded.read()
    if not raw:
        return jsonify({"error": "CSV file is empty"}), 400
    encoding = (chardet.detect(raw).get("encoding") or "utf-8").lower()
    try:
        frame = pd.read_csv(io.BytesIO(raw), encoding=encoding)
        records = normalize_records(frame.to_dict("records"), default_plant_code=_effective_plant_code(request.form.get("plant_code")))
        return jsonify(persist_batch(uploaded.filename, encoding, records, _clean_value(request.form.get("model")) or "qwen")), 201
    except (UnicodeError, pd.errors.ParserError, ValueError) as exc:
        return jsonify({"error": f"invalid CSV: {exc}"}), 400


@app.post("/api/semantic")
def api_semantic():
    payload = request.get_json(silent=True) or {}
    text1 = _clean_value(payload.get("text1"))
    text2 = _clean_value(payload.get("text2"))
    requested_model = _clean_value(payload.get("model")) or "qwen"
    if not text1 or not text2:
        return jsonify({"error": "text1 and text2 are required"}), 400
    if requested_model not in SemanticEngine.MODELS:
        return jsonify({"error": f"unsupported model: {requested_model}", "supported_models": list(SemanticEngine.MODELS)}), 400

    vectors, metadata = semantic_engine.resolve_embeddings([text1, text2], requested_model)
    if vectors is None:
        similarity = semantic_engine.jaccard_similarity(text1, text2)
        return jsonify({
            "similarity": round(similarity, 6),
            **metadata, "requested_model": requested_model, "text1": text1, "text2": text2,
        })
    return jsonify({
        "similarity": round(semantic_engine.cosine_similarity(vectors[0], vectors[1]), 6),
        **metadata, "requested_model": requested_model, "text1": text1, "text2": text2,
    })


@app.post("/api/govern")
def api_govern():
    payload = request.get_json(silent=True) or {}
    records = payload.get("records")
    if records is None:
        records = semantic_governor.generate_demo_records()
    if not isinstance(records, list) or not records:
        return jsonify({"error": "records must be a non-empty array"}), 400
    if len(records) > MAX_RECORDS:
        return jsonify({"error": f"one batch may contain at most {MAX_RECORDS} records"}), 400
    if any(not isinstance(record, dict) for record in records):
        return jsonify({"error": "every record must be an object"}), 400
    model = _clean_value(payload.get("model")) or "qwen"
    if model not in SemanticEngine.MODELS:
        return jsonify({"error": f"unsupported model: {model}", "supported_models": list(SemanticEngine.MODELS)}), 400
    return jsonify(semantic_governor.govern(records, model))


@app.post("/api/classify")
def api_classify():
    payload = request.get_json(silent=True) or {}
    name = _clean_value(payload.get("material_name") or payload.get("name") or payload.get("text"))
    description = _clean_value(payload.get("description"))
    if not name and not description:
        return jsonify({"error": "material_name or text is required"}), 400
    model = _clean_value(payload.get("model")) or "qwen"
    if model not in SemanticEngine.MODELS:
        return jsonify({"error": f"unsupported model: {model}", "supported_models": list(SemanticEngine.MODELS)}), 400
    source_text = f"{name} {description}".strip()
    enriched = engine.enrich({"material_name": name, "description": description, "category": payload.get("category", "")})
    principal = getattr(g, "principal", {}) or {}
    clearance = {
        "GROUP_ADMIN": "RESTRICTED", "AUDITOR": "RESTRICTED",
        "GROUP_APPROVER": "CONFIDENTIAL", "PLANT_STEWARD": "INTERNAL",
    }.get(principal.get("role"), "INTERNAL")
    try:
        rag_result = enterprise_rag.classify(
            name, description, top_k=3, version_id=_clean_value(payload.get("version_id")) or None,
            plant_code=_effective_plant_code(payload.get("plant_code")),
            clearance=clearance, profile_name=_clean_value(payload.get("profile")) or None,
            trace_id=g.trace_id, actor=principal.get("username", "system"),
        )
        category = rag_result["recommended_category"]
        attributes = engine.presentation_attributes(enriched, category)
        rag_result["attributes"] = attributes
        rag_result["standard_name_preview"] = engine.generate_standard_name(
            [{**enriched, "_ext": attributes}], category
        )
        rag_result["source_text"] = source_text
        return jsonify(rag_result)
    except (LookupError, PermissionError, ValueError) as exc:
        logger.warning("enterprise RAG classification fallback: %s", exc)
    with db_connect() as conn:
        standard_references = _search_standard_kb(conn, source_text, 3)
    rag_scores = {item["category"]: item["score"] for item in standard_references}
    categories = [category for category, _pattern in engine.CATEGORY_PATTERNS]
    category_texts = [
        engine.standardize_text(f"{category} {' '.join(engine.SYNONYMS.get(category, []))}") for category in categories
    ]
    query_text = engine.standardize_text(source_text)
    vectors, metadata = semantic_engine.resolve_embeddings([query_text, *category_texts], model)
    candidates = []
    rule_category = enriched["_category"]
    for index, category in enumerate(categories):
        semantic_score = (
            semantic_engine.cosine_similarity(vectors[0], vectors[index + 1])
            if vectors is not None else semantic_engine.jaccard_similarity(query_text, category_texts[index])
        )
        rule_bonus = 0.78 if category == rule_category else 0.0
        rag_score = rag_scores.get(category, 0.0)
        candidates.append({
            "category": category, "score": round(min(0.99, max(
                semantic_score * 0.21 + rule_bonus, rag_score * 0.32 + (0.65 if category == rule_category else 0.0)
            )), 4),
            "code_prefix": engine.CATEGORY_PREFIX.get(category, "MDM-X"),
            "rag_score": round(rag_score, 4),
            "reason": "标准规则命中 + 标准知识库RAG + AI语义匹配" if rule_bonus else (
                "标准知识库RAG候选" if rag_score else "AI语义候选"
            ),
        })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    recommended = candidates[0]
    public_attributes = engine.presentation_attributes(enriched, recommended["category"])
    preview = engine.generate_standard_name([{**enriched, "_ext": public_attributes}], recommended["category"])
    return jsonify({
        "standard": "SY/T5497-2018", "recommended_category": recommended["category"],
        "confidence": recommended["score"], "code_prefix": recommended["code_prefix"],
        "attributes": public_attributes, "standard_name_preview": preview,
        "candidates": candidates[:3], "semantic": metadata, "standard_references": standard_references,
        "rag": {"namespace": "standard_kb", "retrieval_count": len(standard_references),
                "method": "离线Embedding检索增强"},
        "plant_code": _effective_plant_code(payload.get("plant_code")),
    })


@app.get("/api/agent/capabilities")
def agent_capabilities():
    return jsonify({
        "agent": "M-AI Master", "version": APP_VERSION,
        "workflow": [
            {"step": ordinal, "code": code, "name": name, "endpoint": endpoint}
            for code, ordinal, name, endpoint in WORKFLOW_DEFINITION
        ],
        "semantic_models": SemanticEngine.MODELS,
        "configured_models": semantic_engine.configured_models(),
        "llm_agent": bool(qwen_agent.api_key),
        "llm_model": qwen_agent.model,
        "speech": {
            "qwen_asr_configured": bool(os.getenv("DASHSCOPE_API_KEY", "").strip()),
            "model": os.getenv("QWEN_ASR_MODEL", "qwen3-asr-flash-2026-02-10"),
            "fallback": "browser-speech-recognition",
            "audio_persisted": False,
        },
        "fallback": "规则增强 Jaccard 字符相似度（确定性）",
        "plants": PLANTS,
        "collaboration_endpoint": "/api/plants",
        "productization": {
            "trace_header": "X-Trace-Id", "persistent_vector_store": "SQLite float32 BLOB",
            "graph_engine": "NetworkX", "audit_chain": "SHA-256 chained blocks",
            "feedback_loop": "/api/feedback",
        },
    })


@app.post("/api/agent/plan")
def agent_plan():
    payload = request.get_json(silent=True) or {}
    task = _clean_value(payload.get("task") or payload.get("prompt") or payload.get("text"))
    if not task:
        return jsonify({"error": "task is required"}), 400
    batch_id = _clean_value(payload.get("batch_id"))
    context = {"plant_code": _effective_plant_code(payload.get("plant_code")), "trace_id": g.trace_id}
    if batch_id:
        try:
            state = get_batch_state(batch_id)
            context.update({"batch_id": batch_id, "summary": state["summary"], "workflow": state["workflow"]})
        except LookupError:
            return jsonify({"error": "batch not found"}), 404
    plan, runtime = qwen_agent.plan(task, context)
    return jsonify({"plan": plan, "runtime": runtime, "trace_id": g.trace_id})


def _route_agent_action(text: str) -> tuple[str, float, str]:
    lower = _clean_value(text).lower()
    distribution_verbs = ("分发", "发往", "发送", "同步", "下发", "推送", "发到")
    distribution_targets = tuple(DISTRIBUTION_SYSTEM_ALIASES) + ("系统", "工厂", "上海厂", "北京厂")
    governance_words = ("治理", "归并", "查重", "闭环", "质量评估", "审核", "编排", "处理批次")
    if any(word.lower() in lower for word in distribution_verbs) and any(
        word.lower() in lower for word in distribution_targets
    ):
        return "DISTRIBUTE", 0.96, "识别到分发动词以及目标系统或工厂"
    if any(word.lower() in lower for word in governance_words):
        return "GOVERN", 0.9, "识别到主数据治理或闭环编排意图"
    return "SEARCH", 0.82, "未发现副作用操作，按智能检索处理"


@app.post("/api/agent/query")
def unified_agent_query():
    """Classify a command and return a plan; this endpoint never executes side effects."""
    payload = request.get_json(silent=True) or {}
    text = _clean_value(payload.get("text") or payload.get("query") or payload.get("prompt"))
    if not text:
        return jsonify({"error": "text is required"}), 400
    action, confidence, reason = _route_agent_action(text)
    return jsonify({
        "action": action, "intent": action, "confidence": confidence, "reason": reason,
        "requires_confirmation": action in {"DISTRIBUTE", "GOVERN"},
        "next_endpoint": {"SEARCH": "/api/search", "DISTRIBUTE": "/api/intent", "GOVERN": "/api/agent/plan"}[action],
        "trace_id": g.trace_id,
    })


@app.post("/api/agent/transcribe")
def transcribe_agent_audio():
    """Transcribe a short browser recording with Qwen3-ASR; never persist audio."""
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"error": "audio file is required", "fallback": "browser"}), 400
    max_bytes = int(os.getenv("MDM_MAX_ASR_BYTES", str(7 * 1024 * 1024)))
    content = audio.read(max_bytes + 1)
    if not content:
        return jsonify({"error": "audio file is empty", "fallback": "browser"}), 400
    if len(content) > max_bytes:
        return jsonify({"error": "audio file exceeds the configured limit", "fallback": "browser"}), 413
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        return jsonify({
            "error": "DASHSCOPE_API_KEY is not configured", "fallback": "browser",
            "warning": "通义语音未配置，前端将自动尝试浏览器语音识别。",
        }), 503
    mime_type = _clean_value(audio.mimetype).lower()
    allowed = {
        "audio/webm", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3",
        "audio/ogg", "audio/opus", "audio/aac", "audio/mp4", "video/webm",
    }
    if mime_type not in allowed:
        return jsonify({"error": f"unsupported audio type: {mime_type or 'unknown'}", "fallback": "browser"}), 415
    data_uri = f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
    model = os.getenv("QWEN_ASR_MODEL", "qwen3-asr-flash-2026-02-10").strip() or "qwen3-asr-flash-2026-02-10"
    url = os.getenv(
        "QWEN_ASR_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    ).strip()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [{
            "type": "input_audio", "input_audio": {"data": data_uri},
        }]}],
        "stream": False,
        "asr_options": {"language": "zh", "enable_itn": True},
    }
    try:
        upstream = requests.post(
            url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body, timeout=(5.0, 45.0),
        )
        if upstream.status_code != 200:
            logger.warning("Qwen ASR returned HTTP %s trace_id=%s", upstream.status_code, g.trace_id)
            return jsonify({
                "error": f"Qwen ASR returned HTTP {upstream.status_code}", "fallback": "browser",
                "warning": "通义语音暂不可用，前端将自动尝试浏览器语音识别。",
            }), 502
        result = upstream.json()
        transcript = _clean_value(result["choices"][0]["message"]["content"])
        if not transcript:
            raise ValueError("empty transcript")
        return jsonify({
            "text": transcript, "transcript": transcript, "provider": "qwen",
            "model": model, "audio_persisted": False, "trace_id": g.trace_id,
        })
    except (requests.Timeout, requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Qwen ASR unavailable: %s trace_id=%s", exc, g.trace_id)
        return jsonify({
            "error": "Qwen ASR is temporarily unavailable", "fallback": "browser",
            "warning": "通义语音识别失败，前端将自动尝试浏览器语音识别。",
        }), 502


@app.post("/api/demo/run")
def run_competition_demo():
    """Create a new deterministic batch and execute the full factory feedback loop."""
    demo_records = normalize_records([
        {"material_code": "DEMO-SAP-001", "system_source": "SAP", "material_name": "SKF 6312深沟球轴承", "description": "SKF 6312 C3 bearing", "category": "轴承", "unit": "个", "plant_code": "GROUP"},
        {"material_code": "DEMO-EAM-001", "system_source": "EAM", "material_name": "SKF 6312滚动轴承", "description": "Bearing SKF 6312 C3", "category": "轴承", "unit": "个", "plant_code": "GROUP"},
        {"material_code": "DEMO-SAP-002", "system_source": "SAP", "material_name": "Z41H-16C DN150闸阀", "description": "铸钢 PN16 法兰连接", "category": "阀门", "unit": "台", "plant_code": "GROUP"},
        {"material_code": "DEMO-EAM-002", "system_source": "EAM", "material_name": "DN150 Gate Valve Z41H-16C", "description": "carbon steel PN16", "category": "阀门", "unit": "台", "plant_code": "GROUP"},
    ])
    state = persist_batch(f"competition-demo-{datetime.now().strftime('%H%M%S')}.json", "utf-8", demo_records, "qwen")
    batch_id = state["batch"]["batch_id"]
    now = _utc_now()
    with db_connect() as conn:
        pending = conn.execute("SELECT id, mdm_code FROM reviews WHERE batch_id = ? AND status = 'REVIEW'", (batch_id,)).fetchall()
        for review in pending:
            conn.execute("UPDATE reviews SET status = 'APPROVED', approved_action = 'MERGE', approved_at = ? WHERE id = ?", (now, review["id"]))
            conn.execute("UPDATE batch_masters SET decision = 'AUTO_MERGE' WHERE batch_id = ? AND mdm_code = ?", (batch_id, review["mdm_code"]))
        _set_workflow_step(conn, batch_id, "REVIEW", "COMPLETED", 100, {"pending": 0, "demo_approved": len(pending)})
        masters = conn.execute(
            "SELECT mdm_code, standard_name FROM batch_masters WHERE batch_id = ? ORDER BY rowid", (batch_id,)
        ).fetchall()
        distribution_count = 0
        for plant_code in ("SHANGHAI", "BEIJING"):
            for target in ("SAP", "EAM"):
                for master in masters:
                    conn.execute(
                        """INSERT INTO distribution_logs
                           (batch_id, target_system, mdm_code, standard_name, sync_mode, sync_frequency,
                            status, message, plant_code, instruction)
                           VALUES (?, ?, ?, ?, 'FULL', 'MANUAL', 'SUCCESS', ?, ?, ?)""",
                        (batch_id, target, master["mdm_code"], master["standard_name"],
                         "比赛演示适配器已生成接口载荷", plant_code, f"同步到{PLANTS[plant_code]}{target}"),
                    )
                    distribution_count += 1
        _set_workflow_step(conn, batch_id, "DISTRIBUTE", "COMPLETED", 100, {
            "success_count": distribution_count, "failed_count": 0, "plants": ["SHANGHAI", "BEIJING"],
        })
        feedback_count = 0
        for index, plant_code in enumerate(("SHANGHAI", "BEIJING")):
            master = masters[index % len(masters)]
            conn.execute(
                """INSERT INTO plant_feedback
                   (batch_id, plant_code, mdm_code, accepted, rating, comment, actor, created_at)
                   VALUES (?, ?, ?, 1, 5, ?, ?, ?)""",
                (batch_id, plant_code, master["mdm_code"], "现场型号与黄金主数据一致，确认复用",
                 f"{PLANTS[plant_code]}数据管理员", now),
            )
            feedback_count += 1
        vector_meta = _index_batch_vectors(conn, batch_id, "local")
        _set_workflow_step(conn, batch_id, "FEEDBACK", "COMPLETED", 100, {
            "feedback_count": feedback_count, "accepted": feedback_count, "vector_refresh": vector_meta,
        })
        _append_audit_block(conn, batch_id, "DEMO_MULTI_PLANT_DISTRIBUTED", "DISTRIBUTION", batch_id, {
            "plants": ["SHANGHAI", "BEIJING"], "tasks": distribution_count, "success": distribution_count,
        }, actor="比赛演示Agent")
        _append_audit_block(conn, batch_id, "DEMO_FACTORY_FEEDBACK", "FEEDBACK", batch_id, {
            "feedback_count": feedback_count, "accepted": feedback_count, "vector_refresh": vector_meta,
        }, actor="多工厂协同Agent")
    result = get_batch_state(batch_id)
    result["demo_execution"] = {
        "deterministic": True, "distribution_tasks": distribution_count,
        "factory_feedback": feedback_count, "closed_loop": result["workflow"]["closed_loop"],
    }
    return jsonify(result), 201


@app.get("/api/workflow/latest")
def latest_workflow():
    with db_connect() as conn:
        batch_id = _latest_batch_id(conn)
        return jsonify(_workflow_payload(conn, batch_id) if batch_id else {
            "batch_id": None, "steps": [], "completed_steps": 0,
            "total_steps": len(WORKFLOW_DEFINITION), "progress": 0, "closed_loop": False,
        })


@app.get("/api/workflow/<batch_id>")
def batch_workflow(batch_id: str):
    with db_connect() as conn:
        if not conn.execute("SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)).fetchone():
            return jsonify({"error": "batch not found"}), 404
        return jsonify(_workflow_payload(conn, batch_id))


@app.get("/api/standards/stats")
def standard_kb_stats():
    return jsonify({**enterprise_rag.stats(), "audit": enterprise_rag.verify_audit_chain()})


@app.post("/api/standards/search")
def search_standard_kb():
    payload = request.get_json(silent=True) or {}
    query = _clean_value(payload.get("query") or payload.get("text"))
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        top_k = int(payload.get("top_k", 3))
    except (TypeError, ValueError):
        return jsonify({"error": "top_k must be an integer"}), 400
    principal = getattr(g, "principal", {}) or {}
    clearance = {
        "GROUP_ADMIN": "RESTRICTED", "AUDITOR": "RESTRICTED",
        "GROUP_APPROVER": "CONFIDENTIAL", "PLANT_STEWARD": "INTERNAL",
    }.get(principal.get("role"), "INTERNAL")
    try:
        result = enterprise_rag.search(
            query, top_k=top_k, version_id=_clean_value(payload.get("version_id")) or None,
            plant_code=_effective_plant_code(payload.get("plant_code")),
            clearance=clearance, profile_name=_clean_value(payload.get("profile")) or None,
            trace_id=g.trace_id, actor=principal.get("username", "system"),
        )
        return jsonify(result)
    except PermissionError as exc:
        return jsonify({"error": str(exc), "code": "KNOWLEDGE_SCOPE_DENIED"}), 403
    except (LookupError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/knowledge/collections")
def knowledge_collections():
    return jsonify({
        "collections": [{
            "catalog_id": enterprise_rag.catalog_id, "name": enterprise_rag.catalog_name,
            "standard_no": enterprise_rag.standard_no, **enterprise_rag.stats(),
        }],
        "versions": enterprise_rag.list_versions(), "audit": enterprise_rag.verify_audit_chain(),
    })


@app.get("/api/knowledge/versions")
def knowledge_versions():
    return jsonify({"versions": enterprise_rag.list_versions(), "active": enterprise_rag.stats()})


@app.post("/api/knowledge/import")
def knowledge_import():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "XLSX knowledge file is required"}), 400
    if Path(uploaded.filename).suffix.lower() != ".xlsx":
        return jsonify({"error": "only .xlsx standard knowledge files are supported"}), 400
    version_label = _clean_value(request.form.get("version_label"))
    if not version_label:
        return jsonify({"error": "version_label is required"}), 400
    allowed_plants = [
        item.strip().upper() for item in _clean_value(request.form.get("allowed_plants") or "*").split(",")
        if item.strip()
    ]
    allowed_classifications = [
        item.strip().upper() for item in _clean_value(
            request.form.get("allowed_classifications") or "INTERNAL,CONFIDENTIAL,RESTRICTED"
        ).split(",") if item.strip()
    ]
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="mai-rag-", suffix=".xlsx", delete=False) as temp_file:
            uploaded.save(temp_file)
            temp_path = Path(temp_file.name)
        result = enterprise_rag.import_xlsx(
            temp_path, version_label=version_label, actor=g.principal["username"],
            notes=_clean_value(request.form.get("notes")), allowed_plants=allowed_plants,
            allowed_classifications=allowed_classifications,
            security_classification=_clean_value(request.form.get("security_classification")) or "INTERNAL",
        )
        publish = _clean_value(request.form.get("publish")).lower() in {"1", "true", "yes"}
        if publish and result["status"] == "DRAFT":
            result = enterprise_rag.validate_version(result["version_id"], actor=g.principal["username"])
        if publish and result["status"] == "VALIDATED":
            result = enterprise_rag.publish_version(result["version_id"], actor=g.principal["username"])
        return jsonify({"import": result, "stats": enterprise_rag.stats()}), 201
    except (ValueError, FileNotFoundError, zipfile.BadZipFile) as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


@app.post("/api/knowledge/versions/<version_id>/validate")
def knowledge_validate(version_id: str):
    try:
        return jsonify(enterprise_rag.validate_version(version_id, actor=g.principal["username"]))
    except (LookupError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/knowledge/versions/<version_id>/publish")
def knowledge_publish(version_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        result = enterprise_rag.publish_version(
            version_id, actor=g.principal["username"],
            expected_current_version_id=payload.get("expected_current_version_id"),
        )
        return jsonify({"version": result, "stats": enterprise_rag.stats()})
    except (LookupError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.post("/api/knowledge/versions/<version_id>/rollback")
def knowledge_rollback(version_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        result = enterprise_rag.rollback_version(
            version_id, actor=g.principal["username"],
            expected_current_version_id=payload.get("expected_current_version_id"),
        )
        return jsonify({"version": result, "stats": enterprise_rag.stats()})
    except (LookupError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409


@app.post("/api/knowledge/reindex")
def knowledge_reindex():
    payload = request.get_json(silent=True) or {}
    try:
        result = enterprise_rag.reindex_version(
            _clean_value(payload.get("version_id")) or None,
            profile_name=_clean_value(payload.get("profile_name")) or None,
            actor=g.principal["username"],
        )
        return jsonify(result)
    except (LookupError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/knowledge/profile", methods=["GET", "POST"])
def knowledge_profile():
    if request.method == "GET":
        with db_connect() as conn:
            rows = _rows(
                conn,
                """SELECT profile_name, config_json, is_active, updated_by, updated_at
                     FROM enterprise_rag_profiles WHERE catalog_id = ? ORDER BY is_active DESC, profile_name""",
                (enterprise_rag.catalog_id,),
            )
        for row in rows:
            row["config"] = json.loads(row.pop("config_json") or "{}")
            row["is_active"] = bool(row["is_active"])
        return jsonify({"profiles": rows})
    payload = request.get_json(silent=True) or {}
    try:
        result = enterprise_rag.put_profile(
            _clean_value(payload.get("profile_name")), payload.get("config") or {},
            actor=g.principal["username"], activate=bool(payload.get("activate", False)),
        )
        if result["active"] and payload.get("reindex", True):
            result["index"] = enterprise_rag.reindex_version(
                profile_name=result["profile_name"], actor=g.principal["username"]
            )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/knowledge/audit/verify")
def knowledge_audit_verify():
    return jsonify(enterprise_rag.verify_audit_chain())


@app.get("/api/vectors/stats")
def vector_stats():
    with db_connect() as conn:
        batch_id = request.args.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"batch_id": None, "count": 0, "indexes": []})
        groups = _rows(conn, """SELECT namespace, provider, model, dimension, COUNT(*) AS count,
                                      MAX(updated_at) AS updated_at
                               FROM vector_embeddings WHERE batch_id = ?
                               GROUP BY namespace, provider, model, dimension ORDER BY count DESC""", (batch_id,))
        return jsonify({"batch_id": batch_id, "count": sum(int(item["count"]) for item in groups), "indexes": groups})


@app.post("/api/vectors/rebuild")
def rebuild_vectors():
    payload = request.get_json(silent=True) or {}
    model = _clean_value(payload.get("model")) or "qwen"
    if model not in {*SemanticEngine.MODELS, "local"}:
        return jsonify({"error": "unsupported vector model", "supported_models": [*SemanticEngine.MODELS, "local"]}), 400
    with db_connect() as conn:
        batch_id = payload.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"error": "no governed batch is available"}), 400
        result = _index_batch_vectors(conn, batch_id, model, _clean_value(payload.get("namespace")) or "golden_master")
        _set_workflow_step(conn, batch_id, "VECTOR_INDEX", "COMPLETED", 100, result)
        _append_audit_block(conn, batch_id, "VECTOR_INDEX_REBUILT", "VECTOR_INDEX", result["model"], result)
        return jsonify({**result, "trace_id": g.trace_id})


@app.post("/api/vectors/search")
def search_vector_store():
    payload = request.get_json(silent=True) or {}
    query = _clean_value(payload.get("query") or payload.get("text"))
    if not query:
        return jsonify({"error": "query is required"}), 400
    model = _clean_value(payload.get("model")) or "qwen"
    try:
        top_k = max(1, min(50, int(payload.get("top_k", 10))))
    except (TypeError, ValueError):
        return jsonify({"error": "top_k must be an integer"}), 400
    with db_connect() as conn:
        batch_id = payload.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"error": "no governed batch is available"}), 400
        result = _search_vectors(conn, query, batch_id, model, top_k,
                                 _effective_plant_code(payload.get("plant_code")),
                                 _clean_value(payload.get("namespace")) or "golden_master")
        return jsonify({**result, "trace_id": g.trace_id})


@app.get("/api/graph")
def governance_graph():
    try:
        raw_limit = max(0, min(300, int(request.args.get("raw_limit", 80))))
    except ValueError:
        return jsonify({"error": "raw_limit must be an integer"}), 400
    with db_connect() as conn:
        batch_id = request.args.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"batch_id": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0}})
        plant_code = _effective_plant_code(request.args.get("plant_code"))
        graph, stats = _build_governance_graph(conn, batch_id, plant_code, raw_limit)
        positions = nx.spring_layout(graph, seed=5497, iterations=35) if graph.number_of_nodes() else {}
        centrality = nx.degree_centrality(graph) if graph.number_of_nodes() > 1 else {node: 0 for node in graph.nodes}
        nodes = [{"id": node, **data, "x": round(float(positions[node][0]), 5),
                  "y": round(float(positions[node][1]), 5), "centrality": round(float(centrality[node]), 5)}
                 for node, data in graph.nodes(data=True)]
        edges = [{"source": left, "target": right, **data} for left, right, data in graph.edges(data=True)]
        return jsonify({"batch_id": batch_id, "plant_code": plant_code, "nodes": nodes, "edges": edges,
                        "stats": stats, "engine": "NetworkX"})


def _source_record_context(
    conn: sqlite3.Connection, record: dict, batch: dict, requested_plant: str = "GROUP"
) -> dict:
    if requested_plant != "GROUP" and not _plant_visible(record.get("plant_code"), requested_plant):
        raise PermissionError("source record is outside the current factory scope")
    attributes = json.loads(record.get("ext") or "{}")
    provenance = attributes.get("_provenance") if isinstance(attributes.get("_provenance"), dict) else None
    if not provenance:
        provenance, issues = _build_source_provenance(
            record, attributes, record["batch_id"], batch.get("filename") or "历史批次", int(record["id"])
        )
        attributes["_provenance"] = provenance
        attributes["_quality_issues"] = issues
    issues = list(attributes.get("_quality_issues") or _record_quality_issues(record, attributes))
    mappings = _rows(
        conn, "SELECT * FROM mappings WHERE batch_id = ? AND record_id = ? ORDER BY id",
        (record["batch_id"], record["id"]),
    )
    if not mappings:
        mappings = _rows(
            conn,
            """SELECT * FROM mappings WHERE batch_id = ? AND system_source = ? AND original_code = ?
               AND original_name = ? ORDER BY id""",
            (record["batch_id"], record.get("system_source"), record.get("material_code"), record.get("material_name")),
        )
    mdm_codes = list(dict.fromkeys(item["mdm_code"] for item in mappings if item.get("mdm_code")))
    masters, reviews, distributions, feedback = [], [], [], []
    if mdm_codes:
        placeholders = ",".join("?" for _ in mdm_codes)
        masters = _rows(
            conn, f"SELECT * FROM batch_masters WHERE batch_id = ? AND mdm_code IN ({placeholders})",
            [record["batch_id"], *mdm_codes],
        )
        reviews = _rows(
            conn, f"SELECT * FROM reviews WHERE batch_id = ? AND mdm_code IN ({placeholders}) ORDER BY id",
            [record["batch_id"], *mdm_codes],
        )
        if requested_plant != "GROUP":
            masters = [item for item in masters if _plant_visible(item.get("plant_codes"), requested_plant)]
            reviews = [item for item in reviews if _plant_visible(item.get("plant_codes"), requested_plant)]
            distributions = _rows(
                conn, f"""SELECT * FROM distribution_logs WHERE batch_id = ? AND mdm_code IN ({placeholders})
                           AND plant_code = ? ORDER BY id""",
                [record["batch_id"], *mdm_codes, requested_plant],
            )
            feedback = _rows(
                conn, f"""SELECT * FROM plant_feedback WHERE batch_id = ? AND mdm_code IN ({placeholders})
                           AND plant_code = ? ORDER BY id""",
                [record["batch_id"], *mdm_codes, requested_plant],
            )
        else:
            distributions = _rows(
                conn, f"""SELECT * FROM distribution_logs WHERE batch_id = ? AND mdm_code IN ({placeholders})
                           ORDER BY id""", [record["batch_id"], *mdm_codes],
            )
            feedback = _rows(
                conn, f"""SELECT * FROM plant_feedback WHERE batch_id = ? AND mdm_code IN ({placeholders})
                           ORDER BY id""", [record["batch_id"], *mdm_codes],
            )
    for item in distributions:
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
    if requested_plant == "GROUP":
        audit_proofs = _rows(
            conn,
            """SELECT height, event_type, entity_type, entity_id, actor, payload_hash, previous_hash,
                      merkle_root, block_hash, created_at
               FROM audit_blocks WHERE batch_id = ? AND
               (entity_type IN ('BATCH', 'WORKFLOW_STEP') OR entity_id IN ({codes})) ORDER BY height""".format(
                codes=",".join("?" for _ in mdm_codes) or "NULL"
            ),
            [record["batch_id"], *mdm_codes],
        )
    else:
        audit_proofs = _rows(
            conn,
            """SELECT height, event_type, entity_type, entity_id, actor, payload_hash, previous_hash,
                      merkle_root, block_hash, created_at
               FROM audit_blocks WHERE batch_id = ? AND
               (entity_type IN ('BATCH', 'WORKFLOW_STEP') OR
                (entity_type = 'DISTRIBUTION' AND entity_id = ?)) ORDER BY height""",
            (record["batch_id"], requested_plant),
        )
    route = [
        {"type": "SYSTEM", "id": provenance["source_system"], "label": provenance["source_system"]},
        {"type": "SOURCE_RECORD", "id": provenance["source_record_id"], "label": record.get("material_name")},
        *[{"type": "MASTER", "id": item["mdm_code"], "label": item.get("standard_name")} for item in masters],
    ]
    route.extend({
        "type": "TARGET_SYSTEM", "id": item["target_system"],
        "label": f"{item['target_system']} · {PLANTS.get(item['plant_code'], item['plant_code'])}",
    } for item in distributions if item.get("status") == "SUCCESS")
    return {
        "record": {key: record.get(key) for key in (
            "id", "batch_id", "material_code", "system_source", "material_name", "description",
            "category", "unit", "create_time", "plant_code", "created_at",
        )},
        "attributes": {key: value for key, value in attributes.items() if not key.startswith("_")},
        "provenance": provenance, "quality_issues": issues, "mappings": mappings,
        "masters": masters, "reviews": reviews, "distributions": distributions, "feedback": feedback,
        "audit_proofs": audit_proofs, "route": route,
        "fingerprint": {
            "algorithm": "SHA-256", "record_hash": provenance["record_hash"],
            "chain_valid": _verify_audit_chain(conn, record["batch_id"])["valid"],
        },
    }


@app.get("/api/lineage/source/<int:record_id>")
def source_record_lineage(record_id: int):
    requested_plant = _effective_plant_code(request.args.get("plant_code"))
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            return jsonify({"error": "source record not found"}), 404
        record = dict(row)
        batch_row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (record["batch_id"],)).fetchone()
        try:
            context = _source_record_context(conn, record, dict(batch_row), requested_plant)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        return jsonify({**context, "trace_id": g.trace_id})


@app.get("/api/graph/lineage/<mdm_code>")
def graph_lineage(mdm_code: str):
    with db_connect() as conn:
        batch_id = request.args.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"error": "no governed batch is available"}), 400
        requested_plant = _effective_plant_code(request.args.get("plant_code"))
        graph, _stats = _build_governance_graph(conn, batch_id, requested_plant, 300)
        node_id = f"master:{mdm_code}"
        if node_id not in graph:
            return jsonify({"error": "master not found in graph"}), 404
        neighbors = [{"id": neighbor, **graph.nodes[neighbor], "relation": graph.edges[node_id, neighbor]["relation"]}
                     for neighbor in graph.neighbors(node_id)]
        batch_row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        mapping_rows = _rows(
            conn, "SELECT * FROM mappings WHERE batch_id = ? AND mdm_code = ? ORDER BY id", (batch_id, mdm_code)
        )
        source_records = []
        seen_record_ids = set()
        for mapping in mapping_rows:
            record = None
            if mapping.get("record_id"):
                record = conn.execute("SELECT * FROM records WHERE id = ?", (mapping["record_id"],)).fetchone()
            if not record:
                record = conn.execute(
                    """SELECT * FROM records WHERE batch_id = ? AND system_source = ? AND material_code = ?
                       AND material_name = ? ORDER BY id LIMIT 1""",
                    (batch_id, mapping.get("system_source"), mapping.get("original_code"), mapping.get("original_name")),
                ).fetchone()
            if record and record["id"] not in seen_record_ids:
                try:
                    source_records.append(_source_record_context(
                        conn, dict(record), dict(batch_row), requested_plant
                    ))
                    seen_record_ids.add(record["id"])
                except PermissionError:
                    continue
        if requested_plant == "GROUP":
            downstream = _rows(
                conn, "SELECT * FROM distribution_logs WHERE batch_id = ? AND mdm_code = ? ORDER BY id",
                (batch_id, mdm_code),
            )
        else:
            downstream = _rows(
                conn,
                """SELECT * FROM distribution_logs
                   WHERE batch_id = ? AND mdm_code = ? AND plant_code = ? ORDER BY id""",
                (batch_id, mdm_code, requested_plant),
            )
        for item in downstream:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        if requested_plant == "GROUP":
            audit_proofs = _rows(
                conn,
                """SELECT height, event_type, entity_type, entity_id, actor, payload_hash, previous_hash,
                          merkle_root, block_hash, created_at
                   FROM audit_blocks WHERE batch_id = ? AND
                   (entity_type IN ('BATCH', 'WORKFLOW_STEP') OR entity_id = ?) ORDER BY height""",
                (batch_id, mdm_code),
            )
        else:
            audit_proofs = _rows(
                conn,
                """SELECT height, event_type, entity_type, entity_id, actor, payload_hash, previous_hash,
                          merkle_root, block_hash, created_at
                   FROM audit_blocks WHERE batch_id = ? AND
                   (entity_type IN ('BATCH', 'WORKFLOW_STEP') OR
                    (entity_type = 'DISTRIBUTION' AND entity_id = ?)) ORDER BY height""",
                (batch_id, requested_plant),
            )
        return jsonify({
            "batch_id": batch_id, "mdm_code": mdm_code, "neighbors": neighbors,
            "degree": graph.degree(node_id), "source_records": source_records,
            "downstream": downstream, "audit_proofs": audit_proofs,
            "audit_verification": _verify_audit_chain(conn, batch_id), "trace_id": g.trace_id,
        })


@app.get("/api/connectors")
def connector_registry():
    connectors = []
    for key, config in SOURCE_CONNECTORS.items():
        configured = bool(os.environ.get(config.get("env") or "", "")) if config.get("env") else True
        connectors.append({
            "system": key, "connector_id": config["id"], "name": config["label"],
            "source_object": config["table"], "configured": configured,
            "status": "CONNECTED" if configured else "DEMO_ADAPTER",
        })
    return jsonify({"connectors": connectors, "trace_id": g.trace_id})


@app.get("/api/reports/governance")
def governance_report():
    state = get_batch_state(request.args.get("batch_id"), request.args.get("plant_code"))
    if not state.get("batch"):
        return jsonify({"error": "no governed batch is available"}), 400
    batch = state["batch"]
    issue_rows = []
    for record in state["records"]:
        provenance = record.get("_ext", {}).get("_provenance", {})
        for issue in record.get("_ext", {}).get("_quality_issues", []):
            issue_rows.append({
                "record_id": record["id"], "source_system": record.get("system_source"),
                "source_table": provenance.get("source_table"),
                "source_record_id": provenance.get("source_record_id"), "record_hash": provenance.get("record_hash"),
                **issue,
            })
    connector_status = {}
    for source in sorted({_clean_value(item.get("system_source")) or "CSV" for item in state["records"]}):
        profile = _connector_profile(source)
        connector_status[source] = {
            "connector_id": profile["id"], "name": profile["label"],
            "source_object": profile["table"], "configured": bool(profile["base_url"]),
        }
    report = {
        "report_id": f"GOV-{batch['batch_id']}", "generated_at": _utc_now(),
        "batch": {key: batch.get(key) for key in ("batch_id", "filename", "record_count", "created_at", "plant_code")},
        "summary": state["summary"], "quality": state["quality_report"],
        "source_issue_count": len(issue_rows), "source_issues": issue_rows,
        "source_connectors": connector_status, "workflow": state["workflow"],
        "distribution_summary": {
            "total": len(state["distribution_logs"]),
            "success": sum(item.get("status") == "SUCCESS" for item in state["distribution_logs"]),
            "feedback": len(state["feedback"]),
        },
        "audit": state["audit_chain"],
    }
    stable_report = {key: value for key, value in report.items() if key != "generated_at"}
    report["report_hash"] = hashlib.sha256(_canonical_json(stable_report).encode("utf-8")).hexdigest()
    report["hash_scope"] = "报告业务内容（不含生成时间与请求追踪号）"
    return jsonify({**report, "trace_id": g.trace_id})


@app.get("/api/blockchain/blocks")
def audit_blocks():
    with db_connect() as conn:
        batch_id = request.args.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"batch_id": None, "blocks": [], "verification": {"valid": True, "block_count": 0}})
        blocks = _rows(conn, "SELECT * FROM audit_blocks WHERE batch_id = ? ORDER BY height DESC LIMIT 100", (batch_id,))
        for block in blocks:
            block["payload"] = json.loads(block.pop("payload_json") or "{}")
        return jsonify({"batch_id": batch_id, "blocks": blocks, "verification": _verify_audit_chain(conn, batch_id)})


@app.get("/api/blockchain/verify")
def verify_blockchain():
    with db_connect() as conn:
        batch_id = request.args.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"batch_id": None, "valid": True, "block_count": 0, "errors": []})
        return jsonify(_verify_audit_chain(conn, batch_id))


def _request_is_loopback() -> bool:
    remote = (request.remote_addr or "").split("%", 1)[0]
    loopback = {"127.0.0.1", "::1", "localhost"}
    if remote not in loopback:
        return False
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        clients = [item.strip().split("%", 1)[0] for item in forwarded.split(",") if item.strip()]
        if not clients or any(client not in loopback for client in clients):
            return False
    forwarded_header = request.headers.get("Forwarded", "")
    if forwarded_header:
        forwarded_for = re.findall(r"for=(?:\"?\[?)([^\] ;,\"]+)", forwarded_header, flags=re.I)
        if not forwarded_for or any(client.split("%", 1)[0] not in loopback for client in forwarded_for):
            return False
    return True


@app.route("/api/ocr/install", methods=["GET", "POST"])
def api_ocr_install():
    status = ocr_installer.status()
    status["trigger_allowed"] = (
        _request_is_loopback() and status["platform_supported"] and status["auto_install_enabled"]
    )
    if request.method == "GET":
        return jsonify(status)
    if not _request_is_loopback():
        return jsonify({"error": "OCR installation can only be started from the server computer"}), 403
    if not status["auto_install_enabled"]:
        return jsonify({"error": "automatic OCR installation is disabled by MDM_ALLOW_OCR_INSTALL"}), 403
    try:
        started = ocr_installer.start()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    started["trigger_allowed"] = True
    return jsonify(started), 200 if started["runtime_ready"] else 202


@app.post("/api/ocr")
def api_ocr():
    uploaded = request.files.get("image") or request.files.get("file")
    payload = (request.get_json(silent=True) or {}) if request.is_json else request.form.to_dict()
    encoded_image = (payload or {}).get("image")
    if uploaded is None and not encoded_image:
        return jsonify({"error": "image is required"}), 400
    filename = uploaded.filename if uploaded is not None else "base64-image.jpg"
    try:
        if uploaded is not None:
            image_bytes = uploaded.read()
            mime_type = uploaded.mimetype or "image/jpeg"
        else:
            image_bytes, mime_type = ocr_engine.decode_data_image(encoded_image)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    max_ocr_bytes = int(os.getenv("MDM_MAX_OCR_BYTES", str(10 * 1024 * 1024)))
    if not image_bytes:
        return jsonify({"error": "image is empty"}), 400
    if len(image_bytes) > max_ocr_bytes:
        return jsonify({"error": f"image exceeds {max_ocr_bytes} byte OCR limit"}), 413
    hint = _clean_value(payload.get("hint_text") or payload.get("description") or filename)
    recognition = ocr_engine.recognize(image_bytes, filename, mime_type, hint)
    source_text = recognition["raw_text"] or hint
    extracted = engine.enrich({"material_name": source_text, "description": source_text, "category": payload.get("category", "")})
    fields = {key: extracted["_ext"].get(key) or "" for key in ("brand", "model", "pressure", "material", "dn")}
    standard_name = engine.generate_standard_name([{**extracted, "_ext": fields}], extracted["_category"])
    return jsonify({
        "success": True,
        "simulated": not recognition["real_ocr"],
        "real_ocr": recognition["real_ocr"],
        "provider": recognition["provider"],
        "extraction_id": f"OCR-{uuid.uuid4().hex[:10].upper()}",
        "filename": filename,
        "raw_text": recognition["raw_text"],
        "fields": fields,
        **fields,
        "category": extracted["_category"],
        "standard_name_preview": standard_name,
        "confidence": recognition["ocr_confidence"],
        "plant_code": _effective_plant_code(payload.get("plant_code")),
        "warning": recognition["warning"],
        "engine_status": ocr_engine.status(),
        "install_status": ocr_installer.status(),
    })


@app.post("/api/explain")
def explain_governance_decision():
    payload = request.get_json(silent=True) or {}
    mdm_code = _clean_value(payload.get("mdm_code"))
    if not mdm_code:
        return jsonify({"error": "mdm_code is required"}), 400
    with db_connect() as conn:
        batch_id = _clean_value(payload.get("batch_id")) or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"error": "no governed batch is available"}), 400
        master_row = conn.execute(
            "SELECT * FROM batch_masters WHERE batch_id = ? AND mdm_code = ?", (batch_id, mdm_code)
        ).fetchone()
        if not master_row:
            return jsonify({"error": "master not found in batch"}), 404
        master = dict(master_row)
        mapping_rows = _rows(conn, "SELECT * FROM mappings WHERE batch_id = ? AND mdm_code = ? ORDER BY id", (batch_id, mdm_code))
        records = []
        for mapping in mapping_rows:
            record = (
                conn.execute("SELECT * FROM records WHERE id = ?", (mapping["record_id"],)).fetchone()
                if mapping.get("record_id") else None
            )
            if not record:
                record = conn.execute(
                    """SELECT * FROM records WHERE batch_id = ? AND system_source = ?
                       AND material_code = ? AND material_name = ? ORDER BY id LIMIT 1""",
                    (batch_id, mapping.get("system_source"), mapping.get("original_code"), mapping.get("original_name")),
                ).fetchone()
            if record:
                records.append(engine.enrich(dict(record)))
        conflicts = engine.detect_conflicts(records) if records else []
        review = conn.execute(
            "SELECT reason, applied_rules, status FROM reviews WHERE batch_id = ? AND mdm_code = ? ORDER BY id DESC LIMIT 1",
            (batch_id, mdm_code),
        ).fetchone()
    decision_labels = {"AUTO_MERGE": "自动归并", "REVIEW": "人工复核", "NEW": "建议新建", "CONFIRMED_NEW": "审核后新建"}
    evidence = {
        "batch_id": batch_id,
        "master": {key: master.get(key) for key in ("mdm_code", "standard_name", "category", "source_count", "source_systems", "decision", "confidence", "plant_codes")},
        "attributes": {key: master.get(key) or "" for key in ("brand", "model", "dn", "pressure", "material")},
        "decision_label": decision_labels.get(master.get("decision"), master.get("decision") or "未知"),
        "conflicts": conflicts,
        "applied_rules": sorted({rule for item in mapping_rows for rule in _clean_value(item.get("applied_rules")).split("/") if rule}),
        "review_reason": review["reason"] if review else None,
        "sources": [{
            "material_code": item.get("material_code"), "system_source": item.get("system_source"),
            "material_name": item.get("material_name"), "description": item.get("description"),
        } for item in records[:20]],
    }
    explanation, runtime = qwen_agent.explain(evidence, bool(payload.get("use_llm", True)))
    return jsonify({
        "batch_id": batch_id, "mdm_code": mdm_code, "explanation": explanation,
        "evidence": evidence, "runtime": runtime, "trace_id": g.trace_id,
    })


def _normalize_target_system(value: str) -> str:
    text = _clean_value(value).lower()
    for system, aliases in DISTRIBUTION_SYSTEM_ALIASES.items():
        if text == system.lower() or any(alias in text for alias in aliases):
            return system
    return ""


def _extract_distribution_filters(text: str) -> dict:
    filters = {}
    labels = {
        "brand": ("品牌", "brand"),
        "model": ("型号", "model"),
        "material_name": ("备品备件名称", "物料名称", "备件名称", "零件名称", "名称", "name"),
    }
    bracket_items = [
        _clean_value(value) for value in re.findall(r"[【\[]([^】\]]+)[】\]]", text) if _clean_value(value)
    ]
    for field, field_labels in labels.items():
        for item in bracket_items:
            for label in field_labels:
                match = re.match(rf"^{re.escape(label)}\s*[:：=]?\s*(.+)$", item, re.I)
                if match and _clean_value(match.group(1)) not in field_labels:
                    filters[field] = _clean_value(match.group(1))
                    break
            if field in filters:
                break
        if field in filters:
            continue
        label_pattern = "|".join(re.escape(label) for label in field_labels)
        match = re.search(
            rf"(?:{label_pattern})\s*[:：=]?\s*[【\[]?([^】\],，;；]+?)[】\]]?(?=\s*(?:品牌|型号|备品备件名称|物料名称|备件名称|零件名称|发往|同步到|分发到|$))",
            text, re.I,
        )
        if match:
            value = _clean_value(match.group(1))
            if value and value.lower() not in {label.lower() for label in field_labels}:
                filters[field] = value
    before_destination = re.split(r"发往|同步到|分发到|发送到", text, maxsplit=1)[0]
    unnamed = [
        item for item in re.findall(r"[【\[]([^】\]]+)[】\]]", before_destination)
        if not any(label.lower() in item.lower() for values in labels.values() for label in values)
        and not _normalize_target_system(item)
        and not any(alias in item.lower() for alias in PLANT_ALIASES)
    ]
    for field, value in zip(("brand", "model", "material_name"), unnamed):
        filters.setdefault(field, _clean_value(value))
    return filters


def _parse_distribution_intent(text: str, plant_code=None) -> dict:
    lower = _clean_value(text).lower()
    targets = list(DISTRIBUTION_SYSTEM_ALIASES) if any(
        key in lower for key in ("全部系统", "所有系统", "all systems")
    ) else [
        system for system, aliases in DISTRIBUTION_SYSTEM_ALIASES.items()
        if system.lower() in lower or any(alias in lower for alias in aliases)
    ]
    target_plants = []
    for alias, code in sorted(PLANT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in lower and code not in target_plants:
            target_plants.append(code)
    if plant_code:
        explicit_plant = _effective_plant_code(plant_code)
        if explicit_plant not in target_plants:
            target_plants.append(explicit_plant)
    if not target_plants:
        target_plants = ["GROUP"]
    filters = _extract_distribution_filters(text)
    material_part = re.split(r"发往|同步到|分发到|发送到|发到", text, maxsplit=1)[0]
    filters.setdefault("brand", engine.extract_brand(material_part) or "")
    filters.setdefault("model", engine.extract_model(material_part) or "")
    filters = {key: value for key, value in filters.items() if _clean_value(value)}
    mode = "INCREMENTAL" if any(key in lower for key in ("增量", "新增", "刚批准", "最新")) else "FULL"
    confidence = 0.35 + (0.35 if targets else 0) + (0.15 if filters else 0) + (0.1 if target_plants else 0)
    return {
        "text": text, "action": "DISTRIBUTE" if targets else "UNKNOWN",
        "targets": targets, "target_systems": targets, "target_plants": target_plants,
        "plant_code": target_plants[0], "filters": filters, "mode": mode,
        "scope": "FILTERED_APPROVED_MASTERS" if filters else "APPROVED_MASTERS",
        "confidence": round(min(confidence, 0.98), 2),
    }


def _distribution_text_similarity(left, right) -> float:
    """Blend exact, character overlap and edit similarity for short material fields."""
    lhs = engine.standardize_text(_clean_value(left)).lower()
    rhs = engine.standardize_text(_clean_value(right)).lower()
    if not lhs or not rhs:
        return 0.0
    if lhs == rhs:
        return 1.0
    if lhs in rhs or rhs in lhs:
        return 0.92
    left_chars, right_chars = set(lhs), set(rhs)
    union = left_chars | right_chars
    jaccard = len(left_chars & right_chars) / len(union) if union else 0.0
    edit = SequenceMatcher(None, lhs, rhs).ratio()
    return round(min(0.9, jaccard * 0.55 + edit * 0.45), 4)


def _rank_distribution_masters(
    conn: sqlite3.Connection, batch_id: str, filters: dict, target_plants: list[str],
    query_text: str = "", top_k: int = 20,
) -> list[dict]:
    """Return approved masters ranked by explainable fuzzy similarity.

    Plant visibility and approval are hard gates.  User-supplied brand/model/name
    values are ranking signals, so a near match remains discoverable instead of
    disappearing when one spelling or abbreviation differs.
    """
    rows = _rows(
        conn,
        """SELECT * FROM batch_masters WHERE batch_id = ?
           AND decision IN ('AUTO_MERGE', 'NEW', 'CONFIRMED_NEW') ORDER BY rowid""",
        (batch_id,),
    )
    filters = filters or {}
    query_parts = [query_text, *[str(value) for value in filters.values() if value]]
    query = " ".join(query_parts).strip()
    ranked = []
    for row in rows:
        eligible_plants = [
            plant for plant in (target_plants or ["GROUP"])
            if _master_distributable_to_plant(row.get("plant_codes"), plant)
        ]
        if target_plants and not eligible_plants:
            continue
        scores, reasons, explicit_scores = [], [], []
        if filters.get("brand"):
            value = _distribution_text_similarity(filters["brand"], row.get("brand"))
            scores.append((value, 0.20))
            explicit_scores.append(value)
            if value >= 0.86: reasons.append("品牌一致")
            elif value >= 0.55: reasons.append(f"品牌相似 {value:.0%}")
        if filters.get("model"):
            value = _distribution_text_similarity(filters["model"], row.get("model"))
            scores.append((value, 0.35))
            explicit_scores.append(value)
            if value >= 0.86: reasons.append("型号一致")
            elif value >= 0.55: reasons.append(f"型号相似 {value:.0%}")
        if filters.get("material_name"):
            value = _distribution_text_similarity(filters["material_name"], row.get("standard_name"))
            scores.append((value, 0.30))
            explicit_scores.append(value)
            if value >= 0.86: reasons.append("名称一致")
            elif value >= 0.45: reasons.append(f"名称相似 {value:.0%}")
        if filters.get("category"):
            value = _distribution_text_similarity(filters["category"], row.get("category"))
            scores.append((value, 0.15))
            explicit_scores.append(value)
            if value >= 0.7: reasons.append("品类相近")
        if query:
            value = _distribution_text_similarity(query, row.get("standard_name"))
            scores.append((value, 0.10 if scores else 0.35))
            if value >= 0.55: reasons.append(f"整体语义片段相似 {value:.0%}")
        if scores:
            weight = sum(weight for _value, weight in scores)
            score = sum(value * weight for value, weight in scores) / max(weight, 0.01)
        else:
            score = 0.5
        score = round(max(0.0, min(0.99, score)), 4)
        if not reasons:
            reasons.append("可分发黄金主数据")
        if score >= 0.82:
            level = "HIGH"
        elif score >= 0.60:
            level = "MEDIUM"
        else:
            level = "LOW"
        ranked.append({
            "master": row, "similarity": score, "score": score,
            "match_level": level, "reasons": reasons,
            "exact_match": bool(explicit_scores and all(value >= 0.86 for value in explicit_scores)),
            "eligible_plants": eligible_plants,
        })
    ranked.sort(key=lambda item: (-item["similarity"], item["master"].get("mdm_code", "")))
    if filters or query_text:
        ranked = [item for item in ranked if item["similarity"] >= 0.30]
    return ranked[:max(1, int(top_k))]


def _actionable_distribution_ranks(ranked: list[dict], has_filters: bool) -> list[dict]:
    """Keep recommendations broad while limiting executable tasks to credible matches."""
    if not has_filters:
        return ranked
    exact = [item for item in ranked if item.get("exact_match")]
    if exact:
        return exact
    credible = [item for item in ranked if float(item.get("similarity") or 0) >= 0.55]
    return credible or ranked[:1]


def _match_distribution_masters(
    conn: sqlite3.Connection, batch_id: str, filters: dict, target_plants: list[str]
) -> list[dict]:
    """Compatibility wrapper used by existing distribution callers."""
    return [item["master"] for item in _rank_distribution_masters(conn, batch_id, filters, target_plants)]


@app.post("/api/intent")
def api_intent():
    payload = request.get_json(silent=True) or {}
    text = _clean_value(payload.get("text") or payload.get("query") or payload.get("prompt"))
    if not text:
        return jsonify({"error": "text is required"}), 400
    response = _parse_distribution_intent(text, payload.get("plant_code"))
    with db_connect() as conn:
        batch_id = payload.get("batch_id") or _latest_batch_id(conn)
        candidate_ranked = _rank_distribution_masters(
            conn, batch_id, response["filters"], response["target_plants"],
            text if response["filters"] else "",
        ) if batch_id else []
        ranked = _actionable_distribution_ranks(candidate_ranked, bool(response["filters"]))
        matches = [item["master"] for item in ranked]
    explicit_one = bool(re.search(r"(?:一个|一条|最相似|第一条|top\s*1)", text, re.I))
    explicit_all = bool(re.search(r"(?:全部|所有|整批|批量|新增主数据)", text, re.I))
    selection_mode = "TOP_ONE" if explicit_one else ("ALL" if explicit_all else ("EXPLICIT" if len(matches) > 1 else "SINGLE"))
    selected_codes = [item["master"].get("mdm_code") for item in ranked[:1]] if selection_mode == "TOP_ONE" else []
    task_rows = []
    for item in ranked:
        master = item["master"]
        for target_plant in response["target_plants"]:
            eligible = target_plant in item["eligible_plants"]
            for target_system in response["target_systems"]:
                task_signature = f"{batch_id}|{master.get('mdm_code')}|{target_system}|{target_plant}"
                task_rows.append({
                    "task_id": f"TASK-{uuid.uuid5(uuid.NAMESPACE_URL, task_signature).hex[:12].upper()}",
                    "mdm_code": master.get("mdm_code"), "standard_name": master.get("standard_name"),
                    "target_system": target_system, "target_plant": target_plant,
                    "similarity": item["similarity"], "match_level": item["match_level"],
                    "reasons": item["reasons"], "eligible": eligible,
                    "selected": master.get("mdm_code") in selected_codes and eligible,
                    "status": "PENDING_CONFIRMATION" if eligible else "OUT_OF_SCOPE",
                })
    response.update({
        "batch_id": batch_id, "matched_master_count": len(matches),
        "matched_masters": [{
            key: item.get(key) for key in ("mdm_code", "standard_name", "brand", "model", "category", "plant_codes")
        } | {"similarity": ranked[index]["similarity"], "score": ranked[index]["score"],
            "match_level": ranked[index]["match_level"], "reasons": ranked[index]["reasons"],
            "selected": item.get("mdm_code") in selected_codes}
        for index, item in enumerate(matches[:20])],
        "candidate_masters": [{"mdm_code": item["master"].get("mdm_code"), "standard_name": item["master"].get("standard_name"),
                               "similarity": item["similarity"], "match_level": item["match_level"], "reasons": item["reasons"]}
                              for item in candidate_ranked],
        "tasks": task_rows, "selection_mode": selection_mode,
        "selection_required": selection_mode == "EXPLICIT" and len(task_rows) > 1,
        "selected_master_codes": selected_codes,
        "task_count": sum(1 for task in task_rows if task["eligible"]),
        "requires_confirmation": bool(response["target_systems"] and matches),
    })
    response["question"] = (
        f"已匹配 {len(matches)} 条黄金主数据，将生成 {response['task_count']} 个分发任务，是否确认执行？"
        if response["target_systems"] and matches else "请补充可识别的目标系统和物料条件。"
    )
    if not response["targets"]:
        response["warning"] = "未识别到目标系统，请在指令中包含 ERP、SAP、EAM、MES、WMS 或采购平台。"
    elif response["filters"] and not matches:
        response["warning"] = "未找到达到相似度阈值且已批准的黄金主数据。"
    return jsonify(response)


@app.post("/api/governance")
def run_governance_compat():
    payload = request.get_json(silent=True) or {}
    batch_id = payload.get("batch_id")
    if not batch_id:
        return jsonify({"error": "batch_id is required; new clients should POST /api/batches"}), 400
    try:
        return jsonify(get_batch_state(batch_id, payload.get("plant_code")))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/api/masters")
def get_masters():
    state = get_batch_state(request.args.get("batch_id"), request.args.get("plant_code"))
    return jsonify({"batch_id": state["batch"]["batch_id"] if state["batch"] else None, "masters": state["masters"]})


def _search_conditions(query: str) -> dict:
    conditions: dict = {"exclude": []}
    material_terms = [
        "316L", "316", "304L", "304", "2205", "铸钢", "碳钢", "WCB", "Q235", "35CrMoA",
        "哈氏合金", "C276", "钛合金", "碳化硅", "氟橡胶", "FKM", "丁腈橡胶", "NBR",
    ]
    for material in material_terms:
        if re.search(rf"(?:不要|排除|不含|非|without|exclude)(?:的|是)?\s*{re.escape(material)}", query, re.I):
            canonical = engine.extract_material(material) or material
            if canonical not in conditions["exclude"]:
                conditions["exclude"].append(canonical)
    if not conditions["exclude"]:
        material = engine.extract_material(query)
        if material:
            conditions["material"] = material
    brand = engine.extract_brand(query)
    if brand:
        conditions["brand"] = brand
    model = engine.extract_model(query)
    if model:
        conditions["model"] = model
    dn_range = re.search(r"DN\s*(\d+)\s*[~—-]\s*(\d+)", query, re.I)
    if dn_range:
        conditions["dn_range"] = [int(dn_range.group(1)), int(dn_range.group(2))]
    else:
        dn = engine.extract_dn(query)
        if dn:
            conditions["dn"] = dn
    pressure = engine.extract_pressure(query)
    if pressure:
        conditions["pressure"] = pressure
    category = engine.detect_category(engine.standardize_text(query), "")
    if category != "其他":
        conditions["category"] = category
    return conditions


@app.post("/api/search")
def search():
    payload = request.get_json(silent=True) or {}
    query = _clean_value(payload.get("query"))
    if not query:
        return jsonify({"error": "query is required"}), 400
    state = get_batch_state(payload.get("batch_id"), payload.get("plant_code"))
    batch_id = state["batch"]["batch_id"] if state["batch"] else None
    conditions = _search_conditions(query)
    for item in payload.get("conditions") or []:
        if not isinstance(item, dict) or not item.get("type"):
            continue
        condition_type, value = item["type"], item.get("value")
        if condition_type == "exclude":
            canonical = engine.extract_material(value) or engine.standardize_text(value)
            if canonical and canonical not in conditions["exclude"]:
                conditions["exclude"].append(canonical)
        elif condition_type == "material":
            canonical = engine.extract_material(value) or _clean_value(value)
            current = _clean_value(conditions.get("material"))
            if canonical and (not current or (canonical.lower().endswith("l") and not current.lower().endswith("l"))):
                conditions["material"] = canonical
        elif condition_type in {"brand", "model", "dn", "pressure", "category", "dn_range", "pressure_range", "qty_min"}:
            conditions[condition_type] = value
    match_mode = _clean_value(payload.get("match_mode") or "and").lower()
    if match_mode not in {"and", "or", "fuzzy"}:
        match_mode = "and"
    try:
        min_confidence = float(payload.get("min_confidence") or 0)
    except (TypeError, ValueError):
        min_confidence = 0.0
    results, suggestions = [], []
    query_text = engine.standardize_text(query)
    query_grams = {query_text[index:index + 2] for index in range(max(0, len(query_text) - 1))}
    master_semantic_texts = [
        engine.standardize_text(" ".join(_clean_value(master.get(key)) for key in (
            "standard_name", "category", "model", "brand", "dn", "pressure", "material"
        ))) for master in state["masters"]
    ]
    search_vectors, search_meta = semantic_engine.resolve_embeddings(
        [query_text, *master_semantic_texts], _clean_value(payload.get("model")) or "qwen"
    )
    for master_index, master in enumerate(state["masters"]):
        if float(master.get("confidence") or 0) < min_confidence:
            continue
        text = " ".join(_clean_value(master.get(key)) for key in ("standard_name", "category", "model", "brand", "dn", "pressure", "material")).lower()
        if any(engine.standardize_text(item) in engine.standardize_text(text) for item in conditions["exclude"]):
            continue
        score, reasons, required, matched = 0.0, [], 0, 0
        for key, weight, label in [
            ("model", 0.50, "型号一致"), ("brand", 0.25, "品牌一致"), ("dn", 0.20, "口径一致"),
            ("pressure", 0.20, "压力一致"), ("material", 0.20, "材质一致"), ("category", 0.30, "品类一致"),
        ]:
            if key in conditions:
                required += 1
                left, right = _clean_value(conditions[key]).lower(), _clean_value(master.get(key)).lower()
                if left == right or (key == "category" and (left in right or right in left)):
                    score += weight
                    reasons.append(label)
                    matched += 1
        if "dn_range" in conditions:
            required += 1
            try:
                dn_value = int(master.get("dn") or -1)
                if conditions["dn_range"][0] <= dn_value <= conditions["dn_range"][1]:
                    score += 0.20
                    reasons.append("口径范围内")
                    matched += 1
            except ValueError:
                pass
        if "pressure_range" in conditions:
            required += 1
            try:
                pressure_value = float(re.sub(r"[^0-9.]", "", master.get("pressure") or ""))
                pressure_range = [float(re.sub(r"[^0-9.]", "", str(value))) for value in conditions["pressure_range"]]
                if pressure_range[0] <= pressure_value <= pressure_range[1]:
                    score += 0.20
                    reasons.append("压力范围内")
                    matched += 1
            except (TypeError, ValueError):
                pass
        if "qty_min" in conditions:
            required += 1
            if int(master.get("source_count") or 0) >= int(conditions["qty_min"]):
                score += 0.10
                reasons.append("来源数量满足")
                matched += 1
        name_text = engine.standardize_text(text)
        overlap = 0.0
        if query_grams:
            overlap = sum(gram in name_text for gram in query_grams) / len(query_grams)
            score += min(0.20, overlap * 0.20)
            if overlap:
                reasons.append("语义片段命中")
        semantic_score = (
            semantic_engine.cosine_similarity(search_vectors[0], search_vectors[master_index + 1])
            if search_vectors is not None else semantic_engine.jaccard_similarity(query_text, master_semantic_texts[master_index])
        )
        if semantic_score >= 0.45:
            score += min(0.35, semantic_score * 0.35)
            reasons.append(f"AI语义相似 {semantic_score:.0%}")
        if required and match_mode == "and" and matched != required:
            match_ratio = matched / required
            if matched and match_ratio >= 0.5:
                suggestions.append({
                    "master": master,
                    "score": round(min(score * 0.75 + match_ratio * 0.20, 0.89), 4),
                    "reasons": reasons + [f"满足 {matched}/{required} 个条件"],
                    "matched_conditions": matched,
                    "required_conditions": required,
                })
            continue
        if required and match_mode == "or" and matched == 0:
            continue
        if required and match_mode == "fuzzy" and matched == 0:
            # Fuzzy mode is allowed to keep a semantic-only candidate.  This is
            # deliberately stricter than a normal result and remains a suggestion
            # when no structured attribute is exact.
            if semantic_score < 0.45 and overlap < 0.18:
                continue
        if match_mode == "fuzzy" and score > 0:
            score = score * 1.15 + 0.03
        if score > 0:
            results.append({"master": master, "score": round(min(score, 0.99), 4), "reasons": reasons})
    results.sort(key=lambda item: item["score"], reverse=True)
    suggestions.sort(key=lambda item: (item["matched_conditions"] / item["required_conditions"], item["score"]), reverse=True)
    with db_connect() as conn:
        knowledge_references = _search_standard_kb(conn, query, 3)
        conn.execute("INSERT INTO search_history (batch_id, query) VALUES (?, ?)", (batch_id, query))
    public_conditions = [{"type": key, "value": value} for key, value in conditions.items() if value]
    return jsonify({"query": query, "conditions": public_conditions, "match_mode": match_mode,
                    "semantic": search_meta, "plant_code": (state.get("batch") or {}).get("view_plant_code", "GROUP"),
                    "knowledge_references": knowledge_references,
                    "knowledge_retrieval": {"namespace": "standard_kb", "top_k": 3, "access_filtered": True},
                    "results": results[:20], "total": len(results),
                    "similar_candidates": (results[:20] if results else suggestions[:10]),
                    "suggestions": suggestions[:10] if not results else [],
                    "suggestion_total": len(suggestions) if not results else 0})


@app.delete("/api/search-history")
def clear_search_history():
    with db_connect() as conn:
        conn.execute("DELETE FROM search_history")
    return jsonify({"cleared": True})


@app.get("/api/reviews")
def get_reviews():
    state = get_batch_state(request.args.get("batch_id"), request.args.get("plant_code"))
    return jsonify({"reviews": state["reviews"]})


@app.patch("/api/reviews/<int:review_id>")
def decide_review(review_id: int):
    payload = request.get_json(silent=True) or {}
    action = _clean_value(payload.get("action")).upper()
    actor_plant = _effective_plant_code(payload.get("plant_code"))
    if action not in {"MERGE", "NEW", "SKIP"}:
        return jsonify({"error": "action must be MERGE, NEW, or SKIP"}), 400
    with db_connect() as conn:
        review = conn.execute("SELECT * FROM reviews WHERE id = ? AND status = 'REVIEW'", (review_id,)).fetchone()
        if not review:
            return jsonify({"error": "review not found"}), 404
        if actor_plant != "GROUP" and not _plant_visible(review["plant_codes"], actor_plant):
            return jsonify({"error": "review is outside the actor plant scope"}), 403
        decision = "AUTO_MERGE" if action == "MERGE" else "NEW"
        if action != "SKIP":
            conn.execute("UPDATE masters SET decision = ?, confidence = ?, updated_at = ? WHERE mdm_code = ?", (decision, 0.95 if action == "MERGE" else 1.0, _utc_now(), review["mdm_code"]))
            conn.execute("UPDATE batch_masters SET decision = ?, confidence = ? WHERE batch_id = ? AND mdm_code = ?", (decision, 0.95 if action == "MERGE" else 1.0, review["batch_id"], review["mdm_code"]))
            conn.execute("UPDATE mappings SET decision = ?, applied_rules = ? WHERE batch_id = ? AND mdm_code = ?", (decision, "R005-审核批准" if action == "MERGE" else "R007-审核批准新建", review["batch_id"], review["mdm_code"]))
        conn.execute("UPDATE reviews SET status = ?, approved_action = ?, approved_at = ? WHERE id = ?", ("SKIPPED" if action == "SKIP" else "APPROVED", action, _utc_now(), review_id))
        batch_id = review["batch_id"]
        pending = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE batch_id = ? AND status = 'REVIEW'", (batch_id,)
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM reviews WHERE batch_id = ?", (batch_id,)).fetchone()[0]
        _set_workflow_step(
            conn, batch_id, "REVIEW", "COMPLETED" if pending == 0 else "ACTION_REQUIRED",
            100 if pending == 0 else round((total - pending) / max(1, total) * 100),
            {"pending": pending, "resolved": total - pending, "total": total},
        )
        _append_audit_block(conn, batch_id, "REVIEW_DECIDED", "MASTER", review["mdm_code"], {
            "review_id": review_id, "action": action, "decision": decision if action != "SKIP" else "SKIPPED",
            "plant_code": actor_plant, "pending_reviews": pending,
        }, actor=_current_actor(_clean_value(payload.get("actor")) or f"{actor_plant}审核人"))
    return jsonify(get_batch_state(batch_id))


@app.post("/api/lifecycle")
def create_lifecycle():
    payload = request.get_json(silent=True) or {}
    for field in ("name", "category", "reason"):
        if not _clean_value(payload.get(field)):
            return jsonify({"error": f"{field} is required"}), 400
    with db_connect() as conn:
        batch_id = payload.get("batch_id") or _latest_batch_id(conn)
        plant_code = _effective_plant_code(payload.get("plant_code"))
        cursor = conn.execute(
            """INSERT INTO lifecycle
               (batch_id, request_id, name, mdm_code, category, brand, model, description, reason,
                status, creator, change_of, plant_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, payload.get("request_id") or f"REQ-{uuid.uuid4().hex[:10].upper()}", payload["name"],
             payload.get("mdm_code", ""), payload["category"], payload.get("brand", ""), payload.get("model", ""),
             payload.get("description", ""), payload["reason"], "PENDING",
             _current_actor(payload.get("creator", "申请人")),
             payload.get("change_of", ""), plant_code),
        )
        if batch_id:
            _append_audit_block(conn, batch_id, "LIFECYCLE_CREATED", "LIFECYCLE", str(cursor.lastrowid), {
                "request_id": payload.get("request_id"), "name": payload["name"], "category": payload["category"],
                "plant_code": plant_code, "status": "PENDING",
            }, actor=_current_actor(_clean_value(payload.get("creator")) or "申请人"))
    return jsonify({"id": cursor.lastrowid, "status": "PENDING"}), 201


@app.patch("/api/lifecycle/<int:lifecycle_id>")
def update_lifecycle(lifecycle_id: int):
    payload = request.get_json(silent=True) or {}
    status = _clean_value(payload.get("status")).upper()
    if status not in {"PENDING", "REVIEWED", "APPROVED", "REJECTED", "ARCHIVED"}:
        return jsonify({"error": "invalid lifecycle status"}), 400
    now = _utc_now()
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM lifecycle WHERE id = ?", (lifecycle_id,)).fetchone()
        if not row:
            return jsonify({"error": "lifecycle request not found"}), 404
        current_status = _clean_value(row["status"]).upper() or "PENDING"
        transitions = {
            "PENDING": {"REVIEWED", "REJECTED"},
            "REVIEWED": {"APPROVED", "REJECTED"},
            "APPROVED": {"ARCHIVED"},
            "REJECTED": set(),
            "ARCHIVED": set(),
        }
        required_reviewers = {
            ("PENDING", "REVIEWED"): {"数据管理员"},
            ("PENDING", "REJECTED"): {"数据管理员"},
            ("REVIEWED", "APPROVED"): {"审批人"},
            ("REVIEWED", "REJECTED"): {"审批人"},
            ("APPROVED", "ARCHIVED"): {"数据管理员", "审批人"},
        }
        reviewer = _current_actor(_clean_value(payload.get("reviewer")))
        if status != current_status:
            if status not in transitions.get(current_status, set()):
                return jsonify({"error": f"invalid lifecycle transition: {current_status} -> {status}"}), 409
            if enterprise_security.mode == "enterprise":
                required_permission = (
                    "lifecycle.review" if current_status == "PENDING" else "lifecycle.approve"
                )
                if not enterprise_security.has_permission(g.principal, required_permission):
                    return jsonify({"error": f"permission required: {required_permission}",
                                    "code": "PERMISSION_DENIED"}), 403
            else:
                allowed_reviewers = required_reviewers.get((current_status, status), set())
                if allowed_reviewers and reviewer not in allowed_reviewers:
                    return jsonify({
                        "error": f"{current_status} -> {status} requires reviewer role: {' / '.join(sorted(allowed_reviewers))}"
                    }), 403
        mdm_code = row["mdm_code"] or ""
        if status == "APPROVED" and (not mdm_code or mdm_code.startswith("NEW-")):
            prefix = engine.CATEGORY_PREFIX.get(row["category"], "MDM-X")
            signature = f"{row['category']}|{row['name']}|{row['request_id']}"
            mdm_code = f"{prefix}-{hashlib.sha1(signature.encode('utf-8')).hexdigest()[:8].upper()}"
            enriched = engine.enrich({
                "material_name": row["name"], "description": row["description"], "category": row["category"]
            })
            attributes = dict(enriched["_ext"])
            attributes["brand"] = _clean_value(row["brand"]) or attributes["brand"]
            attributes["model"] = _clean_value(row["model"]) or attributes["model"]
            material_name = _clean_value(row["name"])
            name_parts = [attributes["brand"], attributes["model"], material_name]
            if _clean_value(row["category"]) not in material_name:
                name_parts.append(_clean_value(row["category"]))
            standard_name = " ".join(dict.fromkeys(part for part in name_parts if part))
            values = (
                mdm_code, standard_name, row["category"], attributes["model"], attributes["brand"], attributes["dn"],
                attributes["pressure"], attributes["material"], 0, "lifecycle", "NEW", 1.0, prefix, 0,
                row["plant_code"], now,
            )
            conn.execute(
                """INSERT INTO masters
                   (mdm_code, standard_name, category, model, brand, dn, pressure, material, source_count,
                    source_systems, decision, confidence, code_prefix, anomaly_count, plant_codes, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mdm_code) DO UPDATE SET standard_name=excluded.standard_name,
                    plant_codes=excluded.plant_codes, updated_at=excluded.updated_at""",
                values,
            )
            if row["batch_id"]:
                conn.execute(
                    """INSERT INTO batch_masters
                       (batch_id, mdm_code, standard_name, category, model, brand, dn, pressure, material,
                        source_count, source_systems, decision, confidence, code_prefix, anomaly_count, source_records, plant_codes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(batch_id, mdm_code) DO UPDATE SET standard_name=excluded.standard_name""",
                    (row["batch_id"], mdm_code, standard_name, row["category"], attributes["model"], attributes["brand"],
                     attributes["dn"], attributes["pressure"], attributes["material"], 0, "lifecycle", "NEW", 1.0,
                     prefix, 0, row["request_id"], row["plant_code"]),
                )
        conn.execute(
            """UPDATE lifecycle SET status = ?, mdm_code = ?, reviewer = ?, reviewed_at = ?, archived_at = ? WHERE id = ?""",
            (status, mdm_code, reviewer or row["reviewer"] or "", now, now if status == "ARCHIVED" else None, lifecycle_id),
        )
        batch_id = row["batch_id"]
        if batch_id:
            vector_meta = _index_batch_vectors(conn, batch_id, "local") if status == "APPROVED" else None
            _append_audit_block(conn, batch_id, "LIFECYCLE_TRANSITION", "LIFECYCLE", str(lifecycle_id), {
                "from": current_status, "to": status, "mdm_code": mdm_code,
                "plant_code": row["plant_code"], "vector_refresh": vector_meta,
            }, actor=reviewer or "工作流引擎")
    return jsonify(get_batch_state(batch_id))


@app.post("/api/distribute")
def distribute():
    payload = request.get_json(silent=True) or {}
    instruction = _clean_value(payload.get("instruction") or payload.get("text") or payload.get("prompt"))
    intent = _parse_distribution_intent(instruction, payload.get("plant_code")) if instruction else None
    selected_tasks = payload.get("selected_tasks") or []
    if selected_tasks and not isinstance(selected_tasks, list):
        return jsonify({"error": "selected_tasks must be a list"}), 400
    selected_task_codes = [
        item.get("mdm_code") for item in selected_tasks if isinstance(item, dict) and item.get("mdm_code")
    ]
    raw_targets = payload.get("target_systems") or (intent or {}).get("target_systems") or []
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    targets, unsupported_targets = [], []
    for raw_target in raw_targets:
        target = _normalize_target_system(raw_target)
        if not target:
            unsupported_targets.append(_clean_value(raw_target))
        elif target not in targets:
            targets.append(target)
    if unsupported_targets:
        return jsonify({
            "error": "unsupported target system", "unsupported": unsupported_targets,
            "supported_systems": list(DISTRIBUTION_SYSTEM_ALIASES),
        }), 400
    master_codes = payload.get("selected_master_codes") or payload.get("master_codes") or selected_task_codes or [
        item.get("mdm_code") for item in payload.get("masters", []) if item.get("mdm_code")
    ]
    if isinstance(master_codes, str):
        master_codes = [master_codes]
    master_codes = list(dict.fromkeys(filter(None, master_codes)))
    if not targets:
        return jsonify({"error": "target_systems or a recognizable distribution instruction is required"}), 400
    mode = _clean_value(payload.get("mode") or (intent or {}).get("mode") or "FULL").upper()
    raw_plants = payload.get("target_plants") or (intent or {}).get("target_plants")
    if not raw_plants:
        raw_plants = [payload.get("plant_code") or "GROUP"]
    if isinstance(raw_plants, str):
        raw_plants = [raw_plants]
    target_plants = list(dict.fromkeys(_effective_plant_code(item) for item in raw_plants))
    unsupported_plants = [item for item in target_plants if item not in PLANTS]
    if unsupported_plants:
        return jsonify({"error": "unsupported plant_code", "unsupported": unsupported_plants, "supported_plants": PLANTS}), 400
    filters = payload.get("filters") or (intent or {}).get("filters") or {}
    if instruction and payload.get("confirmed") is not True:
        return jsonify({
            "error": "distribution plan requires confirmation", "requires_confirmation": True,
            "intent": intent, "confirmation_hint": "resubmit with confirmed: true",
        }), 409
    logs = []
    with db_connect() as conn:
        batch_id = payload.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"error": "no governed batch is available"}), 400
        if not master_codes:
            ranked = _rank_distribution_masters(
                conn, batch_id, filters, target_plants,
                instruction if filters else "",
            )
            ranked = _actionable_distribution_ranks(ranked, bool(filters))
            if instruction and len(ranked) > 1 and not re.search(
                r"(?:全部|所有|整批|批量|新增主数据|一个|一条|最相似|第一条|top\s*1)", instruction, re.I
            ) and not selected_tasks:
                return jsonify({
                    "error": "multiple masters matched; select one or more tasks before execution",
                    "code": "MASTER_SELECTION_REQUIRED", "requires_selection": True,
                    "candidates": [{"mdm_code": item["master"].get("mdm_code"), "standard_name": item["master"].get("standard_name"),
                                    "similarity": item["similarity"], "reasons": item["reasons"]} for item in ranked[:20]],
                }), 409
            master_codes = [item["master"]["mdm_code"] for item in ranked]
        master_codes = list(dict.fromkeys(filter(None, master_codes)))
        if not master_codes:
            return jsonify({"error": "no approved master data is available for distribution"}), 400
        placeholders = ",".join("?" for _ in master_codes)
        rows = _rows(
            conn,
            f"""SELECT mdm_code, standard_name, category, model, brand, dn, pressure, material,
                       decision, plant_codes FROM batch_masters
                WHERE batch_id = ? AND mdm_code IN ({placeholders})""",
            [batch_id, *master_codes],
        )
        known = {row["mdm_code"]: row for row in rows if row["decision"] in {"AUTO_MERGE", "NEW", "CONFIRMED_NEW"}}
        unknown_codes = [code for code in master_codes if code not in known]
        if unknown_codes:
            return jsonify({"error": "selected master is not approved in this batch", "unknown_master_codes": unknown_codes}), 400
        execution_tasks = []
        if selected_tasks:
            for task in selected_tasks:
                if not isinstance(task, dict):
                    return jsonify({"error": "selected_tasks entries must be objects"}), 400
                code = _clean_value(task.get("mdm_code"))
                target = _normalize_target_system(task.get("target_system"))
                plant = _effective_plant_code(task.get("target_plant"))
                if code not in known or not target or target not in targets or plant not in target_plants:
                    return jsonify({"error": "selected task is outside the confirmed plan", "task": task}), 400
                signature = f"{batch_id}|{code}|{target}|{plant}"
                expected_id = f"TASK-{uuid.uuid5(uuid.NAMESPACE_URL, signature).hex[:12].upper()}"
                if task.get("task_id") and task.get("task_id") != expected_id:
                    return jsonify({"error": "selected task fingerprint is invalid", "task_id": task.get("task_id")}), 400
                if not _master_distributable_to_plant(known[code]["plant_codes"], plant):
                    return jsonify({"error": "selected task is outside the factory scope", "task_id": task.get("task_id")}), 403
                execution_tasks.append((plant, target, code))
            master_codes = list(dict.fromkeys(code for _plant, _target, code in execution_tasks))
        else:
            execution_tasks = [(plant_code, target, code) for plant_code in target_plants for target in targets for code in master_codes]
        artifacts = []
        for plant_code, target, code in execution_tasks:
            master = known.get(code)
            allowed = bool(master and _master_distributable_to_plant(master["plant_codes"], plant_code))
            status = "SUCCESS" if allowed else "FAILED"
            adapter_payload = {
                        "event_id": f"DIST-{uuid.uuid4().hex[:12].upper()}",
                        "target_system": target, "target_plant": plant_code,
                        "mdm_code": code, "material_name": (master or {}).get("standard_name", ""),
                        "brand": (master or {}).get("brand", ""), "model": (master or {}).get("model", ""),
                        "category": (master or {}).get("category", ""), "dn": (master or {}).get("dn", ""),
                        "pressure": (master or {}).get("pressure", ""), "material": (master or {}).get("material", ""),
                        "mode": mode, "issued_at": _utc_now(),
                    }
            adapter_payload["payload_hash"] = hashlib.sha256(
                _canonical_json(adapter_payload).encode("utf-8")
            ).hexdigest()
            message = "分发适配器已生成可交付载荷" if allowed else "主数据未批准或不在当前工厂范围"
            cursor = conn.execute(
                        """INSERT INTO distribution_logs
                           (batch_id, target_system, mdm_code, standard_name, sync_mode, sync_frequency,
                            status, message, plant_code, instruction, payload_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (batch_id, target, code, (master or {}).get("standard_name", ""), mode, "MANUAL",
                         status, message, plant_code, instruction,
                         json.dumps(adapter_payload, ensure_ascii=False)),
                    )
            log = {
                        "id": cursor.lastrowid, "target_system": target, "mdm_code": code,
                        "standard_name": (master or {}).get("standard_name", ""), "sync_mode": mode,
                        "sync_frequency": "MANUAL", "status": status, "message": message,
                        "plant_code": plant_code, "instruction": instruction, "payload": adapter_payload,
                    }
            logs.append(log)
            if allowed:
                artifacts.append({
                            "artifact_id": adapter_payload["event_id"], "target_system": target,
                            "plant_code": plant_code, "mdm_code": code,
                            "payload_hash": adapter_payload["payload_hash"], "status": "DELIVERED",
                })
        success_count = sum(item["status"] == "SUCCESS" for item in logs)
        failed_count = sum(item["status"] == "FAILED" for item in logs)
        if success_count:
            _set_workflow_step(conn, batch_id, "DISTRIBUTE", "COMPLETED", 100, {
                "success_count": success_count, "failed_count": failed_count,
                "plant_codes": target_plants, "target_systems": targets,
            })
            _set_workflow_step(conn, batch_id, "FEEDBACK", "ACTION_REQUIRED", 0, {
                "feedback_count": conn.execute("SELECT COUNT(*) FROM plant_feedback WHERE batch_id = ?", (batch_id,)).fetchone()[0],
                "plant_codes": target_plants,
            })
        for plant_code in target_plants:
            plant_artifacts = [item for item in artifacts if item["plant_code"] == plant_code]
            _append_audit_block(conn, batch_id, "MASTER_DISTRIBUTED", "DISTRIBUTION", plant_code, {
                "target_systems": targets, "master_codes": master_codes,
                "selected_tasks": execution_tasks,
                "artifact_hashes": [item["payload_hash"] for item in plant_artifacts],
                "success_count": len(plant_artifacts),
                "failed_count": sum(item["plant_code"] == plant_code and item["status"] == "FAILED" for item in logs),
                "mode": mode, "instruction": instruction, "filters": filters,
            }, actor=_current_actor(_clean_value(payload.get("actor")) or "分发Agent"))
    primary_plant = target_plants[0]
    return jsonify({
        "simulated": True, "intent": intent, "plant_code": primary_plant,
        "plant_name": PLANTS[primary_plant], "target_plants": target_plants,
        "target_systems": targets, "filters": filters, "master_codes": master_codes,
        "logs": logs, "artifacts": artifacts, "selected_task_count": len(execution_tasks),
        "success_count": success_count,
        "failed_count": failed_count,
        "workflow_endpoint": f"/api/workflow/{batch_id}",
    })


@app.post("/api/feedback")
def submit_plant_feedback():
    payload = request.get_json(silent=True) or {}
    mdm_code = _clean_value(payload.get("mdm_code"))
    if not mdm_code:
        return jsonify({"error": "mdm_code is required"}), 400
    plant_code = _effective_plant_code(payload.get("plant_code"))
    if plant_code not in PLANTS or plant_code == "GROUP":
        return jsonify({"error": "plant_code must identify a factory", "supported_plants": PLANTS}), 400
    try:
        rating = int(payload.get("rating", 5))
    except (TypeError, ValueError):
        return jsonify({"error": "rating must be an integer from 1 to 5"}), 400
    if rating < 1 or rating > 5:
        return jsonify({"error": "rating must be between 1 and 5"}), 400
    accepted_value = payload.get("accepted", True)
    if not isinstance(accepted_value, bool):
        return jsonify({"error": "accepted must be a JSON boolean"}), 400
    accepted = accepted_value
    with db_connect() as conn:
        batch_id = payload.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"error": "no governed batch is available"}), 400
        master = conn.execute(
            "SELECT * FROM batch_masters WHERE batch_id = ? AND mdm_code = ?", (batch_id, mdm_code)
        ).fetchone()
        if not master:
            return jsonify({"error": "master not found in batch"}), 404
        if not _master_distributable_to_plant(master["plant_codes"], plant_code):
            return jsonify({"error": "master is outside the factory scope"}), 403
        distributed = conn.execute(
            """SELECT 1 FROM distribution_logs WHERE batch_id = ? AND mdm_code = ?
               AND plant_code = ? AND status = 'SUCCESS' LIMIT 1""", (batch_id, mdm_code, plant_code)
        ).fetchone()
        if not distributed:
            return jsonify({"error": "feedback requires a successful factory distribution first"}), 409
        now = _utc_now()
        cursor = conn.execute(
            """INSERT INTO plant_feedback
               (batch_id, plant_code, mdm_code, accepted, rating, comment, actor, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, plant_code, mdm_code, int(accepted), rating, _clean_value(payload.get("comment")),
             _current_actor(_clean_value(payload.get("actor")) or "工厂数据管理员"), now),
        )
        vector_meta = _index_batch_vectors(conn, batch_id, "local")
        feedback_count = conn.execute("SELECT COUNT(*) FROM plant_feedback WHERE batch_id = ?", (batch_id,)).fetchone()[0]
        expected_pairs = {
            (item["plant_code"], item["mdm_code"]) for item in _rows(
                conn,
                """SELECT DISTINCT plant_code, mdm_code FROM distribution_logs
                   WHERE batch_id = ? AND status = 'SUCCESS' AND plant_code != 'GROUP'""",
                (batch_id,),
            )
        }
        completed_pairs = {
            (item["plant_code"], item["mdm_code"]) for item in _rows(
                conn,
                """SELECT DISTINCT plant_code, mdm_code FROM plant_feedback
                   WHERE batch_id = ? AND accepted = 1""",
                (batch_id,),
            )
        }
        pending_pairs = sorted(expected_pairs - completed_pairs)
        feedback_complete = bool(expected_pairs) and not pending_pairs
        feedback_progress = round(len(expected_pairs & completed_pairs) / max(1, len(expected_pairs)) * 100)
        _set_workflow_step(conn, batch_id, "FEEDBACK", "COMPLETED" if feedback_complete else "ACTION_REQUIRED",
                           feedback_progress, {
            "feedback_count": feedback_count, "latest_plant": plant_code,
            "expected_factory_master_pairs": len(expected_pairs), "pending_pairs": pending_pairs,
            "vector_refresh": {"indexed": vector_meta["indexed"], "model": vector_meta["model"]},
        })
        block = _append_audit_block(conn, batch_id, "FACTORY_FEEDBACK", "MASTER", mdm_code, {
            "feedback_id": cursor.lastrowid, "plant_code": plant_code, "accepted": accepted,
            "rating": rating, "comment": _clean_value(payload.get("comment")),
            "knowledge_refresh": vector_meta,
        }, actor=_current_actor(_clean_value(payload.get("actor")) or f"{PLANTS[plant_code]}数据管理员"))
        workflow = _workflow_payload(conn, batch_id)
    return jsonify({
        "id": cursor.lastrowid, "batch_id": batch_id, "plant_code": plant_code, "mdm_code": mdm_code,
        "accepted": accepted, "rating": rating, "knowledge_refresh": vector_meta,
        "audit_block": block, "workflow": workflow, "closed_loop": workflow["closed_loop"],
    }), 201


@app.delete("/api/distribution-logs")
def clear_distribution_logs():
    with db_connect() as conn:
        conn.execute("DELETE FROM distribution_logs")
    return jsonify({"cleared": True})


@app.delete("/api/data")
def clear_all_data():
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "DELETE_ALL_DATA":
        return jsonify({"error": "confirmation token is required"}), 400
    with db_connect() as conn:
        held = _rows(
            conn,
            """SELECT batch_id, filename, data_classification FROM batches
                 WHERE legal_hold = 1 ORDER BY created_at""",
        )
        if held:
            return jsonify({
                "error": "data purge is blocked by active legal hold", "code": "LEGAL_HOLD_ACTIVE",
                "blocked_batches": held,
            }), 409
        for table in (
            "audit_blocks", "plant_feedback", "workflow_steps",
            "distribution_logs", "lifecycle", "search_history", "quality_reports", "reviews",
            "mappings", "batch_masters", "records", "batches", "masters",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM vector_embeddings WHERE batch_id IS NOT NULL OR namespace != 'standard_kb'")
        _seed_standard_kb(conn)
    enterprise_security.record_event(
        "BUSINESS_DATA_PURGED", "SUCCESS", "all-business-data", {"knowledge_preserved": True},
        getattr(g, "principal", None), g.trace_id, request.remote_addr or "",
    )
    g.security_event_recorded = True
    return jsonify({"cleared": True, "knowledge_preserved": True, "security_audit_preserved": True})


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({"error": "upload exceeds configured size limit"}), 413


@app.errorhandler(500)
def internal_error(error):
    logger.exception("unhandled request error")
    return jsonify({"error": "internal server error"}), 500


def _rag_env_number(name: str, default, converter):
    try:
        return converter(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("invalid %s; using %s", name, default)
        return default


rag_config = RAGConfig.from_mapping({
    "dimension": _rag_env_number("MDM_RAG_DIMENSION", 384, int),
    "rule_weight": _rag_env_number("MDM_RAG_RULE_WEIGHT", 0.45, float),
    "lexical_weight": _rag_env_number("MDM_RAG_LEXICAL_WEIGHT", 0.30, float),
    "vector_weight": _rag_env_number("MDM_RAG_VECTOR_WEIGHT", 0.25, float),
    "auto_accept_threshold": _rag_env_number("MDM_RAG_AUTO_ACCEPT_THRESHOLD", 0.68, float),
    "minimum_margin": _rag_env_number("MDM_RAG_MINIMUM_MARGIN", 0.08, float),
    "default_top_k": _rag_env_number("MDM_RAG_TOP_K", 3, int),
    "audit_queries": os.environ.get("MDM_RAG_AUDIT_QUERIES", "1") != "0",
})
os.environ.setdefault("MDM_SECURITY_DIR", str(PROJECT_DIR / "runtime" / "data"))
enterprise_governance = EnterpriseGovernance()
enterprise_security = EnterpriseSecurity(app, db_connect, DB_PATH, logger, PLANTS)
enterprise_rag = EnterpriseRAG(DB_PATH, config=rag_config)
init_db()


def _bootstrap_enterprise_rag() -> None:
    if os.environ.get("MDM_RAG_ENABLED", "1") == "0" or os.environ.get("MDM_RAG_AUTO_IMPORT", "1") == "0":
        return
    if enterprise_rag.stats().get("count"):
        return
    source = Path(os.environ.get(
        "MDM_RAG_SOURCE_PATH", str(PROJECT_DIR / "SY_T5497-2018备品备件分类树(1).xlsx")
    )).expanduser()
    if not source.is_absolute():
        source = PROJECT_DIR / source
    try:
        imported = enterprise_rag.import_xlsx(
            source, version_label=os.environ.get("MDM_RAG_VERSION", "2018"), actor="system-bootstrap",
            notes="M-AI Master enterprise standard bootstrap",
            allowed_plants=[item.strip().upper() for item in os.environ.get("MDM_RAG_ALLOWED_PLANTS", "*").split(",") if item.strip()],
            allowed_classifications=[item.strip().upper() for item in os.environ.get(
                "MDM_RAG_ALLOWED_CLASSIFICATIONS", "INTERNAL,CONFIDENTIAL,RESTRICTED"
            ).split(",") if item.strip()],
            security_classification=os.environ.get("MDM_RAG_CLASSIFICATION", "INTERNAL"),
            expected_counts={"entries": 72, "aliases": 12, "references": 10},
        )
        status = imported["status"]
        version_id = imported["version_id"]
        if status == "DRAFT":
            imported = enterprise_rag.validate_version(version_id, actor="system-bootstrap")
            status = imported["status"]
        if status == "VALIDATED":
            enterprise_rag.publish_version(version_id, actor="system-bootstrap")
        elif status == "RETIRED":
            enterprise_rag.rollback_version(version_id, actor="system-bootstrap")
        logger.info("enterprise RAG ready: %s", enterprise_rag.stats())
    except Exception:
        logger.exception("enterprise RAG bootstrap failed; legacy standard index remains available")


_bootstrap_enterprise_rag()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    host = os.environ.get("MDM_HOST", "0.0.0.0")
    port = int(os.environ.get("MDM_PORT", "5000"))
    production = os.environ.get("MDM_PRODUCTION") == "1"
    print("M-AI Master Flask backend")
    print(f"Listening on http://{host}:{port}")
    print(f"Security mode: {enterprise_security.mode}")
    if enterprise_security.initial_credentials_path.is_file():
        print(f"Initial enterprise credentials: {enterprise_security.initial_credentials_path}")
        print("Change all generated passwords after first sign-in; do not share this file.")
    if production:
        from waitress import serve

        threads = max(2, int(os.environ.get("MDM_THREADS", "8")))
        logger.info("starting Waitress host=%s port=%s threads=%s database=%s", host, port, threads, DB_PATH)
        serve(app, host=host, port=port, threads=threads, channel_timeout=120)
    else:
        app.run(debug=debug, host=host, port=port)
