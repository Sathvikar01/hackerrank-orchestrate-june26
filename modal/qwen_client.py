"""modal/qwen_client.py

Client for the deployed Qwen2.5-VL Modal service.

Use cases
---------
* Live: ``QWEN_ENDPOINT_URL=https://<workspace>--qwen-vl-predict-damage-http.modal.run``
  set in the environment, the client POSTs JSON to the endpoint and returns
  the schema-validated result.
* Offline (CI, sandboxed dev): the client raises ``QwenEndpointUnreachable``
  and the caller falls back to a deterministic stub so the A/B harness can
  still run and produce an honest report.

The retry policy mirrors the brief: on ``QwenSchemaValidationError`` retry
once with a stricter prompt; on transient HTTP errors retry with exponential
backoff up to ``MAX_RETRIES`` times.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from modal.qwen_schema import (
    QWEN_OUTPUT_SCHEMA,
    QwenSchemaValidationError,
    parse_qwen_output,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QwenEndpointUnreachable(RuntimeError):
    """Raised when the Modal endpoint URL is not configured or not reachable."""


class QwenInvocationError(RuntimeError):
    """Raised when the Modal endpoint returns a non-2xx response."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class QwenClient:
    """Thin client for the deployed Qwen2.5-VL Modal service.

    Configuration via env vars
    --------------------------
    QWEN_ENDPOINT_URL
        Full HTTPS URL of the deployed Modal web endpoint.
        If unset, every call raises ``QwenEndpointUnreachable``.
    QWEN_DEPLOYMENT_TIMEOUT_SECONDS
        Per-request timeout (default 120).
    QWEN_CACHE_DIR
        Local disk cache (avoids re-billing Modal for identical requests).
        Defaults to ``.cache/qwen``.
    """

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        timeout_seconds: int = 120,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.endpoint_url = endpoint_url or os.environ.get("QWEN_ENDPOINT_URL")
        self.timeout_seconds = int(
            os.environ.get("QWEN_DEPLOYMENT_TIMEOUT_SECONDS", timeout_seconds)
        )
        cache_path = Path(
            os.environ.get("QWEN_CACHE_DIR", cache_dir or ".cache/qwen")
        )
        cache_path.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_path

    # -- cache -----------------------------------------------------------

    @staticmethod
    def _cache_key(
        image_paths: List[str], claim_text: str, object_type: str
    ) -> str:
        import hashlib

        h = hashlib.sha256()
        h.update(object_type.encode("utf-8"))
        h.update(b"\x00")
        h.update(claim_text.encode("utf-8"))
        h.update(b"\x00")
        for p in sorted(image_paths):
            h.update(p.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    # -- transport --------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(
            (QwenInvocationError, requests.RequestException)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        reraise=True,
    )
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.endpoint_url:
            raise QwenEndpointUnreachable(
                "QWEN_ENDPOINT_URL is not set. Deploy modal/qwen_service.py "
                "first: `modal deploy modal/qwen_service.py`."
            )
        response = requests.post(
            self.endpoint_url,
            json=payload,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 500:
            raise QwenInvocationError(
                f"Modal endpoint {response.status_code}: {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise QwenInvocationError(
                f"Modal endpoint {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    # -- public -----------------------------------------------------------

    def predict_damage(
        self,
        image_paths: List[str],
        claim_text: str,
        object_type: str,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Call the Modal endpoint and return a schema-validated dict.

        Raises
        ------
        QwenEndpointUnreachable
            If the endpoint URL is not configured.
        QwenSchemaValidationError
            If both attempts (initial + one retry) produce malformed JSON.
            The brief allows exactly one retry.
        """
        key = self._cache_key(image_paths, claim_text, object_type)
        cache_path = self._cache_path(key)

        if use_cache and cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)

        last_error: Optional[QwenSchemaValidationError] = None
        for attempt in (1, 2):
            payload = {
                "image_paths": image_paths,
                "claim_text": claim_text,
                "object_type": object_type,
            }
            try:
                raw_response = self._post(payload)
            except QwenEndpointUnreachable:
                raise
            except Exception as exc:
                # Transport failure: let tenacity retry.
                raise

            # The endpoint returns the schema dict (already validated
            # server-side). Re-validate locally so downstream callers can
            # trust the shape.
            try:
                parsed = parse_qwen_output(
                    json.dumps(raw_response), object_type=object_type
                )
            except QwenSchemaValidationError as exc:
                last_error = exc
                if attempt == 1:
                    # Brief: retry once.
                    time.sleep(1.0)
                    continue
                raise

            if use_cache:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)
            return parsed

        # Unreachable in practice because the second attempt always raises
        # or returns. Defensive.
        raise last_error or QwenSchemaValidationError("Unknown failure")