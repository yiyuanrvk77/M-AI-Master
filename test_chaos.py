"""Deterministic resilience and degradation tests for M-AI Master."""

from __future__ import annotations

import io
import json
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
os.environ["MDM_SECURITY_MODE"] = "open"
os.environ["MDM_SECURITY_DIR"] = TEST_DIR.name
os.environ.pop("MDM_AUTH_PASSWORD", None)
for key_name in ("DASHSCOPE_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY"):
    # Keep degradation tests offline even when the developer has a local .env.
    os.environ[key_name] = ""
sys.path.insert(0, str(BACKEND_DIR))

import app as backend  # noqa: E402


class ChaosTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.DB_PATH = Path(TEST_DIR.name) / "chaos.db"
        backend.init_db()

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

    def test_ocr_auto_install_is_local_only_and_asynchronous(self):
        remote = self.client.post("/api/ocr/install", environ_base={"REMOTE_ADDR": "192.168.1.20"})
        self.assertEqual(remote.status_code, 403)
        proxied = self.client.post(
            "/api/ocr/install",
            headers={"X-Forwarded-For": "203.0.113.20"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(proxied.status_code, 403)
        mocked = {
            "state": "installing", "progress": 10, "runtime_ready": False, "installing": True,
            "platform_supported": True, "auto_install_enabled": True, "qwen_vl_configured": False,
        }
        with patch.object(backend.ocr_installer, "start", return_value=mocked):
            local = self.client.post("/api/ocr/install", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(local.status_code, 202)
        self.assertTrue(local.get_json()["trigger_allowed"])

    def test_isolated_python_ocr_worker_result_is_consumed(self):
        class WorkerResult:
            returncode = 0
            stderr = ""
            stdout = "worker log\nMAI_OCR_RESULT=" + json.dumps({
                "ready": True, "text": "SKF 6312 轴承", "confidence": 0.96,
            }, ensure_ascii=False)

        with patch.object(backend.ocr_engine, "_load_paddle") as load_paddle, patch.object(
            backend, "_external_ocr_ready", return_value=True
        ), patch.object(backend.subprocess, "run", return_value=WorkerResult()):
            text, confidence = backend.ocr_engine._paddle_recognize(b"image", "nameplate.jpg")
        load_paddle.assert_not_called()
        self.assertEqual(text, "SKF 6312 轴承")
        self.assertEqual(confidence, 0.96)

    def test_stale_ocr_install_lock_is_cleared(self):
        stale_lock = Path(TEST_DIR.name) / "stale-ocr.lock"
        stale_lock.mkdir()
        stale_time = backend.time.time() - 7200
        os.utime(stale_lock, (stale_time, stale_time))
        with patch.object(backend, "OCR_INSTALL_LOCK", stale_lock), patch.object(
            backend, "_external_ocr_ready", return_value=False
        ), patch.object(backend.importlib.util, "find_spec", return_value=None), patch.dict(
            os.environ, {"MDM_OCR_INSTALL_LOCK_SECONDS": "3600"}
        ):
            backend.ocr_installer._process = None
            backend.ocr_installer._exit_code = None
            status = backend.ocr_installer.status()
        self.assertFalse(status["installing"])
        self.assertFalse(stale_lock.exists())

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
        graph = self.client.get(f"/api/graph?batch_id={batch_id}&raw_limit=5").get_json()
        self.assertFalse(graph["stats"]["audit"]["valid"])
        audit_nodes = [node for node in graph["nodes"] if node["node_type"] == "AUDIT"]
        self.assertTrue(audit_nodes)
        self.assertTrue(all(not node["verified"] for node in audit_nodes))

    def test_workflow_state_tampering_is_detected(self):
        state = self.upload_sample()
        batch_id = state["batch"]["batch_id"]
        with backend.db_connect() as conn:
            conn.execute(
                """UPDATE workflow_steps SET status = 'FORGED', progress = 13
                   WHERE batch_id = ? AND step_code = 'INGEST'""",
                (batch_id,),
            )
        verification = self.client.get(f"/api/blockchain/verify?batch_id={batch_id}").get_json()
        self.assertFalse(verification["valid"])
        self.assertTrue(any(
            item.get("step_code") == "INGEST" and "指纹" in item.get("error", "")
            for item in verification["errors"]
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
