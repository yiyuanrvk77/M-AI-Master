"""Enterprise security and compatibility integration tests for M-AI Master.

The suite loads a separate backend module against a temporary SQLite database,
so it never reads or mutates the developer's runtime database.  Enterprise
account passwords are fixed only inside the temporary test process.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_DIR / "backend"
APP_PATH = BACKEND_DIR / "app.py"

# Enterprise contract tests must not depend on a developer's external API key.
for api_key_name in ("DASHSCOPE_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY"):
    os.environ[api_key_name] = ""

ACCOUNT_PASSWORDS = {
    "group_admin": "Mai!Admin12345",
    "group_approver": "Mai!Approve12345",
    "shanghai_steward": "Mai!Shanghai12345",
    "compliance_auditor": "Mai!Audit12345",
}

EXPECTED_IDENTITIES = {
    "group_admin": ("GROUP_ADMIN", "GROUP"),
    "group_approver": ("GROUP_APPROVER", "GROUP"),
    "shanghai_steward": ("PLANT_STEWARD", "SHANGHAI"),
    "compliance_auditor": ("AUDITOR", "GROUP"),
}


class EnterpriseIntegrationTest(unittest.TestCase):
    """Exercise the enterprise profile through Flask's public HTTP contract."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="mai-master-enterprise-")
        runtime_dir = Path(cls.temp_dir.name)
        env_updates = {
            "MDM_DB_PATH": str(runtime_dir / "enterprise-test.db"),
            "MDM_SECURITY_DIR": str(runtime_dir / "security"),
            "MDM_SECURITY_MODE": "enterprise",
            "MDM_SESSION_SECRET": "enterprise-test-session-secret-not-for-production",
            "MDM_COOKIE_SECURE": "0",
            "MDM_PRODUCTION": "1",
            "MDM_PADDLEOCR_ENABLED": "0",
            "MDM_ALLOW_OCR_INSTALL": "0",
            "MDM_AUTH_PASSWORD": "",
            "DASHSCOPE_API_KEY": "",
            "ZHIPU_API_KEY": "",
            "OPENAI_API_KEY": "",
            "MDM_PASSWORD_GROUP_ADMIN": ACCOUNT_PASSWORDS["group_admin"],
            "MDM_PASSWORD_GROUP_APPROVER": ACCOUNT_PASSWORDS["group_approver"],
            "MDM_PASSWORD_SHANGHAI_STEWARD": ACCOUNT_PASSWORDS["shanghai_steward"],
            "MDM_PASSWORD_COMPLIANCE_AUDITOR": ACCOUNT_PASSWORDS["compliance_auditor"],
        }
        old_env = {key: os.environ.get(key) for key in env_updates}
        os.environ.update(env_updates)
        sys.path.insert(0, str(BACKEND_DIR))
        module_name = f"mai_enterprise_test_backend_{uuid.uuid4().hex}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load Flask backend from {APP_PATH}")
            backend = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = backend
            spec.loader.exec_module(backend)
            cls.backend = backend
            cls.module_name = module_name
            cls.app = backend.app
            cls.app.config.update(TESTING=True)
        finally:
            sys.path.remove(str(BACKEND_DIR))
            for key, old_value in old_env.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(getattr(cls, "module_name", ""), None)
        cls.temp_dir.cleanup()

    def setUp(self):
        self._reset_database()

    def _reset_database(self):
        """Clear test transactions while retaining the four bootstrapped users."""
        with self.backend.db_connect() as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()}
            for table in (
                "legal_holds", "audit_blocks", "plant_feedback", "workflow_steps", "distribution_logs",
                "lifecycle", "search_history", "quality_reports", "reviews", "mappings",
                "batch_masters", "records", "batches", "masters",
            ):
                if table in tables:
                    conn.execute(f"DELETE FROM {table}")
            if "vector_embeddings" in tables:
                conn.execute("DELETE FROM vector_embeddings WHERE batch_id IS NOT NULL")
            if "security_events" in tables:
                conn.execute("DELETE FROM security_events")
            if "security_users" in tables:
                conn.execute(
                    """UPDATE security_users SET failed_attempts = 0, locked_until = NULL,
                       session_version = 1, active = 1"""
                )

    def _new_client(self):
        return self.app.test_client()

    def _login(self, username: str):
        client = self._new_client()
        response = client.post("/api/auth/login", json={
            "username": username,
            "password": ACCOUNT_PASSWORDS[username],
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json() or {}
        principal = payload.get("principal") or payload.get("user") or {}
        csrf_token = payload.get("csrf_token") or payload.get("csrf")
        self.assertEqual(principal.get("username"), username)
        self.assertTrue(csrf_token, "login response must return a CSRF token")
        return client, str(csrf_token), principal

    @staticmethod
    def _csrf(token: str) -> dict[str, str]:
        return {"X-CSRF-Token": token}

    @staticmethod
    def _principal(payload: dict) -> dict:
        return payload.get("principal") or payload.get("user") or payload

    @staticmethod
    def _verification(payload: dict) -> dict:
        return payload.get("verification") or payload.get("security_audit") or payload

    @staticmethod
    def _batch_id(payload: dict) -> str:
        batch = payload.get("batch") or {}
        return str(batch.get("batch_id") or payload.get("batch_id") or "")

    @staticmethod
    def _lifecycle_state(payload: dict) -> list[dict]:
        state = payload.get("state") if isinstance(payload.get("state"), dict) else payload
        return list(state.get("lifecycle") or [])

    @staticmethod
    def _identity_labels(principal: dict) -> set[str]:
        username = str(principal.get("username") or "")
        display_name = str(principal.get("display_name") or "")
        return {
            value for value in (
                username, display_name,
                f"{display_name} ({username})" if display_name and username else "",
            ) if value
        }

    def _create_batch(self, client, csrf_token: str, plant_code: str = "GROUP") -> dict:
        prefix = "SH" if plant_code == "SHANGHAI" else "GRP"
        response = client.post(
            "/api/batches",
            headers=self._csrf(csrf_token),
            json={
                "filename": "enterprise-contract.json",
                "plant_code": plant_code,
                "records": [
                    {
                        "material_code": f"{prefix}-001",
                        "system_source": "SAP",
                        "material_name": "SKF 6312 深沟球轴承",
                        "description": "SKF 6312 C3 bearing",
                        "category": "轴承",
                        "unit": "个",
                        "plant_code": plant_code,
                    },
                    {
                        "material_code": f"{prefix}-002",
                        "system_source": "EAM",
                        "material_name": "SKF 6312 滚动轴承",
                        "description": "Bearing SKF 6312 C3",
                        "category": "轴承",
                        "unit": "个",
                        "plant_code": plant_code,
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json() or {}
        self.assertTrue(self._batch_id(payload))
        return payload

    def test_four_bootstrap_accounts_login_and_report_server_identity(self):
        public_health = self._new_client().get("/api/health")
        self.assertIn(public_health.status_code, {200, 503})

        anonymous = self._new_client().get("/api/state/latest")
        self.assertEqual(anonymous.status_code, 401, anonymous.get_json())

        for username, (expected_role, expected_plant) in EXPECTED_IDENTITIES.items():
            with self.subTest(username=username):
                client, _csrf_token, login_principal = self._login(username)
                self.assertEqual(login_principal.get("role"), expected_role)
                self.assertEqual(login_principal.get("plant_code"), expected_plant)
                me = client.get("/api/auth/me")
                self.assertEqual(me.status_code, 200, me.get_json())
                principal = self._principal(me.get_json() or {})
                self.assertEqual(principal.get("username"), username)
                self.assertEqual(principal.get("role"), expected_role)
                self.assertEqual(principal.get("plant_code"), expected_plant)
                self.assertNotIn("password_hash", json.dumps(me.get_json(), ensure_ascii=False))

    def test_csrf_is_required_for_state_changes_and_logout(self):
        client, csrf_token, _principal = self._login("group_admin")
        batch_payload = {
            "filename": "csrf.json",
            "records": [{
                "material_code": "CSRF-001", "system_source": "SAP",
                "material_name": "DN50 闸阀", "description": "PN16 WCB",
            }],
        }
        missing = client.post("/api/batches", json=batch_payload)
        self.assertEqual(missing.status_code, 403, missing.get_json())
        wrong = client.post(
            "/api/batches", headers={"X-CSRF-Token": "invalid-token"}, json=batch_payload
        )
        self.assertEqual(wrong.status_code, 403, wrong.get_json())
        allowed = client.post(
            "/api/batches", headers=self._csrf(csrf_token), json=batch_payload
        )
        self.assertEqual(allowed.status_code, 201, allowed.get_json())

        rejected_logout = client.post("/api/auth/logout")
        self.assertEqual(rejected_logout.status_code, 403, rejected_logout.get_json())
        logout = client.post("/api/auth/logout", headers=self._csrf(csrf_token))
        self.assertEqual(logout.status_code, 200, logout.get_json())
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_configurable_rag_profiles_are_admin_managed(self):
        admin, csrf_token, _principal = self._login("group_admin")
        versions = admin.get("/api/knowledge/versions")
        self.assertEqual(versions.status_code, 200, versions.get_json())
        self.assertEqual((versions.get_json() or {}).get("active", {}).get("count"), 72)

        created = admin.post(
            "/api/knowledge/profile", headers=self._csrf(csrf_token), json={
                "profile_name": "enterprise-balanced",
                "config": {
                    "dimension": 384,
                    "rule_weight": 0.45,
                    "lexical_weight": 0.30,
                    "vector_weight": 0.25,
                    "auto_accept_threshold": 0.68,
                    "minimum_margin": 0.08,
                    "default_top_k": 3,
                },
                "activate": False,
                "reindex": False,
            },
        )
        self.assertEqual(created.status_code, 200, created.get_json())
        profiles = admin.get("/api/knowledge/profile")
        self.assertEqual(profiles.status_code, 200, profiles.get_json())
        self.assertIn(
            "enterprise-balanced",
            {row.get("profile_name") for row in (profiles.get_json() or {}).get("profiles", [])},
        )

        steward, steward_csrf, _ = self._login("shanghai_steward")
        self.assertEqual(steward.get("/api/knowledge/profile").status_code, 200)
        denied = steward.post(
            "/api/knowledge/profile", headers=self._csrf(steward_csrf),
            json={"profile_name": "forged", "config": {}},
        )
        self.assertEqual(denied.status_code, 403, denied.get_json())

    def test_rbac_rejects_operations_outside_each_role(self):
        admin, _admin_csrf, _ = self._login("group_admin")
        self.assertEqual(admin.get("/api/admin/users").status_code, 200)

        approver, approver_csrf, _ = self._login("group_approver")
        denied_ingest = approver.post(
            "/api/batches", headers=self._csrf(approver_csrf),
            json={"filename": "forbidden.json", "records": [{"material_code": "A"}]},
        )
        self.assertEqual(denied_ingest.status_code, 403, denied_ingest.get_json())

        steward, steward_csrf, _ = self._login("shanghai_steward")
        self.assertEqual(steward.get("/api/ocr/install").status_code, 200)
        denied_ocr_install = steward.post(
            "/api/ocr/install", headers=self._csrf(steward_csrf), json={},
        )
        self.assertEqual(denied_ocr_install.status_code, 403, denied_ocr_install.get_json())
        self.assertEqual(steward.get("/api/admin/users").status_code, 403)
        self.assertEqual(steward.get("/api/security/audit").status_code, 403)
        denied_retention = steward.post(
            "/api/compliance/retention/run",
            headers=self._csrf(steward_csrf), json={"dry_run": True},
        )
        self.assertEqual(denied_retention.status_code, 403, denied_retention.get_json())

        auditor, auditor_csrf, _ = self._login("compliance_auditor")
        self.assertEqual(auditor.get("/api/compliance/status").status_code, 200)
        denied_write = auditor.post(
            "/api/batches", headers=self._csrf(auditor_csrf),
            json={"filename": "auditor.json", "records": [{"material_code": "A"}]},
        )
        self.assertEqual(denied_write.status_code, 403, denied_write.get_json())

    def test_shanghai_identity_cannot_spoof_group_or_beijing_scope(self):
        client, csrf_token, _principal = self._login("shanghai_steward")
        own_batch = self._create_batch(client, csrf_token, "SHANGHAI")
        batch_id = self._batch_id(own_batch)

        for requested_scope in ("GROUP", "BEIJING"):
            with self.subTest(scope=requested_scope):
                response = client.get(
                    f"/api/batches/{batch_id}?plant_code={requested_scope}"
                )
                self.assertEqual(response.status_code, 403, response.get_json())

        forged = client.post(
            "/api/batches", headers=self._csrf(csrf_token), json={
                "filename": "beijing-forgery.json", "plant_code": "BEIJING",
                "records": [{
                    "material_code": "BJ-FORGED", "system_source": "SAP",
                    "material_name": "北京伪造数据", "description": "must be rejected",
                    "plant_code": "BEIJING",
                }],
            },
        )
        self.assertEqual(forged.status_code, 403, forged.get_json())

        visible = client.get(
            f"/api/batches/{batch_id}?plant_code=SHANGHAI"
        )
        self.assertEqual(visible.status_code, 200, visible.get_json())
        self.assertTrue(all(
            row.get("plant_code") == "SHANGHAI"
            for row in (visible.get_json() or {}).get("records", [])
        ))

    def test_creator_reviewer_and_audit_actor_are_bound_to_session_identity(self):
        steward, steward_csrf, steward_principal = self._login("shanghai_steward")
        state = self._create_batch(steward, steward_csrf, "SHANGHAI")
        batch_id = self._batch_id(state)

        created = steward.post(
            "/api/lifecycle", headers=self._csrf(steward_csrf), json={
                "batch_id": batch_id, "plant_code": "SHANGHAI",
                "name": "SKF 6312 C3 轴承", "category": "深沟球轴承",
                "reason": "企业身份绑定测试", "creator": "group_admin",
                "actor": "group_admin",
            },
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        lifecycle_id = int((created.get_json() or {}).get("id"))

        snapshot = steward.get(f"/api/batches/{batch_id}?plant_code=SHANGHAI")
        self.assertEqual(snapshot.status_code, 200, snapshot.get_json())
        request_row = next(
            row for row in self._lifecycle_state(snapshot.get_json() or {})
            if int(row["id"]) == lifecycle_id
        )
        steward_names = self._identity_labels(steward_principal)
        self.assertIn(request_row.get("creator"), steward_names)
        self.assertNotEqual(request_row.get("creator"), "group_admin")

        reviewed = steward.patch(
            f"/api/lifecycle/{lifecycle_id}", headers=self._csrf(steward_csrf),
            json={"status": "REVIEWED", "reviewer": "group_approver", "actor": "group_admin"},
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.get_json())
        reviewed_row = next(
            row for row in self._lifecycle_state(reviewed.get_json() or {})
            if int(row["id"]) == lifecycle_id
        )
        self.assertIn(reviewed_row.get("reviewer"), steward_names)

        approver, approver_csrf, approver_principal = self._login("group_approver")
        approved = approver.patch(
            f"/api/lifecycle/{lifecycle_id}", headers=self._csrf(approver_csrf),
            json={"status": "APPROVED", "reviewer": "attacker", "actor": "attacker"},
        )
        self.assertEqual(approved.status_code, 200, approved.get_json())
        approved_row = next(
            row for row in self._lifecycle_state(approved.get_json() or {})
            if int(row["id"]) == lifecycle_id
        )
        self.assertIn(approved_row.get("reviewer"), self._identity_labels(approver_principal))
        self.assertNotEqual(approved_row.get("reviewer"), "attacker")

        admin, _admin_csrf, _ = self._login("group_admin")
        audit = admin.get("/api/security/audit")
        self.assertEqual(audit.status_code, 200, audit.get_json())
        events = (audit.get_json() or {}).get("events") or (audit.get_json() or {}).get("items") or []
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn('"actor": "attacker"', serialized)

    def test_security_audit_hmac_chain_detects_database_tampering(self):
        admin, admin_csrf, _ = self._login("group_admin")
        self._create_batch(admin, admin_csrf)
        verification = admin.get("/api/security/audit/verify")
        self.assertEqual(verification.status_code, 200, verification.get_json())
        self.assertTrue(self._verification(verification.get_json() or {}).get("valid"))

        with self.backend.db_connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM security_events").fetchone()[0]
            self.assertGreater(count, 0, "security-sensitive operations must create audit events")
            conn.execute(
                """UPDATE security_events SET event_type = 'TAMPERED_EVENT'
                   WHERE height = (SELECT MIN(height) FROM security_events)"""
            )

        tampered = admin.get("/api/security/audit/verify")
        self.assertEqual(tampered.status_code, 200, tampered.get_json())
        result = self._verification(tampered.get_json() or {})
        self.assertFalse(result.get("valid"))
        self.assertTrue(result.get("errors"))

    def test_legal_hold_survives_retention_and_global_delete(self):
        admin, csrf_token, _principal = self._login("group_admin")
        state = self._create_batch(admin, csrf_token)
        batch_id = self._batch_id(state)
        policy = admin.patch(
            f"/api/compliance/batches/{batch_id}",
            headers=self._csrf(csrf_token),
            json={
                "data_classification": "CONFIDENTIAL",
                "retention_days": 1,
                "legal_hold": True,
                "legal_hold_reason": "pending regulatory investigation",
            },
        )
        self.assertEqual(policy.status_code, 200, policy.get_json())

        with self.backend.db_connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(int(row["legal_hold"]), 1)
            columns = {item[1] for item in conn.execute("PRAGMA table_info(batches)").fetchall()}
            if "retention_until" in columns:
                conn.execute(
                    "UPDATE batches SET retention_until = '2000-01-01T00:00:00+00:00' WHERE batch_id = ?",
                    (batch_id,),
                )
            else:
                conn.execute(
                    "UPDATE batches SET created_at = '2000-01-01T00:00:00+00:00' WHERE batch_id = ?",
                    (batch_id,),
                )

        retention = admin.post(
            "/api/compliance/retention/run",
            headers=self._csrf(csrf_token),
            json={"dry_run": False, "confirm": "DELETE_EXPIRED_BATCHES"},
        )
        self.assertEqual(retention.status_code, 200, retention.get_json())
        self.assertEqual(admin.get(f"/api/batches/{batch_id}").status_code, 200)

        clear_attempt = admin.delete(
            "/api/data", headers=self._csrf(csrf_token),
            json={"confirm": "DELETE_ALL_DATA"},
        )
        self.assertIn(clear_attempt.status_code, {200, 409}, clear_attempt.get_json())
        self.assertEqual(
            admin.get(f"/api/batches/{batch_id}").status_code, 200,
            "legal hold must survive both retention and bulk deletion",
        )
        compliance = admin.get("/api/compliance/status")
        self.assertEqual(compliance.status_code, 200, compliance.get_json())
        summary = compliance.get_json() or {}
        self.assertGreaterEqual(int(summary.get("active_legal_holds", 0)), 1)

    def test_legacy_governance_and_rag_contracts_remain_additively_compatible(self):
        admin, csrf_token, _principal = self._login("group_admin")
        state = self._create_batch(admin, csrf_token)
        batch_id = self._batch_id(state)
        self.assertTrue({
            "batch", "records", "masters", "mappings", "reviews", "quality_report",
            "summary", "workflow", "vector_index", "audit_chain",
        }.issubset(state))

        state_response = admin.get(f"/api/batches/{batch_id}?plant_code=GROUP")
        self.assertEqual(state_response.status_code, 200, state_response.get_json())
        self.assertTrue({
            "record_count", "master_count", "review_count", "compression_rate"
        }.issubset((state_response.get_json() or {}).get("summary", {})))

        semantic = admin.post(
            "/api/semantic", headers=self._csrf(csrf_token),
            json={"text1": "SKF 6312轴承", "text2": "斯凯孚6312滚动轴承", "model": "qwen"},
        )
        self.assertEqual(semantic.status_code, 200, semantic.get_json())
        self.assertTrue({"similarity", "method", "text1", "text2"}.issubset(semantic.get_json() or {}))

        classified = admin.post(
            "/api/classify", headers=self._csrf(csrf_token),
            json={"material_name": "SKF 6312深沟球轴承", "description": "316L", "plant_code": "GROUP"},
        )
        self.assertEqual(classified.status_code, 200, classified.get_json())
        self.assertTrue({
            "recommended_category", "candidates", "rag", "standard_references", "plant_code"
        }.issubset(classified.get_json() or {}))

        standard_stats = admin.get("/api/standards/stats")
        self.assertEqual(standard_stats.status_code, 200, standard_stats.get_json())
        self.assertTrue({"namespace", "count", "provider", "model", "dimension"}.issubset(
            standard_stats.get_json() or {}
        ))
        standard_search = admin.post(
            "/api/standards/search", headers=self._csrf(csrf_token),
            json={"query": "DN150 铸钢法兰闸阀", "top_k": 3},
        )
        self.assertEqual(standard_search.status_code, 200, standard_search.get_json())
        search_payload = standard_search.get_json() or {}
        self.assertEqual(search_payload.get("namespace"), "standard_kb")
        self.assertIsInstance(search_payload.get("results"), list)
        if search_payload["results"]:
            self.assertTrue({"category", "reference_id", "title", "score"}.issubset(
                search_payload["results"][0]
            ))

        vectors = admin.get(f"/api/vectors/stats?batch_id={batch_id}")
        self.assertEqual(vectors.status_code, 200, vectors.get_json())
        self.assertTrue({"batch_id", "count", "indexes"}.issubset(vectors.get_json() or {}))
        graph = admin.get(f"/api/graph?batch_id={batch_id}&plant_code=GROUP&raw_limit=10")
        self.assertEqual(graph.status_code, 200, graph.get_json())
        self.assertTrue({"nodes", "edges", "stats", "engine"}.issubset(graph.get_json() or {}))
        chain = admin.get(f"/api/blockchain/verify?batch_id={batch_id}")
        self.assertEqual(chain.status_code, 200, chain.get_json())
        self.assertIn("valid", chain.get_json() or {})

    def test_dmbok_catalog_quality_issue_workflow_and_control_permissions(self):
        steward, steward_csrf, _principal = self._login("shanghai_steward")
        created = steward.post(
            "/api/batches", headers=self._csrf(steward_csrf),
            json={
                "filename": "dmbok-quality-contract.json", "plant_code": "SHANGHAI",
                "records": [{
                    "material_code": "SH-DQ-001", "system_source": "SAP",
                    "material_name": "待分类工业备件", "description": "", "category": "",
                    "unit": "", "plant_code": "SHANGHAI",
                }],
            },
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        batch_id = self._batch_id(created.get_json() or {})

        catalog = steward.get(
            f"/api/governance/catalog?batch_id={batch_id}&plant_code=SHANGHAI&include_issues=1"
        )
        self.assertEqual(catalog.status_code, 200, catalog.get_json())
        body = catalog.get_json() or {}
        self.assertGreaterEqual(len(body.get("controls") or []), 14)
        self.assertGreaterEqual(len(body.get("metadata_catalog") or []), 10)
        self.assertGreaterEqual(len(body.get("quality_rules") or []), 9)
        issues = (body.get("issue_summary") or {}).get("issues") or []
        self.assertGreaterEqual(len(issues), 3)
        issue = next(item for item in issues if item["status"] == "OPEN")
        self.assertEqual(issue["plant_code"], "SHANGHAI")
        self.assertEqual(issue["owner_role"], "PLANT_STEWARD")
        self.assertTrue(issue["due_at"])

        acknowledged = steward.patch(
            f"/api/governance/issues/{issue['issue_id']}", headers=self._csrf(steward_csrf),
            json={"status": "ACKNOWLEDGED", "plant_code": "SHANGHAI"},
        )
        self.assertEqual(acknowledged.status_code, 200, acknowledged.get_json())
        resolved = steward.patch(
            f"/api/governance/issues/{issue['issue_id']}", headers=self._csrf(steward_csrf),
            json={
                "status": "RESOLVED", "plant_code": "SHANGHAI",
                "resolution": "已在 SAP 源记录补录并完成主数据管理员复核",
            },
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_json())
        self.assertEqual((resolved.get_json() or {}).get("issue", {}).get("status"), "RESOLVED")

        auditor, auditor_csrf, _principal = self._login("compliance_auditor")
        self.assertEqual(auditor.get("/api/governance/catalog").status_code, 200)
        forbidden = auditor.patch(
            f"/api/governance/issues/{issue['issue_id']}", headers=self._csrf(auditor_csrf),
            json={"status": "OPEN"},
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.get_json())

        admin, admin_csrf, _principal = self._login("group_admin")
        assessed = admin.patch(
            "/api/governance/controls/OPS-01", headers=self._csrf(admin_csrf),
            json={
                "status": "DESIGNED", "maturity_level": 2,
                "notes": "待部署单位完成加密备份和恢复演练后提升成熟度",
            },
        )
        self.assertEqual(assessed.status_code, 200, assessed.get_json())
        self.assertEqual((assessed.get_json() or {}).get("control", {}).get("maturity_level"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
