"""End-to-end regression tests for the Flask version of M-AI Master."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_DIR / "backend"
SAMPLE_CSV = PROJECT_DIR / "备品备件脏数据.csv"
TEST_DIR = tempfile.TemporaryDirectory(prefix="mai-master-tests-")
os.environ["MDM_DB_PATH"] = str(Path(TEST_DIR.name) / "test.db")
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

    def test_end_to_end_sample_flow(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json()["storage"], "sqlite")
        root_response = self.client.get("/")
        self.assertEqual(root_response.status_code, 200)
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

        capabilities = self.client.get("/api/agent/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        self.assertEqual(len(capabilities.get_json()["workflow"]), 6)

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
            json={"batch_id": batch_id, "instruction": "把上海工厂新增主数据同步到SAP和WMS"},
        )
        self.assertEqual(distributed.status_code, 200, distributed.get_json())
        distributed_payload = distributed.get_json()
        self.assertGreater(distributed_payload["success_count"], 0)
        self.assertEqual(distributed_payload["failed_count"], 0)
        self.assertTrue(all(item["plant_code"] == "SHANGHAI" for item in distributed_payload["logs"]))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
