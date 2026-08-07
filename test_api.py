"""End-to-end regression tests for the Flask version of M-AI Master."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_DIR / "backend"
SAMPLE_CSV = PROJECT_DIR / "备品备件脏数据.csv"
INDUSTRIAL_CSV = PROJECT_DIR / "industrial_parts_dirty_data.csv"
TEST_DIR = tempfile.TemporaryDirectory(prefix="mai-master-tests-")
os.environ["MDM_DB_PATH"] = str(Path(TEST_DIR.name) / "test.db")
os.environ.pop("MDM_AUTH_PASSWORD", None)
os.environ.pop("MDM_PRODUCTION", None)
for api_key_name in ("DASHSCOPE_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(api_key_name, None)
sys.path.insert(0, str(BACKEND_DIR))

import app as backend  # noqa: E402


class FlaskApiTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        TEST_DIR.cleanup()

    def setUp(self):
        self.client = backend.app.test_client()
        response = self.client.delete("/api/data", json={"confirm": "DELETE_ALL_DATA"})
        self.assertEqual(response.status_code, 200)

    def upload_sample(self):
        with SAMPLE_CSV.open("rb") as source:
            response = self.client.post(
                "/api/upload",
                data={"file": (io.BytesIO(source.read()), SAMPLE_CSV.name)},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def test_optional_basic_authentication(self):
        token = base64.b64encode(b"operator:strong-test-password").decode("ascii")
        with patch.object(backend, "AUTH_USER", "operator"), patch.object(
            backend, "AUTH_PASSWORD", "strong-test-password"
        ):
            self.assertEqual(self.client.get("/").status_code, 401)
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            authorized = self.client.get("/", headers={"Authorization": f"Basic {token}"})
            self.assertEqual(authorized.status_code, 200)
            authorized.close()

    def test_delivery_files_use_production_servers_and_persistent_storage(self):
        start_windows = (PROJECT_DIR / "start.bat").read_text(encoding="utf-8")
        start_unix = (PROJECT_DIR / "start.sh").read_text(encoding="utf-8")
        dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
        compose = (PROJECT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        requirements = (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8")
        root_requirements = (PROJECT_DIR / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("MDM_HOST=0.0.0.0", start_windows)
        self.assertIn("MDM_PRODUCTION=1", start_windows)
        self.assertIn("waitress", requirements)
        self.assertIn("backend/requirements.txt", root_requirements)
        self.assertTrue((PROJECT_DIR / "start-ocr.bat").exists())
        self.assertTrue((BACKEND_DIR / "ocr_worker.py").exists())
        self.assertIn("py install -y 3.11", (PROJECT_DIR / "install-ocr.bat").read_text(encoding="utf-8"))
        self.assertFalse((PROJECT_DIR / "evaluation" / "ground_truth_50.csv").exists())
        self.assertIn("gunicorn", start_unix)
        self.assertIn("Darwin", start_unix)
        self.assertTrue((PROJECT_DIR / "start.command").exists())
        self.assertNotIn("MDM_FRONTEND_DIR=/app/frontend", dockerfile)
        self.assertIn("/app/data", dockerfile)
        self.assertIn("./runtime/data:/app/data", compose)

    def test_dynamic_field_mapping_preserves_industrial_extensions(self):
        self.assertEqual(
            backend.engine.extract_material("STAINLESS STEEL 316L PRESSURE RATING"), "316L"
        )
        frame = backend.pd.read_csv(INDUSTRIAL_CSV, encoding="utf-8-sig")
        mapping = backend.map_fields(frame.columns.tolist())
        self.assertEqual(mapping["material_code"], "物料编码")
        self.assertEqual(mapping["material_name"], "物料名称")
        self.assertEqual(mapping["description"], "物料描述")
        self.assertEqual(mapping["category"], "物料类别")
        self.assertEqual(mapping["unit"], "计量单位")
        self.assertNotIn("system_source", mapping)
        self.assertNotIn("create_time", mapping)

        payloads = []
        for _ in range(2):
            with INDUSTRIAL_CSV.open("rb") as source:
                response = self.client.post(
                    "/api/upload",
                    data={"file": (io.BytesIO(source.read()), INDUSTRIAL_CSV.name)},
                    content_type="multipart/form-data",
                )
            self.assertEqual(response.status_code, 201, response.get_json())
            payloads.append(response.get_json())

        self.assertEqual(payloads[0]["summary"], payloads[1]["summary"])
        self.assertEqual(payloads[0]["summary"]["record_count"], len(frame))
        first = payloads[0]["records"][0]
        self.assertEqual(first["system_source"], "CSV导入")
        self.assertTrue(first["create_time"])
        self.assertEqual(first["category"], frame.iloc[0]["物料类别"])
        raw_fields = first["_ext"]["raw_fields"]
        for column in ("物料子类", "参考价格", "库存数量", "供应商", "规格型号", "状态"):
            self.assertIn(column, raw_fields)

        duplicate_mapping = {
            "material_code": "物料编码", "material_name": "物料名称", "description": "物料名称"
        }
        normalized = backend.normalize_records(frame.head(1).to_dict(orient="records"), duplicate_mapping)
        self.assertEqual(normalized[0]["material_name"], frame.iloc[0]["物料名称"])
        self.assertEqual(normalized[0]["description"], "")

    def test_end_to_end_sample_flow(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json()["storage"], "sqlite")
        self.assertEqual(health.get_json()["version"], "4.4")
        self.assertTrue(health.get_json()["ready"])
        self.assertFalse(health.get_json()["authentication"])
        root_response = self.client.get("/")
        self.assertEqual(root_response.status_code, 200)
        root_html = root_response.get_data(as_text=True)
        self.assertIn("workflowDefinition", root_html)
        self.assertIn('/agent/capabilities', root_html)
        self.assertIn("getWorkflowSteps", root_html)
        self.assertIn('id="plantView"', root_html)
        self.assertIn("standard_kb", root_html)
        self.assertIn('/ocr/install', root_html)
        self.assertIn('ocrProcessing', root_html)
        self.assertIn('正在识别图片...', root_html)
        self.assertIn("ACTIVE_BATCH_KEY", root_html)
        self.assertIn("getActiveBatchId", root_html)
        self.assertIn("AUTO_FIELD_DEFAULTS", root_html)
        self.assertIn("out._extra", root_html)
        self.assertEqual(root_response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(root_response.headers["Cache-Control"], "no-store")
        root_response.close()

        state = self.upload_sample()
        self.assertEqual(
            state["summary"],
            {
                "record_count": 234,
                "master_count": 72,
                "review_count": 8,
                "auto_merge_count": 42,
                "new_count": 22,
                "compression_rate": 69.2,
            },
        )
        self.assertEqual(len(state["mappings"]), 234)
        self.assertTrue(state["records"][0]["_ext"])
        self.assertFalse(state["governance"]["embedding_active"])
        self.assertIn("Jaccard", state["governance"]["method"])
        quality_systems = state["quality_report"]["systems"]
        self.assertEqual(sum(item["recordCount"] for item in quality_systems.values()), 234)
        for item in quality_systems.values():
            self.assertTrue({"completeness", "accuracy", "consistency", "score"}.issubset(item))
        self.assertFalse(any("润滑油" in item["standard_name"] and "MPa" in item["standard_name"] for item in state["masters"]))
        self.assertFalse(any("DN2 " in item["standard_name"] for item in state["masters"]))
        batch_id = state["batch"]["batch_id"]

        search = self.client.post(
            "/api/search",
            json={"query": "阀门 DN50 碳钢", "batch_id": batch_id, "match_mode": "and"},
        )
        self.assertEqual(search.status_code, 200)
        search_payload = search.get_json()
        condition_types = {item["type"] for item in search_payload["conditions"]}
        self.assertTrue({"category", "dn", "material"}.issubset(condition_types))
        self.assertEqual(search_payload["total"], 0)

        existing = self.client.post(
            "/api/search", json={"query": "机械密封", "batch_id": batch_id, "match_mode": "and"}
        )
        self.assertEqual(existing.status_code, 200)
        self.assertGreater(existing.get_json()["total"], 0)

        review_id = state["reviews"][0]["id"]
        reviewed = self.client.patch(f"/api/reviews/{review_id}", json={"action": "MERGE"})
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.get_json()["summary"]["review_count"], 7)

        created = self.client.post(
            "/api/lifecycle",
            json={
                "batch_id": batch_id, "name": "DN80 测试闸阀", "category": "闸阀",
                "brand": "TEST-BRAND", "model": "TEST-MODEL-X", "reason": "回归测试",
            },
        )
        self.assertEqual(created.status_code, 201)
        lifecycle_id = created.get_json()["id"]
        bypassed = self.client.patch(
            f"/api/lifecycle/{lifecycle_id}", json={"status": "APPROVED", "reviewer": "审批人"}
        )
        self.assertEqual(bypassed.status_code, 409)
        wrong_reviewer = self.client.patch(
            f"/api/lifecycle/{lifecycle_id}", json={"status": "REVIEWED", "reviewer": "审批人"}
        )
        self.assertEqual(wrong_reviewer.status_code, 403)
        initial_review = self.client.patch(
            f"/api/lifecycle/{lifecycle_id}", json={"status": "REVIEWED", "reviewer": "数据管理员"}
        )
        self.assertEqual(initial_review.status_code, 200)
        approved = self.client.patch(
            f"/api/lifecycle/{lifecycle_id}", json={"status": "APPROVED", "reviewer": "审批人"}
        )
        self.assertEqual(approved.status_code, 200)
        lifecycle = next(item for item in approved.get_json()["lifecycle"] if item["id"] == lifecycle_id)
        self.assertEqual(lifecycle["status"], "APPROVED")
        self.assertTrue(lifecycle["mdm_code"].startswith("MDM-53-"))
        lifecycle_master = next(item for item in approved.get_json()["masters"] if item["mdm_code"] == lifecycle["mdm_code"])
        self.assertEqual(lifecycle_master["brand"], "TEST-BRAND")
        self.assertEqual(lifecycle_master["model"], "TEST-MODEL-X")
        self.assertIn("DN80 测试闸阀", lifecycle_master["standard_name"])

        master_codes = [item["mdm_code"] for item in state["masters"][:2]]
        distributed = self.client.post(
            "/api/distribute",
            json={"batch_id": batch_id, "target_systems": ["SAP", "EAM"], "master_codes": master_codes},
        )
        self.assertEqual(distributed.status_code, 200)
        self.assertEqual(distributed.get_json()["success_count"], 4)

    def test_batch_snapshots_are_stable(self):
        first = self.upload_sample()
        first_batch_id = first["batch"]["batch_id"]
        first_codes = [item["mdm_code"] for item in first["masters"]]

        second = self.client.post(
            "/api/batches",
            json={
                "filename": "small.csv",
                "records": [
                    {"material_code": "A-1", "system_source": "SAP", "material_name": "DN50 闸阀", "description": "WCB PN16"},
                    {"material_code": "A-2", "system_source": "EAM", "material_name": "Gate Valve DN50", "description": "carbon steel PN16"},
                ],
            },
        )
        self.assertEqual(second.status_code, 201, second.get_json())

        reloaded = self.client.get(f"/api/batches/{first_batch_id}")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.get_json()["summary"]["master_count"], 72)
        self.assertEqual([item["mdm_code"] for item in reloaded.get_json()["masters"]], first_codes)

    def test_validation_and_delete_guards(self):
        self.assertEqual(self.client.post("/api/search", json={"query": "test"}).status_code, 200)
        self.assertEqual(self.client.post("/api/batches", json={"records": []}).status_code, 400)
        self.assertEqual(self.client.post("/api/upload", data={}).status_code, 400)
        self.assertEqual(self.client.delete("/api/data", json={}).status_code, 400)
        empty = self.client.get("/api/state/latest").get_json()
        self.assertIsNone(empty["batch"])
        self.assertEqual(empty["summary"]["record_count"], 0)

    def test_ai_semantic_ocr_and_governance_contracts(self):
        health = self.client.get("/api/health").get_json()
        self.assertEqual(health["semantic"]["model"], "text-embedding-v3")
        self.assertEqual(health["semantic"]["dimension"], 1024)
        self.assertEqual(health["semantic"]["configured_models"], [])
        self.assertEqual(health["capabilities"]["standard_rag"]["count"], 10)

        semantic = self.client.post(
            "/api/semantic",
            json={"text1": "SKF 6312轴承", "text2": "斯凯孚6312滚动轴承", "model": "qwen"},
        )
        self.assertEqual(semantic.status_code, 200)
        semantic_payload = semantic.get_json()
        self.assertFalse(semantic_payload["embedding_active"])
        self.assertIn("Jaccard", semantic_payload["method"])
        self.assertIn("warning", semantic_payload)
        self.assertEqual(self.client.post(
            "/api/semantic", json={"text1": "a", "text2": "b", "model": "unknown"}
        ).status_code, 400)

        governed = self.client.post("/api/govern", json={"model": "qwen"})
        self.assertEqual(governed.status_code, 200)
        governed_payload = governed.get_json()
        self.assertEqual(governed_payload["total_records"], 234)
        self.assertEqual(governed_payload["golden_master_count"], 72)
        self.assertIn("Jaccard", governed_payload["method"])

        ocr = self.client.post(
            "/api/ocr",
            json={"image": "data:image/png;base64,AA==", "hint_text": "SKF 6312-2RS1 316L", "plant_code": "BEIJING"},
        )
        self.assertEqual(ocr.status_code, 200)
        ocr_payload = ocr.get_json()
        self.assertEqual(ocr_payload["plant_code"], "BEIJING")
        self.assertEqual(ocr_payload["brand"], "SKF")
        self.assertEqual(ocr_payload["model"], "6312-2RS1")
        self.assertTrue(ocr_payload["standard_name_preview"])
        self.assertFalse(ocr_payload["real_ocr"])
        self.assertEqual(ocr_payload["provider"], "rule-fallback")

        classified = self.client.post(
            "/api/classify",
            json={"material_name": "SKF 6312深沟球轴承", "description": "滚动轴承 316L", "plant_code": "SHANGHAI"},
        )
        self.assertEqual(classified.status_code, 200)
        classified_payload = classified.get_json()
        self.assertEqual(classified_payload["recommended_category"], "深沟球轴承")
        self.assertEqual(classified_payload["standard"], "SY/T5497-2018")
        self.assertEqual(classified_payload["plant_code"], "SHANGHAI")
        self.assertEqual(len(classified_payload["candidates"]), 3)
        self.assertEqual(classified_payload["rag"]["namespace"], "standard_kb")
        self.assertEqual(len(classified_payload["standard_references"]), 3)

        standard_search = self.client.post("/api/standards/search", json={
            "query": "DN150 铸钢法兰闸阀", "top_k": 3,
        })
        self.assertEqual(standard_search.status_code, 200, standard_search.get_json())
        self.assertEqual(standard_search.get_json()["namespace"], "standard_kb")
        self.assertEqual(len(standard_search.get_json()["results"]), 3)
        self.assertEqual(self.client.get("/api/standards/stats").get_json()["count"], 10)

        capabilities = self.client.get("/api/agent/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        capability_payload = capabilities.get_json()
        self.assertEqual(len(capability_payload["workflow"]), 8)
        self.assertEqual(capability_payload["workflow"][-1]["code"], "FEEDBACK")

    def test_real_ocr_adapter_and_explanation(self):
        with patch.object(backend.ocr_engine, "_paddle_recognize", return_value=(
            "SKF 6312-2RS1 深沟球轴承 316L 1.6MPa", 0.94
        )):
            ocr = self.client.post(
                "/api/ocr", data={"image": (io.BytesIO(b"real-image-bytes"), "nameplate.jpg")},
                content_type="multipart/form-data",
            )
        self.assertEqual(ocr.status_code, 200, ocr.get_json())
        self.assertTrue(ocr.get_json()["real_ocr"])
        self.assertEqual(ocr.get_json()["provider"], "paddleocr-local")
        self.assertEqual(ocr.get_json()["brand"], "SKF")
        self.assertEqual(ocr.get_json()["pressure"], "1.6MPa")

        state = self.upload_sample()
        explanation = self.client.post("/api/explain", json={
            "batch_id": state["batch"]["batch_id"], "mdm_code": state["masters"][0]["mdm_code"],
            "use_llm": False,
        })
        self.assertEqual(explanation.status_code, 200, explanation.get_json())
        payload = explanation.get_json()
        self.assertIn(state["masters"][0]["mdm_code"], payload["explanation"])
        self.assertEqual(payload["runtime"]["method"], "事实模板解释（降级）")
        self.assertTrue(payload["runtime"]["evidence_hash"])

    def test_ground_truth_evaluation_module_is_removed(self):
        self.assertEqual(self.client.get("/api/evaluation/ground-truth").status_code, 404)
        self.assertEqual(self.client.post("/api/evaluation/run", json={}).status_code, 404)
        capabilities = self.client.get("/api/health").get_json()["capabilities"]
        self.assertNotIn("ground_truth_evaluation", capabilities)
        page_response = self.client.get("/")
        page = page_response.get_data(as_text=True)
        page_response.close()
        self.assertNotIn("/evaluation/run", page)
        self.assertNotIn("50 条人工真值集评测", page)

    def test_qwen_embedding_request_and_native_dimension(self):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"output": {"embeddings": [
                    {"text_index": 0, "embedding": [0.1] * 1024},
                    {"text_index": 1, "embedding": [0.2] * 1024},
                ]}}

        semantic = backend.SemanticEngine()
        semantic.api_keys["qwen"] = "test-key"
        with patch.object(backend.requests, "post", return_value=FakeResponse()) as mocked_post:
            vectors, metadata = semantic.resolve_embeddings(["机械密封", "端面密封"], "qwen")
        self.assertEqual(len(vectors), 2)
        self.assertTrue(all(len(vector) == 1024 for vector in vectors))
        self.assertEqual(metadata["dimension"], 1024)
        self.assertTrue(metadata["embedding_active"])
        request_payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(request_payload["model"], "text-embedding-v3")
        self.assertEqual(request_payload["parameters"]["dimension"], 1024)

    def test_multi_plant_collaboration_and_natural_language_distribution(self):
        response = self.client.post(
            "/api/batches",
            json={
                "filename": "multi-plant.json",
                "records": [
                    {"material_code": "SH-01", "system_source": "SAP", "material_name": "SKF 6312轴承", "description": "6312 深沟球轴承", "plant_code": "SHANGHAI"},
                    {"material_code": "BJ-01", "system_source": "EAM", "material_name": "SKF 6312滚动轴承", "description": "bearing 6312", "plant_code": "BEIJING"},
                    {"material_code": "SH-02", "system_source": "SAP", "material_name": "Z41H-16C DN50闸阀", "description": "PN16 铸钢", "plant_code": "SHANGHAI"},
                    {"material_code": "BJ-02", "system_source": "EAM", "material_name": "DN50 Gate Valve Z41H-16C", "description": "PN16 carbon steel", "plant_code": "BEIJING"},
                ],
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        group_state = response.get_json()
        batch_id = group_state["batch"]["batch_id"]
        self.assertEqual(group_state["summary"]["record_count"], 4)
        self.assertEqual({item["plant_code"] for item in group_state["records"]}, {"SHANGHAI", "BEIJING"})

        shanghai = self.client.get(f"/api/batches/{batch_id}?plant_code=SHANGHAI").get_json()
        beijing = self.client.get(f"/api/batches/{batch_id}?plant_code=BEIJING").get_json()
        self.assertEqual(shanghai["summary"]["record_count"], 2)
        self.assertEqual(beijing["summary"]["record_count"], 2)

        collaboration = self.client.get(f"/api/plants?batch_id={batch_id}").get_json()
        self.assertEqual({item["plant_code"] for item in collaboration["plants"]}, {"GROUP", "SHANGHAI", "BEIJING"})
        self.assertGreaterEqual(collaboration["collaboration"]["shared_golden_masters"], 1)
        self.assertGreaterEqual(collaboration["collaboration"]["avoided_duplicate_codes"], 1)
        self.assertGreaterEqual(collaboration["collaboration"]["approved_golden_masters"], 1)
        self.assertGreaterEqual(collaboration["collaboration"]["potential_plant_deliveries"], 2)

        intent = self.client.post(
            "/api/intent", json={"text": "把上海工厂新增主数据同步到SAP和WMS"}
        ).get_json()
        self.assertEqual(intent["plant_code"], "SHANGHAI")
        self.assertEqual(intent["target_systems"], ["SAP", "WMS"])
        self.assertEqual(intent["mode"], "INCREMENTAL")

        distributed = self.client.post(
            "/api/distribute",
            json={
                "batch_id": batch_id, "instruction": "把上海工厂新增主数据同步到SAP和WMS",
                "confirmed": True,
            },
        )
        self.assertEqual(distributed.status_code, 200, distributed.get_json())
        distributed_payload = distributed.get_json()
        self.assertGreater(distributed_payload["success_count"], 0)
        self.assertEqual(distributed_payload["failed_count"], 0)
        self.assertTrue(all(item["plant_code"] == "SHANGHAI" for item in distributed_payload["logs"]))

    def test_precise_natural_language_distribution_requires_confirmation(self):
        response = self.client.post("/api/batches", json={
            "filename": "precise-distribution.json", "plant_code": "GROUP", "records": [
                {"material_code": "P-01", "system_source": "SAP", "material_name": "SKF 6312深沟球轴承", "description": "SKF bearing 6312"},
                {"material_code": "P-02", "system_source": "EAM", "material_name": "FAG 6205深沟球轴承", "description": "FAG bearing 6205"},
            ],
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        batch_id = response.get_json()["batch"]["batch_id"]
        instruction = "帮我把【品牌 SKF】【型号 6312】【备品备件名称 深沟球轴承】发往【ERP系统】【上海厂】"
        plan_response = self.client.post("/api/intent", json={"batch_id": batch_id, "text": instruction})
        self.assertEqual(plan_response.status_code, 200, plan_response.get_json())
        plan = plan_response.get_json()
        self.assertEqual(plan["filters"], {"brand": "SKF", "model": "6312", "material_name": "深沟球轴承"})
        self.assertEqual(plan["target_systems"], ["ERP"])
        self.assertEqual(plan["target_plants"], ["SHANGHAI"])
        self.assertEqual(plan["matched_master_count"], 1)
        self.assertTrue(plan["requires_confirmation"])
        self.assertEqual(self.client.get(f"/api/batches/{batch_id}").get_json()["distribution_logs"], [])

        unconfirmed = self.client.post("/api/distribute", json={"batch_id": batch_id, "instruction": instruction})
        self.assertEqual(unconfirmed.status_code, 409, unconfirmed.get_json())
        false_string = self.client.post("/api/distribute", json={
            "batch_id": batch_id, "instruction": instruction, "confirmed": "false",
        })
        self.assertEqual(false_string.status_code, 409, false_string.get_json())
        executed = self.client.post("/api/distribute", json={
            "batch_id": batch_id, "instruction": instruction, "confirmed": True,
        })
        self.assertEqual(executed.status_code, 200, executed.get_json())
        result = executed.get_json()
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(len(result["logs"]), 1)
        self.assertEqual(result["logs"][0]["target_system"], "ERP")
        self.assertEqual(result["logs"][0]["plant_code"], "SHANGHAI")
        self.assertEqual(result["logs"][0]["payload"]["brand"], "SKF")
        self.assertEqual(result["logs"][0]["payload"]["model"], "6312")
        self.assertRegex(result["logs"][0]["payload"]["payload_hash"], r"^[0-9a-f]{64}$")

    def test_group_golden_master_can_be_distributed_to_factories(self):
        response = self.client.post(
            "/api/batches",
            json={
                "filename": "group-master.json",
                "plant_code": "GROUP",
                "records": [
                    {"material_code": "HQ-01", "system_source": "SAP", "material_name": "SKF 6205深沟球轴承", "description": "集团标准物料"},
                ],
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        state = response.get_json()
        master_code = state["masters"][0]["mdm_code"]
        distributed = self.client.post(
            "/api/distribute",
            json={
                "batch_id": state["batch"]["batch_id"],
                "plant_code": "SHANGHAI",
                "target_systems": ["SAP系统"],
                "master_codes": [master_code],
            },
        )
        self.assertEqual(distributed.status_code, 200, distributed.get_json())
        payload = distributed.get_json()
        self.assertEqual(payload["plant_name"], "上海工厂")
        self.assertEqual(payload["success_count"], 1)
        self.assertEqual(payload["failed_count"], 0)
        self.assertEqual(payload["logs"][0]["plant_code"], "SHANGHAI")

        rejected = self.client.post(
            "/api/distribute",
            json={
                "batch_id": state["batch"]["batch_id"],
                "plant_code": "UNKNOWN",
                "target_systems": ["SAP系统"],
                "master_codes": [master_code],
            },
        )
        self.assertEqual(rejected.status_code, 400)

    def test_persistent_vector_store_and_search(self):
        state = self.upload_sample()
        batch_id = state["batch"]["batch_id"]
        self.assertEqual(state["vector_index"]["count"], 72)
        self.assertEqual(state["vector_index"]["indexes"][0]["model"], "feature-hash-v1")

        search = self.client.post("/api/vectors/search", json={
            "batch_id": batch_id, "query": "SKF 6312 轴承", "model": "qwen", "top_k": 5,
        })
        self.assertEqual(search.status_code, 200, search.get_json())
        payload = search.get_json()
        self.assertEqual(payload["provider"], "local")
        self.assertEqual(payload["dimension"], 384)
        self.assertEqual(len(payload["results"]), 5)
        self.assertGreaterEqual(payload["results"][0]["score"], payload["results"][-1]["score"])

        rebuilt = self.client.post("/api/vectors/rebuild", json={"batch_id": batch_id, "model": "local"})
        self.assertEqual(rebuilt.status_code, 200)
        self.assertEqual(rebuilt.get_json()["indexed"], 72)
        stats = self.client.get(f"/api/vectors/stats?batch_id={batch_id}").get_json()
        self.assertEqual(stats["count"], 72)

    def test_networkx_graph_and_lineage(self):
        state = self.upload_sample()
        batch_id = state["batch"]["batch_id"]
        response = self.client.get(f"/api/graph?batch_id={batch_id}&raw_limit=30")
        self.assertEqual(response.status_code, 200)
        graph = response.get_json()
        self.assertEqual(graph["engine"], "NetworkX")
        self.assertGreater(graph["stats"]["nodes"], 72)
        self.assertGreater(graph["stats"]["edges"], 72)
        self.assertIn("MASTER", graph["stats"]["types"])
        self.assertTrue({"SYSTEM", "RAW", "MASTER", "BATCH", "AUDIT", "WORKFLOW_STEP"}.issubset(graph["stats"]["types"]))
        self.assertTrue({"PROVIDES_RECORD", "MAPPED_TO", "INGESTED_RECORD", "FINGERPRINTS", "PREVIOUS_HASH"}.issubset(
            {edge["relation"] for edge in graph["edges"]}
        ))
        self.assertTrue(graph["stats"]["audit"]["valid"])
        self.assertTrue(all("x" in node and "centrality" in node for node in graph["nodes"]))
        workflow_nodes = [node for node in graph["nodes"] if node["node_type"] == "WORKFLOW_STEP"]
        self.assertEqual(len(workflow_nodes), 8)
        self.assertTrue(all(node["fingerprint_count"] >= 1 for node in workflow_nodes))
        self.assertTrue(all(node["chain_valid"] for node in workflow_nodes))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", node["latest_block_hash"]) for node in workflow_nodes))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", node["latest_payload_hash"]) for node in workflow_nodes))
        first_report = self.client.get(f"/api/reports/governance?batch_id={batch_id}").get_json()
        second_report = self.client.get(f"/api/reports/governance?batch_id={batch_id}").get_json()
        self.assertEqual(first_report["report_hash"], second_report["report_hash"])
        self.assertNotEqual(first_report["generated_at"], "")

        mdm_code = state["masters"][0]["mdm_code"]
        lineage = self.client.get(f"/api/graph/lineage/{mdm_code}?batch_id={batch_id}")
        self.assertEqual(lineage.status_code, 200)
        lineage_payload = lineage.get_json()
        self.assertGreater(lineage_payload["degree"], 0)
        self.assertTrue(lineage_payload["source_records"])
        self.assertTrue(lineage_payload["audit_proofs"])

        record_id = state["mappings"][0]["record_id"]
        source = self.client.get(f"/api/lineage/source/{record_id}")
        self.assertEqual(source.status_code, 200, source.get_json())
        source_payload = source.get_json()
        self.assertTrue(source_payload["provenance"]["source_record_id"])
        self.assertTrue(source_payload["provenance"]["source_table"])
        self.assertRegex(source_payload["provenance"]["record_hash"], r"^[0-9a-f]{64}$")
        self.assertTrue(source_payload["mappings"])
        self.assertEqual(self.client.get("/api/lineage/source/999999").status_code, 404)

    def test_source_lineage_preserves_business_key_and_factory_scope(self):
        response = self.client.post("/api/batches", json={
            "filename": "source-lineage.json", "records": [
                {"material_code": "SH-L-01", "system_source": "SAP", "material_name": "上海轴承",
                 "description": "6205 bearing", "plant_code": "SHANGHAI",
                 "_extra": {"source_record_id": "MARA-SH-001", "source_table": "MARA"}},
                {"material_code": "BJ-L-01", "system_source": "EAM", "material_name": "北京闸阀",
                 "description": "DN50 gate valve", "plant_code": "BEIJING",
                 "_extra": {"source_record_id": "EAM-BJ-001", "source_table": "EAM_MATERIAL"}},
            ],
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        state = response.get_json()
        self.assertEqual(state["quality_report"]["lineage"]["traceable_records"], 2)
        self.assertRegex(state["quality_report"]["lineage"]["source_fingerprint_root"], r"^[0-9a-f]{64}$")
        by_plant = {item["plant_code"]: item for item in state["records"]}
        shanghai = self.client.get(f"/api/lineage/source/{by_plant['SHANGHAI']['id']}?plant_code=SHANGHAI")
        self.assertEqual(shanghai.status_code, 200, shanghai.get_json())
        self.assertEqual(shanghai.get_json()["provenance"]["source_record_id"], "MARA-SH-001")
        self.assertEqual(shanghai.get_json()["provenance"]["source_table"], "MARA")
        forbidden = self.client.get(f"/api/lineage/source/{by_plant['BEIJING']['id']}?plant_code=SHANGHAI")
        self.assertEqual(forbidden.status_code, 403, forbidden.get_json())

    def test_factory_lineage_hides_other_factory_delivery_and_feedback(self):
        response = self.client.post("/api/batches", json={
            "filename": "factory-scope.json", "plant_code": "GROUP", "records": [
                {"material_code": "HQ-SCOPE-01", "system_source": "SAP",
                 "material_name": "SKF 6205深沟球轴承", "description": "上海来源", "plant_code": "SHANGHAI"},
                {"material_code": "HQ-SCOPE-02", "system_source": "EAM",
                 "material_name": "SKF 6205深沟球轴承", "description": "北京来源", "plant_code": "BEIJING"},
            ],
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        state = response.get_json()
        batch_id = state["batch"]["batch_id"]
        master_code = state["masters"][0]["mdm_code"]
        record_id = next(item["id"] for item in state["records"] if item["plant_code"] == "SHANGHAI")
        distributed = self.client.post("/api/distribute", json={
            "batch_id": batch_id, "target_systems": ["ERP"],
            "target_plants": ["SHANGHAI", "BEIJING"], "master_codes": [master_code],
        })
        self.assertEqual(distributed.status_code, 200, distributed.get_json())
        for plant, comment in (("SHANGHAI", "上海可复用"), ("BEIJING", "北京内部备注")):
            feedback = self.client.post("/api/feedback", json={
                "batch_id": batch_id, "plant_code": plant, "mdm_code": master_code,
                "accepted": True, "rating": 5, "comment": comment,
            })
            self.assertEqual(feedback.status_code, 201, feedback.get_json())
        source = self.client.get(f"/api/lineage/source/{record_id}?plant_code=SHANGHAI").get_json()
        self.assertTrue(source["distributions"])
        self.assertTrue(source["feedback"])
        self.assertTrue(all(item["plant_code"] == "SHANGHAI" for item in source["distributions"]))
        self.assertTrue(all(item["plant_code"] == "SHANGHAI" for item in source["feedback"]))
        self.assertNotIn("北京内部备注", json.dumps(source, ensure_ascii=False))
        graph = self.client.get(f"/api/graph?batch_id={batch_id}&plant_code=SHANGHAI").get_json()
        scoped_nodes = [item for item in graph["nodes"] if item["node_type"] in {"TARGET_SYSTEM", "FEEDBACK", "PLANT"}]
        self.assertNotIn("BEIJING", {item.get("entity_id") for item in scoped_nodes})

    def test_qwen_agent_fallback_and_traceability(self):
        response = self.client.post("/api/agent/plan", json={"task": "治理新批次并分发到上海工厂"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["runtime"]["active"])
        self.assertEqual(payload["runtime"]["method"], "确定性Agent编排（降级）")
        self.assertEqual(len(payload["plan"]["steps"]), 8)
        self.assertTrue(response.headers.get("X-Trace-Id", "").startswith("TRC-"))

    def test_one_click_competition_demo_completes_all_eight_steps(self):
        response = self.client.post("/api/demo/run", json={})
        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["summary"]["record_count"], 4)
        self.assertEqual(len(payload["workflow"]["steps"]), 8)
        self.assertTrue(payload["workflow"]["closed_loop"])
        self.assertTrue(all(step["status"] == "COMPLETED" for step in payload["workflow"]["steps"]))
        self.assertEqual(payload["demo_execution"]["factory_feedback"], 2)
        self.assertGreaterEqual(payload["demo_execution"]["distribution_tasks"], 4)
        self.assertTrue(payload["audit_chain"]["valid"])

    def test_audit_chain_and_factory_feedback_close_loop(self):
        response = self.client.post("/api/batches", json={
            "filename": "closed-loop.json", "plant_code": "GROUP", "records": [
                {"material_code": "HQ-CL-01", "system_source": "SAP", "material_name": "SKF 6205深沟球轴承",
                 "description": "集团标准 6205 bearing"}
            ],
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        state = response.get_json()
        batch_id = state["batch"]["batch_id"]
        master_code = state["masters"][0]["mdm_code"]
        self.assertEqual(len(state["workflow"]["steps"]), 8)
        self.assertFalse(state["workflow"]["closed_loop"])
        self.assertTrue(state["audit_chain"]["valid"])

        distributed = self.client.post("/api/distribute", json={
            "batch_id": batch_id, "plant_code": "SHANGHAI", "target_systems": ["SAP系统"],
            "master_codes": [master_code], "instruction": "同步到上海工厂SAP", "confirmed": True,
        })
        self.assertEqual(distributed.status_code, 200, distributed.get_json())
        self.assertEqual(distributed.get_json()["success_count"], 1)

        rejected = self.client.post("/api/feedback", json={
            "batch_id": batch_id, "plant_code": "SHANGHAI", "mdm_code": master_code,
            "accepted": False, "rating": 2, "comment": "现场规格不一致", "actor": "上海工厂数据管理员",
        })
        self.assertEqual(rejected.status_code, 201, rejected.get_json())
        self.assertFalse(rejected.get_json()["closed_loop"])
        self.assertEqual(rejected.get_json()["workflow"]["steps"][-1]["status"], "ACTION_REQUIRED")
        invalid_boolean = self.client.post("/api/feedback", json={
            "batch_id": batch_id, "plant_code": "SHANGHAI", "mdm_code": master_code,
            "accepted": "false", "rating": 2,
        })
        self.assertEqual(invalid_boolean.status_code, 400, invalid_boolean.get_json())

        feedback = self.client.post("/api/feedback", json={
            "batch_id": batch_id, "plant_code": "SHANGHAI", "mdm_code": master_code,
            "accepted": True, "rating": 5, "comment": "现场型号和规格一致，可复用", "actor": "上海工厂数据管理员",
        })
        self.assertEqual(feedback.status_code, 201, feedback.get_json())
        feedback_payload = feedback.get_json()
        self.assertTrue(feedback_payload["closed_loop"])
        self.assertEqual(feedback_payload["knowledge_refresh"]["model"], "feature-hash-v1")

        verification = self.client.get(f"/api/blockchain/verify?batch_id={batch_id}").get_json()
        self.assertTrue(verification["valid"])
        self.assertGreaterEqual(verification["block_count"], 3)
        blocks = self.client.get(f"/api/blockchain/blocks?batch_id={batch_id}").get_json()
        self.assertEqual(blocks["blocks"][0]["event_type"], "FACTORY_FEEDBACK")
        self.assertTrue(blocks["verification"]["valid"])
        workflow_codes = {step["step_code"] for step in feedback_payload["workflow"]["steps"]}
        fingerprinted_codes = {
            block["entity_id"] for block in blocks["blocks"] if block["entity_type"] == "WORKFLOW_STEP"
        }
        self.assertEqual(fingerprinted_codes, workflow_codes)
        graph = self.client.get(f"/api/graph?batch_id={batch_id}&raw_limit=10").get_json()
        audit_nodes = [node for node in graph["nodes"] if node["node_type"] == "AUDIT"]
        self.assertEqual(len(audit_nodes), blocks["verification"]["block_count"])
        self.assertEqual(
            sum(edge["relation"] == "PREVIOUS_HASH" for edge in graph["edges"]),
            blocks["verification"]["block_count"] - 1,
        )
        self.assertTrue(all(
            len(node["block_hash"]) == len(node["payload_hash"]) == len(node["merkle_root"]) == 64
            for node in audit_nodes
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
