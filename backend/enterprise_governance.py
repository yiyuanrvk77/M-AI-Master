"""DMBOK-aligned governance catalog and data-quality issue workflow."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EnterpriseGovernance:
    """Persist governance ownership, metadata, controls, and quality work items."""

    CONTROL_SEEDS = (
        ("DG-01", "数据治理", "治理组织与职责", "明确数据所有者、审批人、数据管理员和审计员的职责分离。",
         "GROUP_ADMIN", "GROUP_APPROVER", "角色职责覆盖率", "100%", "IMPLEMENTED", 3, "/api/admin/users"),
        ("DG-02", "数据治理", "治理控制与度量", "以可验证控制、负责人、成熟度和证据管理治理计划。",
         "GROUP_ADMIN", "AUDITOR", "已监控控制占比", ">=80%", "IMPLEMENTED", 3, "/api/governance/catalog"),
        ("DG-03", "数据治理", "业务术语与定义", "维护物料主数据关键术语、定义、责任人和标准引用。",
         "GROUP_APPROVER", "PLANT_STEWARD", "已发布关键术语覆盖率", "100%", "IMPLEMENTED", 3, "/api/governance/catalog"),
        ("MD-01", "元数据管理", "元数据目录", "统一管理业务、技术、操作和安全元数据。",
         "GROUP_APPROVER", "PLANT_STEWARD", "关键字段入册率", "100%", "IMPLEMENTED", 3, "/api/governance/catalog"),
        ("DQ-01", "数据质量", "质量规则与度量口径", "对完整性、一致性、唯一性、标准化和可追溯性定义可复核规则。",
         "GROUP_APPROVER", "PLANT_STEWARD", "启用规则数", ">=8", "IMPLEMENTED", 3, "/api/governance/catalog"),
        ("DQ-02", "数据质量", "问题整改闭环", "质量问题具有来源、责任角色、期限、状态和整改证据。",
         "GROUP_APPROVER", "PLANT_STEWARD", "逾期未关闭问题数", "0", "IMPLEMENTED", 3, "/api/governance/issues"),
        ("MDM-01", "参考数据和主数据", "黄金主数据与映射", "保留源记录到黄金编码的映射、决策和版本化批次证据。",
         "GROUP_APPROVER", "PLANT_STEWARD", "源记录映射覆盖率", "100%", "IMPLEMENTED", 4, "/api/graph"),
        ("MDM-02", "参考数据和主数据", "标准知识版本治理", "标准知识导入、校验、发布、回滚和引用均可审计。",
         "GROUP_APPROVER", "GROUP_ADMIN", "正式版本可验证率", "100%", "IMPLEMENTED", 4, "/api/knowledge/versions"),
        ("INT-01", "数据集成与互操作", "来源血缘与数据契约", "记录来源系统、业务主键、源表、连接器状态和内容指纹。",
         "GROUP_APPROVER", "PLANT_STEWARD", "来源记录可追溯率", "100%", "IMPLEMENTED", 4, "/api/reports/governance"),
        ("INT-02", "数据集成与互操作", "企业连接器交付", "真实 ERP/EAM/MES/WMS 连接器需完成鉴权、幂等、重试和验收。",
         "GROUP_ADMIN", "GROUP_ADMIN", "生产连接器验收数", ">=1", "DESIGNED", 2, "/api/connectors"),
        ("SEC-01", "数据安全", "身份、权限与主体范围", "服务端强制执行账户、角色、工厂范围、CSRF 和最小权限。",
         "GROUP_ADMIN", "AUDITOR", "越权测试通过率", "100%", "MONITORED", 4, "/api/security/audit/verify"),
        ("SEC-02", "数据安全", "分类、出域与保留", "按数据分级控制外部 AI 出域、保留期限和法律保全。",
         "GROUP_ADMIN", "AUDITOR", "受限数据违规出域数", "0", "MONITORED", 4, "/api/compliance/status"),
        ("OPS-01", "数据存储和运营", "备份恢复与持续性", "形成加密备份、定期恢复演练和恢复点/恢复时间证据。",
         "GROUP_ADMIN", "GROUP_ADMIN", "恢复演练成功率", "100%", "DESIGNED", 1, "待企业部署后验证"),
        ("LC-01", "数据生命周期", "保留、处置与法律保全", "到期处置先预演，法律保全优先于删除，并保留安全审计证据。",
         "GROUP_ADMIN", "AUDITOR", "保全对象误删数", "0", "MONITORED", 4, "/api/compliance/retention/run"),
    )

    METADATA_SEEDS = (
        ("material_code", "物料编码", "源系统中的物料业务标识；治理后通过映射关联黄金编码。", "TEXT", 1,
         "CONFIDENTIAL", "GROUP_APPROVER", "PLANT_STEWARD", "SY/T 5497-2018；企业编码规则", "1.0"),
        ("system_source", "来源系统", "产生或维护该源记录的 ERP、EAM、MES、WMS 或采购系统。", "TEXT", 1,
         "INTERNAL", "GROUP_APPROVER", "PLANT_STEWARD", "企业数据源目录", "1.0"),
        ("material_name", "物料名称", "用于识别物料实体的业务名称，需与型号、规格等属性联合判断。", "TEXT", 1,
         "INTERNAL", "GROUP_APPROVER", "PLANT_STEWARD", "SY/T 5497-2018 适配分类", "1.0"),
        ("description", "物料描述", "物料规格、结构、用途或关键技术属性的文本描述。", "TEXT", 1,
         "INTERNAL", "GROUP_APPROVER", "PLANT_STEWARD", "企业物料描述规范", "1.0"),
        ("category", "物料分类", "物料在已发布标准分类版本中的业务类别。", "TEXT", 1,
         "INTERNAL", "GROUP_APPROVER", "PLANT_STEWARD", "SY/T 5497-2018 适配分类", "1.0"),
        ("unit", "计量单位", "物料基础计量单位，应使用企业批准的参考数据值。", "TEXT", 1,
         "INTERNAL", "GROUP_APPROVER", "PLANT_STEWARD", "企业计量单位参考数据", "1.0"),
        ("create_time", "源记录创建时间", "源系统记录首次创建或本批次导入的时间。", "TIMESTAMP", 0,
         "INTERNAL", "GROUP_APPROVER", "PLANT_STEWARD", "ISO 8601", "1.0"),
        ("plant_code", "工厂编码", "数据责任主体和访问范围对应的工厂或集团编码。", "TEXT", 1,
         "CONFIDENTIAL", "GROUP_ADMIN", "PLANT_STEWARD", "企业组织机构参考数据", "1.0"),
        ("source_record_id", "来源业务主键", "在源系统和源表中唯一定位问题记录的业务主键。", "TEXT", 1,
         "CONFIDENTIAL", "GROUP_APPROVER", "PLANT_STEWARD", "来源系统数据契约", "1.0"),
        ("mdm_code", "黄金主数据编码", "通过治理审批形成、用于跨系统共享的统一物料编码。", "TEXT", 1,
         "CONFIDENTIAL", "GROUP_APPROVER", "PLANT_STEWARD", "企业主数据编码规则", "1.0"),
    )

    RULE_SEEDS = (
        ("DQ-REQ-MATERIAL-CODE", "物料编码非空", "完整性", "material_code", "REQUIRED", 100.0, "HIGH", "PLANT_STEWARD", "物料编码必须存在。"),
        ("DQ-REQ-MATERIAL-NAME", "物料名称非空", "完整性", "material_name", "REQUIRED", 100.0, "HIGH", "PLANT_STEWARD", "物料名称必须存在。"),
        ("DQ-REQ-SOURCE", "来源系统非空", "完整性", "system_source", "REQUIRED", 100.0, "HIGH", "PLANT_STEWARD", "来源系统必须存在。"),
        ("DQ-REQ-DESCRIPTION", "物料描述非空", "完整性", "description", "REQUIRED", 95.0, "MEDIUM", "PLANT_STEWARD", "物料描述缺失会降低语义识别证据。"),
        ("DQ-REQ-UNIT", "计量单位非空", "完整性", "unit", "REQUIRED", 100.0, "MEDIUM", "PLANT_STEWARD", "计量单位应引用企业参考数据。"),
        ("DQ-CATEGORY-MAPPED", "标准分类可解析", "标准化", "category", "CATEGORY_RESOLVED", 95.0, "MEDIUM", "PLANT_STEWARD", "分类应引用已发布标准知识版本。"),
        ("DQ-LINEAGE-TRACE", "来源记录可追溯", "可追溯性", "source_record_id", "LINEAGE_PRESENT", 100.0, "HIGH", "PLANT_STEWARD", "记录应包含源系统、源表、业务主键和内容指纹。"),
        ("DQ-CODE-UNIQUE", "批次内源编码唯一", "唯一性", "material_code", "UNIQUE_IN_SOURCE", 100.0, "HIGH", "PLANT_STEWARD", "同一来源系统和工厂内编码应唯一。"),
        ("DQ-SEMANTIC-REVIEW", "低置信度或冲突复核", "准确性代理", "semantic_match", "REVIEW_GATE", 85.0, "HIGH", "PLANT_STEWARD", "低置信度或属性冲突必须进入人工审核。"),
    )

    ISSUE_RULE_MAP = {
        "MISSING_MATERIAL_CODE": "DQ-REQ-MATERIAL-CODE",
        "MISSING_MATERIAL_NAME": "DQ-REQ-MATERIAL-NAME",
        "MISSING_SYSTEM_SOURCE": "DQ-REQ-SOURCE",
        "MISSING_DESCRIPTION": "DQ-REQ-DESCRIPTION",
        "MISSING_UNIT": "DQ-REQ-UNIT",
        "MISSING_CATEGORY": "DQ-CATEGORY-MAPPED",
        "CATEGORY_UNRESOLVED": "DQ-CATEGORY-MAPPED",
        "REVIEW_REQUIRED": "DQ-SEMANTIC-REVIEW",
        "LOW_SIMILARITY": "DQ-SEMANTIC-REVIEW",
    }

    def init_schema(self, conn) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS governance_controls (
                control_code TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                control_name TEXT NOT NULL,
                objective TEXT NOT NULL,
                accountable_role TEXT NOT NULL,
                responsible_role TEXT NOT NULL,
                target_metric TEXT NOT NULL,
                target_value TEXT NOT NULL,
                status TEXT NOT NULL,
                maturity_level INTEGER NOT NULL DEFAULT 1,
                evidence_source TEXT,
                assessed_by TEXT,
                last_assessed_at TEXT,
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS metadata_catalog (
                element_name TEXT PRIMARY KEY,
                business_name TEXT NOT NULL,
                definition TEXT NOT NULL,
                data_type TEXT NOT NULL,
                required INTEGER NOT NULL DEFAULT 0,
                classification TEXT NOT NULL,
                owner_role TEXT NOT NULL,
                steward_role TEXT NOT NULL,
                standard_reference TEXT,
                status TEXT NOT NULL DEFAULT 'PUBLISHED',
                version TEXT NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS data_quality_rules (
                rule_id TEXT PRIMARY KEY,
                rule_name TEXT NOT NULL,
                dimension TEXT NOT NULL,
                target_field TEXT NOT NULL,
                rule_type TEXT NOT NULL,
                threshold REAL NOT NULL,
                severity TEXT NOT NULL,
                owner_role TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                description TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS data_quality_issues (
                issue_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                record_id INTEGER,
                rule_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                source_system TEXT,
                source_record_id TEXT,
                plant_code TEXT NOT NULL DEFAULT 'GROUP',
                owner_role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                due_at TEXT NOT NULL,
                resolution TEXT,
                updated_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(batch_id, record_id, rule_id, message),
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE,
                FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE,
                FOREIGN KEY (rule_id) REFERENCES data_quality_rules(rule_id)
            );
            CREATE INDEX IF NOT EXISTS idx_quality_issues_batch_status
                ON data_quality_issues(batch_id, status, plant_code);
            CREATE INDEX IF NOT EXISTS idx_governance_controls_domain
                ON governance_controls(domain, status);
            """
        )
        now = _utc_now()
        conn.executemany(
            """INSERT INTO governance_controls
               (control_code, domain, control_name, objective, accountable_role, responsible_role,
                target_metric, target_value, status, maturity_level, evidence_source, last_assessed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(control_code) DO NOTHING""",
            [(*row, now) for row in self.CONTROL_SEEDS],
        )
        conn.executemany(
            """INSERT INTO metadata_catalog
               (element_name, business_name, definition, data_type, required, classification,
                owner_role, steward_role, standard_reference, version, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'system-bootstrap', ?)
               ON CONFLICT(element_name) DO NOTHING""",
            [(*row, now) for row in self.METADATA_SEEDS],
        )
        conn.executemany(
            """INSERT INTO data_quality_rules
               (rule_id, rule_name, dimension, target_field, rule_type, threshold, severity,
                owner_role, description, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(rule_id) DO NOTHING""",
            [(*row, now) for row in self.RULE_SEEDS],
        )

    @staticmethod
    def _rows(conn, sql: str, params=()) -> list[dict]:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def capture_batch_issues(self, conn, batch_id: str) -> dict:
        rules = {
            row["rule_id"]: dict(row)
            for row in conn.execute("SELECT * FROM data_quality_rules WHERE active = 1").fetchall()
        }
        records = conn.execute(
            "SELECT id, system_source, material_code, plant_code, ext FROM records WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        duplicates: dict[tuple[str, str, str], list[int]] = {}
        items: list[dict] = []
        for row in records:
            ext = json.loads(row["ext"] or "{}")
            provenance = ext.get("_provenance") or {}
            issues = list(ext.get("_quality_issues") or [])
            if not provenance.get("source_record_id") or not provenance.get("record_hash"):
                issues.append({"code": "LINEAGE_MISSING", "severity": "HIGH", "message": "来源业务主键或内容指纹缺失"})
            key = (
                str(row["system_source"] or "").strip().upper(),
                str(row["plant_code"] or "GROUP").strip().upper(),
                str(row["material_code"] or "").strip().upper(),
            )
            if key[2]:
                duplicates.setdefault(key, []).append(int(row["id"]))
            for issue in issues:
                rule_id = "DQ-LINEAGE-TRACE" if issue.get("code") == "LINEAGE_MISSING" else self.ISSUE_RULE_MAP.get(issue.get("code"))
                if not rule_id or rule_id not in rules:
                    continue
                items.append({
                    "record_id": int(row["id"]), "rule_id": rule_id,
                    "severity": str(issue.get("severity") or rules[rule_id]["severity"]).upper(),
                    "message": str(issue.get("message") or rules[rule_id]["description"]),
                    "source_system": row["system_source"],
                    "source_record_id": provenance.get("source_record_id") or row["material_code"],
                    "plant_code": row["plant_code"] or "GROUP",
                })
        for (source, plant, code), record_ids in duplicates.items():
            if len(record_ids) < 2:
                continue
            for record_id in record_ids:
                items.append({
                    "record_id": record_id, "rule_id": "DQ-CODE-UNIQUE", "severity": "HIGH",
                    "message": f"来源系统 {source} 的物料编码 {code} 在当前批次重复 {len(record_ids)} 次",
                    "source_system": source, "source_record_id": code, "plant_code": plant,
                })
        now = datetime.now(timezone.utc)
        inserted = 0
        for item in items:
            rule = rules[item["rule_id"]]
            due_days = 2 if item["severity"] == "HIGH" else 5 if item["severity"] == "MEDIUM" else 10
            issue_key = f'{batch_id}|{item["record_id"]}|{item["rule_id"]}|{item["message"]}'
            issue_id = "DQI-" + hashlib.sha256(issue_key.encode("utf-8")).hexdigest()[:16].upper()
            cursor = conn.execute(
                """INSERT INTO data_quality_issues
                   (issue_id, batch_id, record_id, rule_id, dimension, severity, message,
                    source_system, source_record_id, plant_code, owner_role, status, due_at,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
                   ON CONFLICT(batch_id, record_id, rule_id, message) DO NOTHING""",
                (issue_id, batch_id, item["record_id"], item["rule_id"], rule["dimension"],
                 item["severity"], item["message"], item["source_system"], item["source_record_id"],
                 item["plant_code"], rule["owner_role"], (now + timedelta(days=due_days)).isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
            )
            inserted += int(cursor.rowcount > 0)
        counts = dict(conn.execute(
            "SELECT COUNT(*) AS total, SUM(status = 'OPEN') AS open_count FROM data_quality_issues WHERE batch_id = ?",
            (batch_id,),
        ).fetchone())
        return {"batch_id": batch_id, "created": inserted, "total": counts["total"], "open": counts["open_count"] or 0}

    def catalog(self, conn) -> dict:
        controls = self._rows(conn, "SELECT * FROM governance_controls ORDER BY domain, control_code")
        metadata = self._rows(conn, "SELECT * FROM metadata_catalog ORDER BY element_name")
        rules = self._rows(conn, "SELECT * FROM data_quality_rules ORDER BY dimension, rule_id")
        domains: dict[str, dict] = {}
        for control in controls:
            item = domains.setdefault(control["domain"], {"total": 0, "implemented": 0, "monitored": 0})
            item["total"] += 1
            item["implemented"] += int(control["status"] in {"IMPLEMENTED", "MONITORED"})
            item["monitored"] += int(control["status"] == "MONITORED")
        return {
            "framework": "DAMA-DMBOK2 Rev 适配控制视图",
            "framework_note": "用于工程控制映射和证据管理，不代表 DAMA、等保或法律合规认证。",
            "domains": domains,
            "controls": controls,
            "metadata_catalog": metadata,
            "quality_rules": rules,
            "raci": {
                "GROUP_ADMIN": "治理机制、平台安全、策略和企业集成最终负责",
                "GROUP_APPROVER": "主数据定义、黄金记录和跨系统发布审批负责",
                "PLANT_STEWARD": "本厂数据接入、问题整改、审核和反馈执行负责",
                "AUDITOR": "控制证据、审计链、保留与越权事件独立复核",
            },
        }

    def issue_summary(self, conn, batch_id: str | None = None, plant_code: str = "GROUP") -> dict:
        clauses = []
        params: list[object] = []
        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if plant_code != "GROUP":
            clauses.append("plant_code = ?")
            params.append(plant_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        issues = self._rows(
            conn,
            "SELECT * FROM data_quality_issues" + where + " ORDER BY status, severity, due_at, issue_id",
            params,
        )
        now = _utc_now()
        status_counts: dict[str, int] = {}
        dimension_counts: dict[str, int] = {}
        overdue = 0
        for issue in issues:
            status_counts[issue["status"]] = status_counts.get(issue["status"], 0) + 1
            dimension_counts[issue["dimension"]] = dimension_counts.get(issue["dimension"], 0) + 1
            overdue += int(issue["status"] in {"OPEN", "ACKNOWLEDGED"} and issue["due_at"] < now)
        return {
            "issues": issues, "count": len(issues), "status_counts": status_counts,
            "dimension_counts": dimension_counts, "overdue_count": overdue,
        }

    def update_issue(self, conn, issue_id: str, status: str, resolution: str, actor: str, plant_code: str) -> dict | None:
        row = conn.execute("SELECT * FROM data_quality_issues WHERE issue_id = ?", (issue_id,)).fetchone()
        if not row:
            return None
        if plant_code != "GROUP" and row["plant_code"] != plant_code:
            raise PermissionError("quality issue is outside the identity plant scope")
        allowed = {
            "OPEN": {"ACKNOWLEDGED", "RESOLVED", "WAIVED"},
            "ACKNOWLEDGED": {"OPEN", "RESOLVED", "WAIVED"},
            "RESOLVED": {"OPEN"},
            "WAIVED": {"OPEN"},
        }
        current = row["status"]
        if status not in allowed.get(current, set()):
            raise ValueError(f"invalid quality issue transition: {current} -> {status}")
        if status in {"RESOLVED", "WAIVED"} and not resolution.strip():
            raise ValueError("resolution is required when closing a quality issue")
        conn.execute(
            "UPDATE data_quality_issues SET status = ?, resolution = ?, updated_by = ?, updated_at = ? WHERE issue_id = ?",
            (status, resolution.strip(), actor, _utc_now(), issue_id),
        )
        return dict(conn.execute("SELECT * FROM data_quality_issues WHERE issue_id = ?", (issue_id,)).fetchone())

    def assess_control(self, conn, control_code: str, payload: dict, actor: str) -> dict | None:
        row = conn.execute("SELECT * FROM governance_controls WHERE control_code = ?", (control_code,)).fetchone()
        if not row:
            return None
        status = str(payload.get("status") or row["status"]).strip().upper()
        if status not in {"DESIGNED", "IMPLEMENTED", "MONITORED", "GAP"}:
            raise ValueError("status must be DESIGNED, IMPLEMENTED, MONITORED, or GAP")
        try:
            maturity = int(payload.get("maturity_level", row["maturity_level"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("maturity_level must be an integer from 1 to 5") from exc
        if maturity < 1 or maturity > 5:
            raise ValueError("maturity_level must be an integer from 1 to 5")
        evidence = str(payload.get("evidence_source") or row["evidence_source"] or "").strip()
        notes = str(payload.get("notes") or "").strip()
        conn.execute(
            """UPDATE governance_controls SET status = ?, maturity_level = ?, evidence_source = ?,
                      assessed_by = ?, last_assessed_at = ?, notes = ? WHERE control_code = ?""",
            (status, maturity, evidence, actor, _utc_now(), notes, control_code),
        )
        return dict(conn.execute("SELECT * FROM governance_controls WHERE control_code = ?", (control_code,)).fetchone())
