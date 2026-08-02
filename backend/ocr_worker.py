"""Isolated PaddleOCR worker used by the Python 3.11 OCR environment."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


RESULT_PREFIX = "MAI_OCR_RESULT="


def _build_engine(language: str):
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    from paddleocr import PaddleOCR

    try:
        return PaddleOCR(
            lang=language,
            enable_mkldnn=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except TypeError:
        return PaddleOCR(lang=language, use_angle_cls=True, enable_mkldnn=False)


def _extract_lines(value) -> tuple[list[str], list[float]]:
    texts: list[str] = []
    scores: list[float] = []
    visited: set[int] = set()

    def add(text, score=None):
        clean = str(text or "").strip()
        if clean and clean not in texts:
            texts.append(clean)
            try:
                scores.append(float(score))
            except (TypeError, ValueError):
                pass

    def walk(item):
        if item is None or isinstance(item, (str, bytes, int, float, bool)):
            return
        identity = id(item)
        if identity in visited:
            return
        visited.add(identity)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--lang", default="ch")
    args = parser.parse_args()
    try:
        engine = _build_engine(args.lang)
        if args.check:
            from PIL import Image, ImageDraw

            with tempfile.TemporaryDirectory(prefix="mai-ocr-check-") as directory:
                check_path = Path(directory) / "check.png"
                image = Image.new("RGB", (360, 120), "white")
                ImageDraw.Draw(image).text((24, 42), "M-AI OCR 123", fill="black")
                image.save(check_path)
                list(engine.predict(str(check_path))) if hasattr(engine, "predict") else engine.ocr(str(check_path), cls=True)
            print(RESULT_PREFIX + json.dumps({"ready": True}, ensure_ascii=False))
            return 0
        image_path = Path(args.image or "")
        if not image_path.is_file():
            raise ValueError("image file is required")
        result = list(engine.predict(str(image_path))) if hasattr(engine, "predict") else engine.ocr(str(image_path), cls=True)
        texts, scores = _extract_lines(result)
        confidence = round(sum(scores) / len(scores), 4) if scores else None
        print(RESULT_PREFIX + json.dumps({"ready": True, "text": "\n".join(texts), "confidence": confidence}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(RESULT_PREFIX + json.dumps({"ready": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
