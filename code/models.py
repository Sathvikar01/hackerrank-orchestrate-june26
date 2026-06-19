import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from openai import OpenAI, APIError, RateLimitError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from PIL import Image

from config import (
    NVIDIA_API_KEY,
    MIMO_API_KEY,
    NVIDIA_BASE_URL,
    MIMO_BASE_URL,
    TEMPERATURE,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    CACHE_DIR,
)


def image_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def encode_image_to_base64(path: Path, max_dim: int = 1024, quality: int = 85) -> Tuple[str, str]:
    """Encode image to base64 JPEG. Resize if largest dimension > max_dim."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    import io
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality)
    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return b64, "image/jpeg"


class VLMClient:
    def __init__(self):
        self.nvidia_client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=NVIDIA_API_KEY)
        self.mimo_client = OpenAI(base_url=MIMO_BASE_URL, api_key=MIMO_API_KEY)
        self.cache_dir = Path(CACHE_DIR) / "vlm_calls"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _client_for_model(self, model: str) -> OpenAI:
        if model.startswith("meta/") or model.startswith("nvidia/") or model.startswith("microsoft/") or model.startswith("google/"):
            return self.nvidia_client
        return self.mimo_client

    def _cache_key(self, model: str, prompt: str, image_paths: List[Path], prompt_version: str = "v1") -> str:
        parts = [prompt_version, model, prompt]
        for p in sorted(image_paths):
            parts.append(image_hash(p))
        digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]
        return digest

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    @retry(
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((APIError, RateLimitError, requests.RequestException)),
        reraise=True,
    )
    def _call_api(self, model: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        client = self._client_for_model(model)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=TEMPERATURE,
            max_tokens=2048,
            timeout=REQUEST_TIMEOUT,
        )
        content = response.choices[0].message.content or ""
        return {
            "content": content,
            "model": model,
            "usage": response.usage.model_dump() if response.usage else {},
            "finish_reason": response.choices[0].finish_reason,
        }

    def call(
        self,
        model: str,
        prompt: str,
        image_paths: List[Path],
        prompt_version: str = "v1",
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        cache_key = self._cache_key(model, prompt, image_paths, prompt_version)
        cache_path = self._cache_path(cache_key)
        cache_hit = cache_path.exists()

        if use_cache and cache_hit:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
                cached["cache_hit"] = True
                return cached

        encoded = []
        for p in image_paths:
            b64, mime = encode_image_to_base64(p)
            encoded.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        messages = [
            {"role": "system", "content": "You are a precise visual evidence reviewer. Return only the requested JSON."},
            {"role": "user", "content": [{"type": "text", "text": prompt}] + encoded},
        ]

        start = time.time()
        result = self._call_api(model, messages)
        result["latency"] = time.time() - start
        result["cache_key"] = cache_key
        result["prompt_version"] = prompt_version
        result["cache_hit"] = False

        if use_cache:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

        return result

    def call_text(
        self,
        model: str,
        prompt: str,
        prompt_version: str = "v1",
        use_cache: bool = True,
        json_mode: bool = True,
    ) -> Dict[str, Any]:
        """Text-only call for judge/verifier agents."""
        cache_key = hashlib.sha256(f"{prompt_version}:{model}:{prompt}".encode("utf-8")).hexdigest()[:32]
        cache_path = self.cache_dir / f"text_{cache_key}.json"
        cache_hit = cache_path.exists()

        if use_cache and cache_hit:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
                cached["cache_hit"] = True
                return cached

        client = self._client_for_model(model)
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a careful reasoning assistant. Return only the requested JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": 2048,
            "timeout": REQUEST_TIMEOUT,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        start = time.time()
        response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        result = {
            "content": content,
            "model": model,
            "usage": response.usage.model_dump() if response.usage else {},
            "finish_reason": response.choices[0].finish_reason,
            "latency": time.time() - start,
            "cache_key": cache_key,
            "prompt_version": prompt_version,
            "cache_hit": False,
        }

        if use_cache:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

        return result


def parse_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from VLM output, handling markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            # Try to find first { and last }
            start = text.index("{")
            end = text.rindex("}")
            return json.loads(text[start : end + 1])
        except Exception:
            return None
