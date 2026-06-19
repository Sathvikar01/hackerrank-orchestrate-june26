"""modal/qwen_client.py

Client for the deployed Qwen2.5-VL Modal service.

Uses Modal's RPC interface (``modal`` Python SDK → ``.remote()``) instead
of an HTTPS endpoint. This avoids the hard dependency on FastAPI at the
Modal layer and lets the deployment stay minimal (torch + transformers).

The retry policy mirrors the brief: on ``QwenSchemaValidationError`` retry
once with the same payload (the model is non-deterministic in principle
even at temperature=0 if any randomness creeps in via batching).

Local disk cache (keyed on image hash + claim text + object type) avoids
re-billing Modal for identical requests.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from modal.qwen_schema import (
    QwenSchemaValidationError,
    parse_qwen_output,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QwenEndpointUnreachable(RuntimeError):
    """Raised when the Modal client cannot be initialised."""


class QwenInvocationError(RuntimeError):
    """Raised when the Modal RPC returns a non-success response."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class QwenClient:
    """Client for the deployed Qwen2.5-VL Modal service via RPC."""

    def __init__(
        self,
        app_name: Optional[str] = None,
        timeout_seconds: int = 240,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.app_name = app_name or os.environ.get(
            "QWEN_APP_NAME", "hackerrank-orchestrate-qwen"
        )
        self.class_name = os.environ.get(
            "QWEN_CLASS_NAME", "QwenVL"
        )
        self.method_name = os.environ.get(
            "QWEN_METHOD_NAME", "predict_damage"
        )
        self.timeout_seconds = int(
            os.environ.get("QWEN_DEPLOYMENT_TIMEOUT_SECONDS", timeout_seconds)
        )
        cache_path = Path(
            os.environ.get("QWEN_CACHE_DIR", cache_dir or ".cache/qwen")
        )
        cache_path.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_path
        self._function = None

    # -- lazy import of modal --------------------------------------------

    def _get_function(self):
        """Resolve the deployed Qwen function lazily.

        Workaround: the local ``modal/`` folder is a regular Python
        package (we ship it for the local scripts). It shadows the
        installed Modal Labs SDK whenever ``import modal`` resolves
        inside this process. We temporarily strip any sys.path entries
        that would resolve to our local package, then re-import the real
        SDK under a different name so sys.modules does not collide.
        """
        if self._function is not None:
            return self._function

        import importlib
        import sys

        # Find the local package on disk (the directory that contains
        # this file's parent).
        local_modal_dir = str(Path(__file__).resolve().parent)

        saved_path = list(sys.path)
        # Remove every sys.path entry that would let ``import modal``
        # resolve to our local package.
        new_path = []
        for entry in saved_path:
            try:
                p = Path(entry).resolve()
                if p == Path(local_modal_dir).resolve():
                    continue  # our local modal/ - skip
                candidate = p / "modal" / "qwen_client.py"
                if candidate.exists():
                    continue  # directory that contains our local modal/
            except Exception:
                pass
            new_path.append(entry)
        sys.path[:] = new_path

        # Also drop any cached "modal" module so the next import resolves
        # against the cleaned sys.path.
        for name in list(sys.modules.keys()):
            if name == "modal" or name.startswith("modal."):
                del sys.modules[name]

        try:
            import modal as modal_sdk
            if not hasattr(modal_sdk, "App"):
                raise QwenEndpointUnreachable(
                    "Resolved 'modal' but it has no attribute 'App'. "
                    "sys.path was: " + repr(sys.path)
                )
            try:
                # Use Cls.from_name for a remote class reference; works
                # even when we do not have the local class definition.
                cls = modal_sdk.Cls.from_name(
                    self.app_name, self.class_name
                )
            except Exception as exc:
                raise QwenEndpointUnreachable(
                    f"Modal class {self.app_name!r}.{self.class_name!r} "
                    f"not found: {exc}"
                ) from exc
            self._function = getattr(cls(), self.method_name)
            return self._function
        finally:
            sys.path[:] = saved_path

    # -- cache ------------------------------------------------------------

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

    # -- RPC --------------------------------------------------------------

    def _read_image_bytes(self, image_paths: List[str]) -> List[bytes]:
        """Read each image path from the local filesystem and return raw bytes."""
        out: List[bytes] = []
        for p in image_paths:
            try:
                with open(p, "rb") as f:
                    out.append(f.read())
            except Exception as exc:
                # Skip unreadable files; the server will treat them as
                # missing.
                continue
        return out

    def predict_damage(
        self,
        image_paths: List[str],
        claim_text: str,
        object_type: str,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Call the Modal function and return a schema-validated dict.

        Reads the images locally, sends them as ``image_bytes`` over the
        Modal RPC, and lets the container save them to ``CACHE_DIR`` for
        inference. This avoids needing a shared filesystem.

        Raises
        ------
        QwenEndpointUnreachable
            If the modal app is not deployed.
        QwenSchemaValidationError
            If both attempts (initial + one retry) produce malformed JSON.
            The brief allows exactly one retry.
        """
        image_bytes = self._read_image_bytes(image_paths)

        key = self._cache_key(image_paths, claim_text, object_type)
        cache_path = self._cache_path(key)

        if use_cache and cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)

        fn = self._get_function()

        last_error: Optional[QwenSchemaValidationError] = None
        for attempt in (1, 2):
            try:
                raw_response = fn.remote(
                    image_paths=None,
                    claim_text=claim_text,
                    object_type=object_type,
                    image_bytes=image_bytes,
                )
            except Exception as exc:
                if attempt == 1:
                    time.sleep(2.0)
                    continue
                raise QwenInvocationError(
                    f"Modal RPC failed on retry: {exc}"
                ) from exc

            try:
                parsed = parse_qwen_output(
                    json.dumps(raw_response), object_type=object_type
                )
            except QwenSchemaValidationError as exc:
                last_error = exc
                if attempt == 1:
                    time.sleep(1.0)
                    continue
                raise

            if use_cache:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)
            return parsed

        raise last_error or QwenSchemaValidationError("Unknown failure")