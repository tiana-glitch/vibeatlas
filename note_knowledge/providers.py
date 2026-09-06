"""Optional Wenxin multimodal provider.

The provider is deliberately opt-in.  Set ``WENXIN_API_URL`` and
``WENXIN_API_KEY`` (or inject the class directly) when a compatible Wenxin
endpoint is available.  The local pipeline never makes a network call unless
this provider is explicitly configured.
"""

from __future__ import annotations

import json
import base64
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .agents import OCRProvider
from .models import ImageInput
from .prompts import OCR_PROMPT, PROMPT_VERSION


class WenxinMultimodalProvider:
    """Small urllib-based adapter for a Wenxin vision chat endpoint."""

    def __init__(self, api_url: str, api_key: str, model: str = "ernie-4.5-turbo-vl") -> None:
        if not api_url or not api_key:
            raise ValueError("api_url and api_key are required")
        self.api_url = api_url
        self.api_key = api_key
        self.model = model

    def extract(self, image: ImageInput, user_note: str = "") -> Mapping[str, Any]:
        prompt = OCR_PROMPT
        if user_note:
            prompt += "\n用户备注：" + user_note
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (image.mime, image.data)}},
                    ],
                }
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        request = Request(
            self.api_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key},
            method="POST",
        )
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("Wenxin OCR request failed: %s" % exc) from exc
        content = payload.get("result") or payload.get("output") or payload.get("choices", [{}])[0].get("message", {}).get("content")
        if isinstance(content, list):
            content = "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
        if isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Wenxin OCR returned non-JSON content") from exc
        if isinstance(content, dict):
            return content
        raise RuntimeError("Wenxin OCR response has no usable result")


class MacOSVisionOCRProvider:
    """Use the built-in macOS Vision framework as a credential-free fallback."""

    _compile_lock = threading.Lock()

    def __init__(self, script_path: Path | None = None, binary_path: Path | None = None) -> None:
        self.script_path = script_path or Path(__file__).with_name("vision_ocr.swift")
        self.binary_path = binary_path or self.script_path.parent.parent / ".note_runtime" / "vision_ocr"
        if not self.is_available():
            raise RuntimeError("macOS Vision OCR requires xcrun, Swift, and vision_ocr.swift")

    def is_available(self) -> bool:
        return shutil.which("xcrun") is not None and self.script_path.is_file()

    def _ensure_binary(self) -> None:
        with self._compile_lock:
            binary_is_current = (
                self.binary_path.is_file()
                and self.binary_path.stat().st_mtime >= self.script_path.stat().st_mtime
            )
            if binary_is_current:
                return
            self.binary_path.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                ["xcrun", "swiftc", str(self.script_path), "-O", "-o", str(self.binary_path)],
                capture_output=True,
                check=False,
                text=True,
                timeout=60,
            )
            if completed.returncode != 0:
                message = completed.stderr.strip() or "unknown compiler error"
                raise RuntimeError("macOS Vision OCR 编译失败: %s" % message)

    def extract(self, image: ImageInput, user_note: str = "") -> Mapping[str, Any]:
        self._ensure_binary()
        try:
            raw = base64.b64decode(image.data, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise RuntimeError("图片数据不是有效的 base64") from exc
        suffix_by_mime = {"image/png": ".png", "image/webp": ".webp", "image/heic": ".heic"}
        suffix = suffix_by_mime.get(image.mime, ".jpg")
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                temporary.write(raw)
                temporary_path = temporary.name
            completed = subprocess.run(
                [str(self.binary_path), temporary_path],
                capture_output=True,
                check=False,
                text=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("macOS Vision OCR 超时") from exc
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)
        if completed.returncode != 0:
            message = completed.stderr.strip() or "unknown error"
            raise RuntimeError("macOS Vision OCR 失败: %s" % message)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("macOS Vision OCR 返回了无效结果") from exc
        if not str(payload.get("原始提取文本", "")).strip():
            raise RuntimeError("图片中没有识别到可用文字")
        return {
            "原始提取文本": payload["原始提取文本"],
            "噪声内容": "无",
            "文本类型": "纸质拍照",
            "低置信度片段": payload.get("低置信度片段", []),
            "图片质量问题": "无",
        }


__all__ = ["MacOSVisionOCRProvider", "WenxinMultimodalProvider"]
