"""Deterministic resilience and degradation tests for M-AI Master."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


PROJECT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_DIR / "backend"
SAMPLE_CSV = PROJECT_DIR / "备品备件脏数据.csv"
TEST_DIR = tempfile.TemporaryDirectory(prefix="mai-master-chaos-")
os.environ["MDM_DB_PATH"] = str(Path(TEST_DIR.name) / "chaos.db")
os.environ.pop("MDM_AUTH_PASSWORD", None)
for key_name in ("DASHSCOPE_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(key_name, None)
sys.path.insert(0, str(BACKEND_DIR))

import app as backend  # noqa: E402


class ChaosTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        TEST_DIR.cleanup()

    def setUp(self):
        self.client = backend.app.test_client()
        self.client.delete("/api/data", json={"confirm": "DELETE_ALL_DATA"})

    def upload_sample(self):
        with SAMPLE_CSV.open("rb") as source:
            response = self.client.post(
                "/api/upload", data={"file": (io.BytesIO(source.read()), SAMPLE_CSV.name)},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def test_embedding_timeout_degrades_without_crashing(self):
        with patch.dict(backend.semantic_engine.api_keys, {"qwen": "invalid-key"}, clear=False), patch.object(
            backend.requests, "post", side_effect=requests.Timeout("simulated timeout")
        ):
            response = self.client.post("/api/semantic", json={
                "text1": "SKF 6312轴承", "text2": "斯凯孚6312滚动轴承", "model": "qwen",
            })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["embedding_active"])
        self.assertIn("Jaccard", response.get_json()["method"])

    def test_malformed_remote_response_degrades(self):
        class MalformedResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"unexpected": "payload"}

        with patch.dict(backend.semantic_engine.api_keys, {"qwen": "invalid-key"}, clear=False), patch.object(
            backend.requests, "post", return_value=MalformedResponse()
        ):
            response = self.client.post("/api/semantic", json={"text1": "闸阀", "text2": "gate valve", "model": "qwen"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["embedding_active"])

    def test_invalid_ocr_payload_is_rejected(self):
        response = self.client.post("/api/ocr", json={"image": "data:image/png;base64,not-base64"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("base64", response.get_json()["error"])

    def test_ocr_provider_failure_uses_explicit_fallback(self):
        with patch.object(backend.ocr_engine, "_paddle_recognize", return_value=("", None)), patch.object(
            backend.ocr_engine, "_qwen_vl_recognize", return_value=""
        ):
            response = self.client.post("/api/ocr", json={
                "image": "data:image/png;base64,QUE=", "hint_text": "SKF 6312-2RS1 316L",
            })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["real_ocr"])
        self.assertEqual(response.get_json()["provider"], "rule-fallback")
        self.assertTrue(response.get_json()["warning"])

    def test_audit_tampering_is_detected(self):
        state = self.upload_sample()
        batch_id = state["batch"]["batch_id"]
        with backend.db_connect() as conn:
            conn.execute(
                "UPDATE audit_blocks SET payload_json = ? WHERE batch_id = ? AND height = 1",
                ('{"tampered":true}', batch_id),
            )
        verification = self.client.get(f"/api/blockchain/verify?batch_id={batch_id}")
        self.assertEqual(verification.status_code, 200)
        self.assertFalse(verification.get_json()["valid"])
        self.assertTrue(verification.get_json()["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
