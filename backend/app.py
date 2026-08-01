"""M-AI Master Flask backend.

Flask and SQLite are the authoritative runtime. The browser keeps an IndexedDB
cache only so the existing single-page UI can render and export data quickly.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import chardet
import networkx as nx
import numpy as np
import pandas as pd
import requests
from flask import Flask, g, jsonify, request, send_from_directory
from sklearn.ensemble import IsolationForest


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


def _config_path(name: str, default: Path) -> Path:
    path = Path(os.environ.get(name, str(default))).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


FRONTEND_DIR = _config_path("MDM_FRONTEND_DIR", BASE_DIR)
FRONTEND_FILE = os.environ.get("MDM_FRONTEND_FILE", "index.html")
DB_PATH = _config_path("MDM_DB_PATH", BASE_DIR / "mdm_data.db")
MAX_RECORDS = int(os.environ.get("MDM_MAX_RECORDS", "10000"))
APP_VERSION = "4.1"
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


@app.before_request
def attach_trace_context():
    g.trace_id = request.headers.get("X-Trace-Id") or f"TRC-{uuid.uuid4().hex[:16].upper()}"
    g.request_started = time.perf_counter()
    if AUTH_PASSWORD and request.path != "/api/health":
        auth = request.authorization
        valid_user = bool(auth) and hmac.compare_digest(auth.username or "", AUTH_USER)
        valid_password = bool(auth) and hmac.compare_digest(auth.password or "", AUTH_PASSWORD)
        if not (valid_user and valid_password):
            response = jsonify({"error": "authentication required", "trace_id": g.trace_id})
            response.status_code = 401
            response.headers["WWW-Authenticate"] = 'Basic realm="M-AI Master", charset="UTF-8"'
            return response


@app.after_request
def attach_trace_headers(response):
    elapsed_ms = round((time.perf_counter() - getattr(g, "request_started", time.perf_counter())) * 1000, 2)
    response.headers["X-Trace-Id"] = getattr(g, "trace_id", "")
    response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        logger.info(
            "%s %s status=%s duration_ms=%s trace_id=%s remote=%s",
            request.method, request.path, response.status_code, elapsed_ms,
            getattr(g, "trace_id", ""), request.remote_addr or "-",
        )
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
PLANT_ALIASES = {
    "集团": "GROUP", "集团总部": "GROUP", "总部": "GROUP", "group": "GROUP", "hq": "GROUP",
    "上海": "SHANGHAI", "上海工厂": "SHANGHAI", "shanghai": "SHANGHAI", "sh": "SHANGHAI",
    "北京": "BEIJING", "北京工厂": "BEIJING", "beijing": "BEIJING", "bj": "BEIJING",
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
        return alias
    code = re.sub(r"[^A-Z0-9_-]", "", raw.upper())
    return code or default


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
        record["plant_code"] = _normalize_plant_code(record.get("plant_code"), default_plant_code)
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
                semantic_warning TEXT
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
            """
        )

        # Non-destructive migration for databases shipped with earlier builds.
        for table, columns in {
            "batches": {
                "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'", "semantic_method": "TEXT",
                "semantic_model": "TEXT", "semantic_dimension": "INTEGER", "semantic_warning": "TEXT",
            },
            "records": {"create_time": "TEXT", "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'"},
            "masters": {
                "anomaly_count": "INTEGER NOT NULL DEFAULT 0", "updated_at": "TEXT",
                "plant_codes": "TEXT NOT NULL DEFAULT 'GROUP'",
            },
            "batch_masters": {"plant_codes": "TEXT NOT NULL DEFAULT 'GROUP'"},
            "mappings": {"batch_id": "TEXT", "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'"},
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
                "plant_code": "TEXT NOT NULL DEFAULT 'GROUP'", "instruction": "TEXT",
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
            """
        )
        _migrate_legacy_batches(conn)
        conn.execute("UPDATE lifecycle SET status = UPPER(status) WHERE status IS NOT NULL")
        for table in ("batches", "records", "mappings", "lifecycle", "distribution_logs"):
            conn.execute(f"UPDATE {table} SET plant_code = 'GROUP' WHERE plant_code IS NULL OR plant_code = ''")
        for table in ("masters", "batch_masters", "reviews"):
            conn.execute(f"UPDATE {table} SET plant_codes = 'GROUP' WHERE plant_codes IS NULL OR plant_codes = ''")

        # Product upgrades must make historical batches usable without forcing a CSV re-upload.
        for batch in conn.execute("SELECT batch_id, record_count, filename FROM batches ORDER BY id").fetchall():
            batch_id = batch["batch_id"]
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
            ("钛合金", r"钛合金|钛材|titanium|ti"), ("316L", r"316l|sus316l"),
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
        self, records: list[dict], semantic=None, preferred_model: str = "qwen"
    ) -> tuple[list[dict], list[dict], list[dict], dict]:
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
                local_match = min(score, 1.0) >= self.SIMILARITY_THRESHOLD
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
    conn.execute(
        """INSERT INTO workflow_steps
           (batch_id, step_code, ordinal, name, status, progress, metrics, action_endpoint, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(batch_id, step_code) DO UPDATE SET status=excluded.status,
            progress=excluded.progress, metrics=excluded.metrics, updated_at=excluded.updated_at""",
        (batch_id, code, ordinal, name, status, max(0, min(100, int(progress))),
         json.dumps(metrics or {}, ensure_ascii=False), endpoint, _utc_now()),
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
        expected_previous = block["block_hash"]
    return {
        "batch_id": batch_id, "valid": not errors, "block_count": len(blocks),
        "latest_hash": expected_previous if blocks else None, "errors": errors,
        "chain_type": "本地联盟链式审计账本", "public_chain": False,
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


qwen_agent = QwenGovernanceAgent()


def _build_governance_graph(
    conn: sqlite3.Connection, batch_id: str, plant_code: str = "GROUP", raw_limit: int = 80
) -> tuple[nx.Graph, dict]:
    graph = nx.Graph(batch_id=batch_id)
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
        for source in filter(None, _clean_value(master["source_systems"]).split(",")):
            source_id = f"system:{source}"
            graph.add_node(source_id, label=source, node_type="SYSTEM", entity_id=source)
            graph.add_edge(master_id, source_id, relation="SOURCED_FROM")
        for plant in filter(None, _clean_value(master["plant_codes"]).split(",")):
            plant = _normalize_plant_code(plant)
            plant_id = f"plant:{plant}"
            graph.add_node(plant_id, label=PLANTS.get(plant, plant), node_type="PLANT", entity_id=plant)
            graph.add_edge(master_id, plant_id, relation="AVAILABLE_TO")
    mappings = conn.execute(
        "SELECT * FROM mappings WHERE batch_id = ? ORDER BY id LIMIT ?", (batch_id, max(0, raw_limit))
    ).fetchall()
    for mapping in mappings:
        if mapping["mdm_code"] not in visible_codes:
            continue
        raw_id = f"raw:{mapping['id']}"
        graph.add_node(raw_id, label=mapping["original_name"] or mapping["original_code"] or raw_id,
                       node_type="RAW", entity_id=mapping["original_code"])
        graph.add_edge(raw_id, f"master:{mapping['mdm_code']}", relation="MAPPED_TO",
                       similarity=round(float(mapping["similarity"] or 0), 4))
    stats = {
        "nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
        "connected_components": nx.number_connected_components(graph) if graph.number_of_nodes() else 0,
        "types": {},
    }
    for _node, data in graph.nodes(data=True):
        stats["types"][data["node_type"]] = stats["types"].get(data["node_type"], 0) + 1
    return graph, stats


def analyze_quality(records: list[dict], anomaly_count: int = 0) -> dict:
    if not records:
        return {"overall": {"score": 0, "recordCount": 0}, "systems": {}, "issues": [], "suggestions": []}
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
    systems = {}
    for system in sorted({record.get("system_source") or "未分类" for record in records}):
        subset = [record for record in records if (record.get("system_source") or "未分类") == system]
        systems[system] = {"recordCount": len(subset), "score": metrics["score"]}
    issues = []
    if completeness < 90:
        issues.append({"level": "high", "text": f"关键字段完整率为 {completeness:.1f}%", "action": "补齐物料编码、名称、描述、分类和计量单位。"})
    if anomaly_count:
        issues.append({"level": "mid", "text": f"Isolation Forest 识别 {anomaly_count} 条结构异常记录", "action": "在人工审核中复核异常记录。"})
    return {"overall": metrics, "systems": systems, "issues": issues, "suggestions": list(dict.fromkeys(item["action"] for item in issues))}


def _batch_id() -> str:
    return f"BATCH-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"


def persist_batch(filename: str, encoding: str, records: list[dict], preferred_model: str = "qwen") -> dict:
    batch_id = _batch_id()
    created_at = _utc_now()
    masters, reviews, mappings, semantic_meta = engine.govern(records, semantic_engine, preferred_model)
    plant_codes = sorted({_normalize_plant_code(record.get("plant_code")) for record in records})
    batch_plant = plant_codes[0] if len(plant_codes) == 1 else "MULTI"
    record_attributes = [engine.enrich(record)["_ext"] for record in records]
    anomaly_count = sum(master.get("anomaly_count", 0) for master in masters)
    quality_report = analyze_quality(records, anomaly_count)
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO batches
               (batch_id, filename, created_at, encoding, record_count, plant_code,
                semantic_method, semantic_model, semantic_dimension, semantic_warning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, filename or "uploaded.csv", created_at, encoding or "utf-8", len(records), batch_plant,
             semantic_meta["method"], semantic_meta["model"], semantic_meta["dimension"], semantic_meta["warning"]),
        )
        conn.executemany(
            """INSERT INTO records
               (batch_id, material_code, system_source, material_name, description, category, unit, create_time, plant_code, ext)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(batch_id, record["material_code"], record["system_source"], record["material_name"], record["description"],
              record["category"], record["unit"], record["create_time"], record["plant_code"],
              json.dumps(attributes, ensure_ascii=False))
             for record, attributes in zip(records, record_attributes)],
        )
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
        conn.executemany(
            """INSERT INTO mappings
               (batch_id, system_source, original_code, original_name, mdm_code, standard_name, decision,
                similarity, applied_rules, plant_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(batch_id, item["system_source"], item["original_code"], item["original_name"], item["mdm_code"],
              item["standard_name"], item["decision"], item["similarity"], item["applied_rules"], item["plant_code"])
             for item in mappings],
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
    requested_plant = _normalize_plant_code(plant_code, "") if plant_code else ""
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
        records = _rows(conn, "SELECT * FROM records WHERE batch_id = ? ORDER BY id", (batch_id,))
        if requested_plant and requested_plant != "GROUP":
            records = [item for item in records if _plant_visible(item.get("plant_code"), requested_plant)]
        for record in records:
            record["_ext"] = json.loads(record.pop("ext") or "{}")
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
            masters = [item for item in masters if _plant_visible(item.get("plant_codes"), requested_plant)]
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


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, FRONTEND_FILE)


@app.get("/api/health")
def health_check():
    database_error = ""
    try:
        with db_connect() as conn:
            conn.execute("SELECT 1").fetchone()
        database_ready = True
    except Exception as exc:
        database_ready = False
        database_error = type(exc).__name__
        logger.exception("database readiness check failed")
    payload = {
        "status": "ok" if database_ready else "degraded", "ready": database_ready,
        "version": APP_VERSION, "storage": "sqlite", "database": DB_PATH.name,
        "deployment": "production" if os.environ.get("MDM_PRODUCTION") == "1" else "development",
        "authentication": bool(AUTH_PASSWORD), "database_error": database_error,
        "semantic": {
            "primary": "qwen", "model": SemanticEngine.MODELS["qwen"]["model"],
            "dimension": SemanticEngine.MODELS["qwen"]["dimension"],
            "configured_models": semantic_engine.configured_models(),
        },
        "plants": PLANTS,
        "capabilities": {
            "llm_agent": bool(qwen_agent.api_key), "vector_store": True, "knowledge_graph": True,
            "audit_blockchain": True, "closed_loop_workflow": True,
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
        plant_code = _normalize_plant_code(payload.get("plant_code"))
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
        records = normalize_records(frame.to_dict("records"), default_plant_code=_normalize_plant_code(request.form.get("plant_code")))
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
        candidates.append({
            "category": category, "score": round(min(0.99, semantic_score * 0.21 + rule_bonus), 4),
            "code_prefix": engine.CATEGORY_PREFIX.get(category, "MDM-X"),
            "reason": "标准规则命中 + AI语义匹配" if rule_bonus else "AI语义候选",
        })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    recommended = candidates[0]
    public_attributes = engine.presentation_attributes(enriched, recommended["category"])
    preview = engine.generate_standard_name([{**enriched, "_ext": public_attributes}], recommended["category"])
    return jsonify({
        "standard": "SY/T5497-2018", "recommended_category": recommended["category"],
        "confidence": recommended["score"], "code_prefix": recommended["code_prefix"],
        "attributes": public_attributes, "standard_name_preview": preview,
        "candidates": candidates[:3], "semantic": metadata,
        "plant_code": _normalize_plant_code(payload.get("plant_code")),
    })


@app.get("/api/agent/capabilities")
def agent_capabilities():
    return jsonify({
        "agent": "M-AI Master", "version": "4.0",
        "workflow": [
            {"step": ordinal, "code": code, "name": name, "endpoint": endpoint}
            for code, ordinal, name, endpoint in WORKFLOW_DEFINITION
        ],
        "semantic_models": SemanticEngine.MODELS,
        "configured_models": semantic_engine.configured_models(),
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
    context = {"plant_code": _normalize_plant_code(payload.get("plant_code")), "trace_id": g.trace_id}
    if batch_id:
        try:
            state = get_batch_state(batch_id)
            context.update({"batch_id": batch_id, "summary": state["summary"], "workflow": state["workflow"]})
        except LookupError:
            return jsonify({"error": "batch not found"}), 404
    plan, runtime = qwen_agent.plan(task, context)
    return jsonify({"plan": plan, "runtime": runtime, "trace_id": g.trace_id})


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
                                 _normalize_plant_code(payload.get("plant_code")),
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
        plant_code = _normalize_plant_code(request.args.get("plant_code"))
        graph, stats = _build_governance_graph(conn, batch_id, plant_code, raw_limit)
        positions = nx.spring_layout(graph, seed=5497, iterations=35) if graph.number_of_nodes() else {}
        centrality = nx.degree_centrality(graph) if graph.number_of_nodes() > 1 else {node: 0 for node in graph.nodes}
        nodes = [{"id": node, **data, "x": round(float(positions[node][0]), 5),
                  "y": round(float(positions[node][1]), 5), "centrality": round(float(centrality[node]), 5)}
                 for node, data in graph.nodes(data=True)]
        edges = [{"source": left, "target": right, **data} for left, right, data in graph.edges(data=True)]
        return jsonify({"batch_id": batch_id, "plant_code": plant_code, "nodes": nodes, "edges": edges,
                        "stats": stats, "engine": "NetworkX"})


@app.get("/api/graph/lineage/<mdm_code>")
def graph_lineage(mdm_code: str):
    with db_connect() as conn:
        batch_id = request.args.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"error": "no governed batch is available"}), 400
        graph, _stats = _build_governance_graph(conn, batch_id, _normalize_plant_code(request.args.get("plant_code")), 300)
        node_id = f"master:{mdm_code}"
        if node_id not in graph:
            return jsonify({"error": "master not found in graph"}), 404
        neighbors = [{"id": neighbor, **graph.nodes[neighbor], "relation": graph.edges[node_id, neighbor]["relation"]}
                     for neighbor in graph.neighbors(node_id)]
        return jsonify({"batch_id": batch_id, "mdm_code": mdm_code, "neighbors": neighbors,
                        "degree": graph.degree(node_id), "trace_id": g.trace_id})


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


@app.post("/api/ocr")
def api_ocr():
    uploaded = request.files.get("image") or request.files.get("file")
    payload = (request.get_json(silent=True) or {}) if request.is_json else request.form.to_dict()
    encoded_image = (payload or {}).get("image")
    if uploaded is None and not encoded_image:
        return jsonify({"error": "image is required"}), 400
    filename = uploaded.filename if uploaded is not None else "base64-image"
    hint = _clean_value(payload.get("hint_text") or payload.get("description") or filename)
    extracted = engine.enrich({"material_name": hint, "description": hint, "category": payload.get("category", "")})
    defaults = {"brand": "SKF", "model": "6312-2RS1", "pressure": "1.6MPa", "material": "316L", "dn": ""}
    fields = {key: extracted["_ext"].get(key) or value for key, value in defaults.items()}
    standard_name = engine.generate_standard_name([{**extracted, "_ext": fields}], extracted["_category"])
    return jsonify({
        "success": True,
        "simulated": True,
        "provider": "mock-ocr-with-rule-validation",
        "extraction_id": f"OCR-{uuid.uuid4().hex[:10].upper()}",
        "filename": filename,
        "fields": fields,
        **fields,
        "category": extracted["_category"],
        "standard_name_preview": standard_name,
        "confidence": 0.96,
        "plant_code": _normalize_plant_code(payload.get("plant_code")),
        "warning": "当前为可替换的OCR模拟适配器，字段已通过主数据规则引擎标准化。",
    })


def _parse_distribution_intent(text: str, plant_code=None) -> dict:
    lower = _clean_value(text).lower()
    rules = {
        "SAP": ("sap", "erp", "财务系统"),
        "EAM": ("eam", "设备系统", "资产系统"),
        "MES": ("mes", "制造系统", "生产系统"),
        "WMS": ("wms", "仓储系统", "库存系统", "仓库系统"),
    }
    targets = list(rules) if any(k in lower for k in ("全部系统", "所有系统", "all systems")) else [
        target for target, keywords in rules.items() if any(keyword in lower for keyword in keywords)
    ]
    detected_plant = _normalize_plant_code(plant_code)
    for alias, code in PLANT_ALIASES.items():
        if alias in lower:
            detected_plant = code
            break
    mode = "INCREMENTAL" if any(k in lower for k in ("增量", "新增", "刚批准", "最新")) else "FULL"
    return {
        "text": text, "action": "DISTRIBUTE" if targets else "UNKNOWN",
        "targets": targets, "target_systems": targets, "plant_code": detected_plant,
        "mode": mode, "scope": "APPROVED_MASTERS", "confidence": 0.96 if targets else 0.35,
    }


@app.post("/api/intent")
def api_intent():
    payload = request.get_json(silent=True) or {}
    text = _clean_value(payload.get("text") or payload.get("query") or payload.get("prompt"))
    if not text:
        return jsonify({"error": "text is required"}), 400
    response = _parse_distribution_intent(text, payload.get("plant_code"))
    if not response["targets"]:
        response["warning"] = "未识别到目标系统，请在指令中包含 SAP、EAM、MES 或 WMS。"
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
        if required and match_mode in {"or", "fuzzy"} and matched == 0:
            continue
        if match_mode == "fuzzy" and score > 0:
            score = score * 1.15 + 0.03
        if score > 0:
            results.append({"master": master, "score": round(min(score, 0.99), 4), "reasons": reasons})
    results.sort(key=lambda item: item["score"], reverse=True)
    suggestions.sort(key=lambda item: (item["matched_conditions"] / item["required_conditions"], item["score"]), reverse=True)
    with db_connect() as conn:
        conn.execute("INSERT INTO search_history (batch_id, query) VALUES (?, ?)", (batch_id, query))
    public_conditions = [{"type": key, "value": value} for key, value in conditions.items() if value]
    return jsonify({"query": query, "conditions": public_conditions, "match_mode": match_mode,
                    "semantic": search_meta, "plant_code": (state.get("batch") or {}).get("view_plant_code", "GROUP"),
                    "results": results[:20], "total": len(results),
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
    actor_plant = _normalize_plant_code(payload.get("plant_code"))
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
        }, actor=_clean_value(payload.get("actor")) or f"{actor_plant}审核人")
    return jsonify(get_batch_state(batch_id))


@app.post("/api/lifecycle")
def create_lifecycle():
    payload = request.get_json(silent=True) or {}
    for field in ("name", "category", "reason"):
        if not _clean_value(payload.get(field)):
            return jsonify({"error": f"{field} is required"}), 400
    with db_connect() as conn:
        batch_id = payload.get("batch_id") or _latest_batch_id(conn)
        plant_code = _normalize_plant_code(payload.get("plant_code"))
        cursor = conn.execute(
            """INSERT INTO lifecycle
               (batch_id, request_id, name, mdm_code, category, brand, model, description, reason,
                status, creator, change_of, plant_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, payload.get("request_id") or f"REQ-{uuid.uuid4().hex[:10].upper()}", payload["name"],
             payload.get("mdm_code", ""), payload["category"], payload.get("brand", ""), payload.get("model", ""),
             payload.get("description", ""), payload["reason"], "PENDING", payload.get("creator", "申请人"),
             payload.get("change_of", ""), plant_code),
        )
        if batch_id:
            _append_audit_block(conn, batch_id, "LIFECYCLE_CREATED", "LIFECYCLE", str(cursor.lastrowid), {
                "request_id": payload.get("request_id"), "name": payload["name"], "category": payload["category"],
                "plant_code": plant_code, "status": "PENDING",
            }, actor=_clean_value(payload.get("creator")) or "申请人")
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
        reviewer = _clean_value(payload.get("reviewer"))
        if status != current_status:
            if status not in transitions.get(current_status, set()):
                return jsonify({"error": f"invalid lifecycle transition: {current_status} -> {status}"}), 409
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
    targets = payload.get("target_systems") or (intent or {}).get("target_systems") or []
    master_codes = payload.get("master_codes") or [item.get("mdm_code") for item in payload.get("masters", []) if item.get("mdm_code")]
    if not targets:
        return jsonify({"error": "target_systems or a recognizable distribution instruction is required"}), 400
    mode = _clean_value(payload.get("mode") or (intent or {}).get("mode") or "FULL").upper()
    plant_code = _normalize_plant_code((intent or {}).get("plant_code") or payload.get("plant_code"))
    if plant_code not in PLANTS:
        return jsonify({"error": f"unsupported plant_code: {plant_code}", "supported_plants": PLANTS}), 400
    logs = []
    with db_connect() as conn:
        batch_id = payload.get("batch_id") or _latest_batch_id(conn)
        if not batch_id:
            return jsonify({"error": "no governed batch is available"}), 400
        if not master_codes and instruction:
            rows = conn.execute(
                """SELECT mdm_code FROM batch_masters
                   WHERE batch_id = ? AND decision IN ('AUTO_MERGE', 'NEW', 'CONFIRMED_NEW')""",
                (batch_id,),
            ).fetchall()
            master_codes = [row["mdm_code"] for row in rows]
        if not master_codes:
            return jsonify({"error": "no approved master data is available for distribution"}), 400
        placeholders = ",".join("?" for _ in master_codes)
        rows = conn.execute(
            f"""SELECT mdm_code, standard_name, decision, plant_codes FROM batch_masters
                WHERE batch_id = ? AND mdm_code IN ({placeholders})""",
            [batch_id, *master_codes],
        ).fetchall()
        known = {
            row["mdm_code"]: row["standard_name"] for row in rows
            if row["decision"] in {"AUTO_MERGE", "NEW", "CONFIRMED_NEW"}
            and _master_distributable_to_plant(row["plant_codes"], plant_code)
        }
        for target in targets:
            for code in master_codes:
                status = "SUCCESS" if code in known else "FAILED"
                message = "模拟适配器已生成接口载荷" if status == "SUCCESS" else "主数据未批准或不在当前工厂范围"
                cursor = conn.execute(
                    """INSERT INTO distribution_logs
                       (batch_id, target_system, mdm_code, standard_name, sync_mode, sync_frequency,
                        status, message, plant_code, instruction)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (batch_id, target, code, known.get(code, ""), mode, "MANUAL", status, message,
                     plant_code, instruction),
                )
                logs.append({"id": cursor.lastrowid, "target_system": target, "mdm_code": code,
                             "standard_name": known.get(code, ""), "sync_mode": mode, "sync_frequency": "MANUAL",
                             "status": status, "message": message, "plant_code": plant_code,
                             "instruction": instruction})
        success_count = sum(item["status"] == "SUCCESS" for item in logs)
        failed_count = sum(item["status"] == "FAILED" for item in logs)
        if success_count:
            _set_workflow_step(conn, batch_id, "DISTRIBUTE", "COMPLETED", 100, {
                "success_count": success_count, "failed_count": failed_count,
                "plant_code": plant_code, "target_systems": targets,
            })
            _set_workflow_step(conn, batch_id, "FEEDBACK", "ACTION_REQUIRED", 0, {
                "feedback_count": conn.execute("SELECT COUNT(*) FROM plant_feedback WHERE batch_id = ?", (batch_id,)).fetchone()[0],
                "plant_code": plant_code,
            })
        _append_audit_block(conn, batch_id, "MASTER_DISTRIBUTED", "DISTRIBUTION", plant_code, {
            "target_systems": targets, "master_count": len(master_codes), "success_count": success_count,
            "failed_count": failed_count, "mode": mode, "instruction": instruction,
        }, actor=_clean_value(payload.get("actor")) or "分发Agent")
    return jsonify({
        "simulated": True, "intent": intent, "plant_code": plant_code,
        "plant_name": PLANTS[plant_code], "logs": logs,
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
    plant_code = _normalize_plant_code(payload.get("plant_code"))
    if plant_code not in PLANTS or plant_code == "GROUP":
        return jsonify({"error": "plant_code must identify a factory", "supported_plants": PLANTS}), 400
    try:
        rating = int(payload.get("rating", 5))
    except (TypeError, ValueError):
        return jsonify({"error": "rating must be an integer from 1 to 5"}), 400
    if rating < 1 or rating > 5:
        return jsonify({"error": "rating must be between 1 and 5"}), 400
    accepted = bool(payload.get("accepted", True))
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
             _clean_value(payload.get("actor")) or "工厂数据管理员", now),
        )
        vector_meta = _index_batch_vectors(conn, batch_id, "local")
        feedback_count = conn.execute("SELECT COUNT(*) FROM plant_feedback WHERE batch_id = ?", (batch_id,)).fetchone()[0]
        _set_workflow_step(conn, batch_id, "FEEDBACK", "COMPLETED", 100, {
            "feedback_count": feedback_count, "latest_plant": plant_code,
            "vector_refresh": {"indexed": vector_meta["indexed"], "model": vector_meta["model"]},
        })
        block = _append_audit_block(conn, batch_id, "FACTORY_FEEDBACK", "MASTER", mdm_code, {
            "feedback_id": cursor.lastrowid, "plant_code": plant_code, "accepted": accepted,
            "rating": rating, "comment": _clean_value(payload.get("comment")),
            "knowledge_refresh": vector_meta,
        }, actor=_clean_value(payload.get("actor")) or f"{PLANTS[plant_code]}数据管理员")
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
        for table in (
            "audit_blocks", "plant_feedback", "workflow_steps", "vector_embeddings",
            "distribution_logs", "lifecycle", "search_history", "quality_reports", "reviews",
            "mappings", "batch_masters", "records", "batches", "masters",
        ):
            conn.execute(f"DELETE FROM {table}")
    return jsonify({"cleared": True})


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({"error": "upload exceeds configured size limit"}), 413


@app.errorhandler(500)
def internal_error(error):
    logger.exception("unhandled request error")
    return jsonify({"error": "internal server error"}), 500


init_db()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    host = os.environ.get("MDM_HOST", "0.0.0.0")
    port = int(os.environ.get("MDM_PORT", "5000"))
    production = os.environ.get("MDM_PRODUCTION") == "1"
    print("M-AI Master Flask backend")
    print(f"Listening on http://{host}:{port}")
    if production:
        from waitress import serve

        threads = max(2, int(os.environ.get("MDM_THREADS", "8")))
        logger.info("starting Waitress host=%s port=%s threads=%s database=%s", host, port, threads, DB_PATH)
        serve(app, host=host, port=port, threads=threads, channel_timeout=120)
    else:
        app.run(debug=debug, host=host, port=port)
