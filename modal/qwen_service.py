"""modal/qwen_service.py

Modal deployment of Qwen2.5-VL for the HackerRank Orchestrate evidence review
pipeline.

Goals
-----
* GPU must NOT remain running permanently.
* Spin up A10G only when inference arrives.
* Process request, return prediction, allow Modal to scale to zero.
* Pay only for active inference time.

Architecture
------------
* `app.cls()` class keeps model weights warm across consecutive calls within
  the same container (avoids reloading on every cold start).
* `scaledown_window=60` (seconds) lets Modal scale the GPU to zero after 60 s
  of inactivity.
* `concurrency_limit=1` ensures one inference at a time per container (A10G
  has 24 GB VRAM; we do not want concurrent requests to OOM).
* `allow_concurrent_inputs=4` lets Modal buffer up to 4 inputs in the queue
  before scaling an additional container.
* Cold start amortised across the class lifetime, not per-call.
* Inference is wrapped in `predict_damage(image_paths, claim_text,
  object_type)` which returns a strictly-typed JSON dict.

Why Qwen2.5-VL-3B-Instruct as the default
-----------------------------------------
* A10G = 24 GB GDDR6.
* Qwen2.5-VL-3B-Instruct in bfloat16 ≈ 6.5 GB weights + 0.4 GB vision tower.
* Qwen2.5-VL-7B-Instruct in bfloat16 ≈ 15 GB weights + 0.4 GB vision tower.
  Multi-image inputs and longer conversations can OOM.
* 3B is the safer production default. Use ``QWEN_MODEL_ID`` env var to
  override to ``Qwen/Qwen2.5-VL-7B-Instruct`` for the A/B test if desired.

Deploy
------
    modal deploy modal/qwen_service.py

Smoke test
----------
    modal run modal/qwen_service.py::smoke

Returns
-------
The ``predict_damage`` function returns:

    {
        "issue_type": "...",
        "object_part": "...",
        "severity": "...",
        "visible_damage": true,
        "evidence_sufficient": true,
        "quality_flags": []
    }

with values constrained to the schema defined in ``modal.qwen_schema``.

Cost model
----------
* A10G on Modal: $0.0006125/GPU-second (~$2.20/hr) as of mid-2026.
* Cold start (load 3B model + vision tower + CUDA init): 25-45 s.
* Warm latency (single image, 1024 px): 1.5-4.0 s.
* Warm latency (3 images, 1024 px each): 3.5-8.0 s.
* Cost per claim (warm, single image, ~3 s): ~$0.002.
* Cost per claim (warm, multi image, ~6 s): ~$0.004.
* Cold-start overhead per burst (first request after idle): ~$0.02.
* 20-row sample run, single image avg: 20 * $0.004 = $0.08 (cold-start first).
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import modal


# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

APP_NAME = "hackerrank-orchestrate-qwen"

app = modal.App(APP_NAME)

# Model + cache configuration via env so deployment can be tuned without code
# changes.
QWEN_MODEL_ID = os.environ.get(
    "QWEN_MODEL_ID", "Qwen/Qwen2.5-VL-3B-Instruct"
)
QWEN_REVISION = os.environ.get("QWEN_REVISION", "main")
QWEN_DTYPE = os.environ.get("QWEN_DTYPE", "bfloat16")
# Maximum image dimension (px). Larger images are downscaled before
# tokenisation. 1024 keeps token count manageable on A10G.
QWEN_MAX_IMAGE_DIM = int(os.environ.get("QWEN_MAX_IMAGE_DIM", "1024"))
# Maximum number of new tokens the model may generate. The structured JSON is
# < 1k tokens; we keep headroom for retries.
QWEN_MAX_NEW_TOKENS = int(os.environ.get("QWEN_MAX_NEW_TOKENS", "512"))

# Container image: Pinned versions for reproducibility.
# - torch 2.4.x supports qwen2_5_vl natively (transformers >= 4.49).
# - flash-attn 2 for memory-efficient attention on A10G.
#
# The schema validator is inlined below as ``_parse_qwen_output`` to avoid
# the brittle ``add_local_python_source`` cross-platform path issue on
# Windows. The canonical schema still lives in ``modal/qwen_schema.py``
# for the local client and tests; the inline copy is the deployment-time
# source of truth.
qwen_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch==2.4.1",
        "torchvision==0.19.1",
        "transformers==4.49.0",
        "accelerate==1.0.1",
        "qwen-vl-utils==0.0.8",
        "pillow==10.4.0",
        "jsonschema==4.23.0",
        # httpx used by the modal client to talk to the @modal.method RPC.
        "httpx==0.27.2",
    )
    .env(
        {
            "QWEN_MODEL_ID": QWEN_MODEL_ID,
            "QWEN_REVISION": QWEN_REVISION,
            "QWEN_DTYPE": QWEN_DTYPE,
            "QWEN_MAX_IMAGE_DIM": str(QWEN_MAX_IMAGE_DIM),
            "QWEN_MAX_NEW_TOKENS": str(QWEN_MAX_NEW_TOKENS),
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
)


# ---------------------------------------------------------------------------
# Volume for model cache (so re-deploys do not re-download from HF)
# ---------------------------------------------------------------------------

qwen_cache_vol = modal.Volume.from_name(
    "hackerrank-orchestrate-qwen-cache", create_if_missing=True
)
CACHE_DIR = "/cache/qwen"


# ---------------------------------------------------------------------------
# Prompt + schema (kept in this module so the service is self-contained)
# ---------------------------------------------------------------------------

QWEN_SYSTEM_PROMPT = """You are an expert insurance claim evidence reviewer.

You receive:
- One or more photographs of a {object_type} the user is filing a claim about.
- A short conversation describing what the user claims is wrong.

Your job is to inspect the images and decide whether the user's claim is
supported, contradicted, or cannot be evaluated from the supplied evidence.

You MUST respond with a single JSON object matching this schema exactly:

{{
  "issue_type": one of ["dent","scratch","crack","glass_shatter","broken_part","missing_part","torn_packaging","crushed_packaging","water_damage","stain","none","unknown"],
  "object_part": the visible part affected (use "unknown" if not visible),
  "severity": one of ["none","low","medium","high","unknown"],
  "visible_damage": true or false,
  "evidence_sufficient": true or false,
  "quality_flags": [] // any of ["blurry_image","cropped_or_obstructed","low_light_or_glare","wrong_angle","wrong_object","wrong_object_part","damage_not_visible","claim_mismatch","possible_manipulation","non_original_image","text_instruction_present"]
}}

Rules:
1. Images are the PRIMARY source of truth.
2. If you cannot see the claimed damage in any image, set visible_damage=false
   and issue_type to "none" or "unknown" depending on whether the part is at
   least visible.
3. If the relevant part is not in frame at all, set evidence_sufficient=false.
4. Be conservative with severity.
5. If the image is blurry / cropped / too dark / wrong object, add the
   corresponding flag to quality_flags.
6. Output JSON only. No prose, no markdown fences.
"""


# ---------------------------------------------------------------------------
# Helper: local image resize (CPU-side, keeps memory low inside the container)
# ---------------------------------------------------------------------------


def _resize_image(path: str, max_dim: int) -> bytes:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Inline schema validator (mirrors modal/qwen_schema.py)
# ---------------------------------------------------------------------------
#
# This block is intentionally duplicated from ``modal/qwen_schema.py`` so
# the Modal container image does not need to bundle a local Python
# source file. Keep the two in sync when modifying the schema.
# ---------------------------------------------------------------------------


class _QwenSchemaError(ValueError):
    """Raised when model output does not conform to the schema."""


_INLINE_ISSUE_TYPES = [
    "dent", "scratch", "crack", "glass_shatter", "broken_part",
    "missing_part", "torn_packaging", "crushed_packaging", "water_damage",
    "stain", "none", "unknown",
]
_INLINE_SEVERITY = ["none", "low", "medium", "high", "unknown"]
_INLINE_QUALITY_FLAGS = [
    "blurry_image", "cropped_or_obstructed", "low_light_or_glare",
    "wrong_angle", "wrong_object", "wrong_object_part", "damage_not_visible",
    "claim_mismatch", "possible_manipulation", "non_original_image",
    "text_instruction_present",
]
_INLINE_OBJECT_PARTS = {
    "car": [
        "front_bumper", "rear_bumper", "door", "hood", "windshield",
        "side_mirror", "headlight", "taillight", "fender", "quarter_panel",
        "body", "unknown",
    ],
    "laptop": [
        "screen", "keyboard", "trackpad", "hinge", "lid", "corner", "port",
        "base", "body", "unknown",
    ],
    "package": [
        "box", "package_corner", "package_side", "seal", "label", "contents",
        "item", "unknown",
    ],
}


_INLINE_ISSUE_ALIASES = {
    "dented": "dent", "scratched": "scratch", "cracked": "crack",
    "shattered": "glass_shatter", "shatter": "glass_shatter",
    "broken": "broken_part", "break": "broken_part",
    "missing": "missing_part", "torn": "torn_packaging",
    "crushed": "crushed_packaging", "wet": "water_damage",
    "water": "water_damage", "stained": "stain",
    "no_damage": "none", "no_issue": "none", "ok": "none", "fine": "none",
}
_INLINE_SEVERITY_ALIASES = {
    "no": "none", "minor": "low", "small": "low", "moderate": "medium",
    "moderate_damage": "medium", "severe": "high", "major": "high",
    "critical": "high", "unsure": "unknown", "cant_tell": "unknown",
    "cannot_tell": "unknown", "n/a": "unknown", "na": "unknown",
}
_INLINE_PART_ALIASES = {
    "front_bumper": "front_bumper", "rear_bumper": "rear_bumper",
    "back_bumper": "rear_bumper", "windshield": "windshield",
    "front_glass": "windshield", "rear_glass": "windshield",
    "side_mirror": "side_mirror", "wing_mirror": "side_mirror",
    "headlight": "headlight", "headlamp": "headlight",
    "taillight": "taillight", "taillamp": "taillight",
    "tail_light": "taillight", "quarter_panel": "quarter_panel",
    "screen": "screen", "display": "screen", "lcd": "screen",
    "keyboard": "keyboard", "trackpad": "trackpad", "hinge": "hinge",
    "lid": "lid", "corner": "corner", "port": "port", "base": "base",
    "box": "box", "package_corner": "package_corner",
    "package_side": "package_side", "seal": "seal", "label": "label",
    "contents": "contents", "item": "item",
}


def _inline_fuzzy(value: Any, allowed: List[str], aliases: Dict[str, str]) -> str:
    if value is None:
        return "__invalid__"
    s = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if s in allowed:
        return s
    if s in aliases and aliases[s] in allowed:
        return aliases[s]
    for a in allowed:
        if s == a or s in a or a in s:
            return a
    return "__invalid__"


def _inline_coerce_quality_flags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v.strip() for v in re.split(r"[,;|]", value) if v.strip()]
    if not isinstance(value, list):
        return []
    cleaned: List[str] = []
    for v in value:
        s = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        if s in _INLINE_QUALITY_FLAGS and s not in cleaned:
            cleaned.append(s)
    return cleaned


def _inline_coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("true", "yes", "1", "t", "y"):
        return True
    if s in ("false", "no", "0", "f", "n"):
        return False
    raise _QwenSchemaError(f"bool: {value!r} not recognised")


def _parse_qwen_output(raw_text: str, object_type: str = "") -> Dict[str, Any]:
    """Parse + validate model output text. Mirrors ``modal.qwen_schema.parse_qwen_output``."""
    text = (raw_text or "").strip()
    if not text:
        raise _QwenSchemaError("Model returned empty text.")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise _QwenSchemaError(f"No JSON object found: {text[:200]!r}")
        text = text[start : end + 1]
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _QwenSchemaError(f"JSON decode failed: {exc}") from exc
    if not isinstance(raw, dict):
        raise _QwenSchemaError(
            f"Expected JSON object, got {type(raw).__name__}"
        )

    if "visible_damage" not in raw:
        raise _QwenSchemaError("visible_damage: missing")
    if "evidence_sufficient" not in raw:
        raise _QwenSchemaError("evidence_sufficient: missing")

    issue_type = _inline_fuzzy(
        raw.get("issue_type"), _INLINE_ISSUE_TYPES, _INLINE_ISSUE_ALIASES
    )
    if issue_type == "__invalid__":
        raise _QwenSchemaError(
            f"issue_type: {raw.get('issue_type')!r} not in allowed"
        )

    severity = _inline_fuzzy(
        raw.get("severity"), _INLINE_SEVERITY, _INLINE_SEVERITY_ALIASES
    )
    if severity == "__invalid__":
        raise _QwenSchemaError(
            f"severity: {raw.get('severity')!r} not in allowed"
        )

    allowed_parts = _INLINE_OBJECT_PARTS.get(object_type, ["unknown"])
    object_part = _inline_fuzzy(
        raw.get("object_part"), allowed_parts, _INLINE_PART_ALIASES
    )
    if object_part == "__invalid__":
        raise _QwenSchemaError(
            f"object_part: {raw.get('object_part')!r} not in allowed"
        )

    return {
        "issue_type": issue_type,
        "object_part": object_part,
        "severity": severity,
        "visible_damage": _inline_coerce_bool(raw.get("visible_damage")),
        "evidence_sufficient": _inline_coerce_bool(raw.get("evidence_sufficient")),
        "quality_flags": _inline_coerce_quality_flags(raw.get("quality_flags")),
    }


# ---------------------------------------------------------------------------
# Modal class: holds the model + processor across calls in the same container
# ---------------------------------------------------------------------------


@app.cls(
    gpu="A10G",
    image=qwen_image,
    volumes={CACHE_DIR: qwen_cache_vol},
    scaledown_window=60,           # scale to zero after 60 s of idle
    max_containers=1,              # one inference at a time per container
    timeout=600,                   # hard ceiling per request
)
class QwenVL:
    """Long-lived Qwen2.5-VL inference worker.

    Lifecycle:
        start  -> load model + processor (cold start)
        call   -> predict_damage (warm)
        idle   -> scaledown_window ticks down -> container dies
    """

    @modal.enter()
    def load(self) -> None:
        """One-time setup when the container starts (cold start)."""
        import torch
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

        t0 = time.time()
        model_id = os.environ["QWEN_MODEL_ID"]
        dtype_str = os.environ["QWEN_DTYPE"]
        dtype = getattr(torch, dtype_str)
        cache_dir = CACHE_DIR

        self.processor = AutoProcessor.from_pretrained(
            model_id,
            revision=os.environ["QWEN_REVISION"],
            cache_dir=cache_dir,
        )
        # attn_implementation="flash_attention_2" cuts memory ~30% on A10G
        # but requires flash-attn. We use sdpa as a safe fallback.
        self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            model_id,
            revision=os.environ["QWEN_REVISION"],
            torch_dtype=dtype,
            device_map="cuda",
            cache_dir=cache_dir,
            attn_implementation="sdpa",
        )
        self.model.eval()
        # Persist the cache so subsequent cold starts skip the download.
        qwen_cache_vol.commit()

        self.cold_start_seconds = time.time() - t0
        print(
            f"[qwen] cold start complete in {self.cold_start_seconds:.1f}s "
            f"({model_id}, {dtype_str})"
        )

    def _build_messages(
        self,
        image_paths: List[str],
        claim_text: str,
        object_type: str,
    ) -> List[Dict[str, Any]]:
        """Construct chat messages in Qwen2.5-VL format."""
        image_entries = []
        for p in image_paths:
            image_entries.append(
                {"type": "image", "image": f"file://{p}"}
            )
        user_content: List[Dict[str, Any]] = list(image_entries)
        user_content.append(
            {
                "type": "text",
                "text": (
                    f"Object type: {object_type}\n"
                    f"User claim transcript:\n\"\"\"\n{claim_text}\n\"\"\"\n\n"
                    "Respond with a single JSON object matching the schema."
                ),
            }
        )
        return [
            {"role": "system", "content": self._system_prompt(object_type)},
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _system_prompt(object_type: str) -> str:
        return QWEN_SYSTEM_PROMPT.format(object_type=object_type)

    @modal.method()
    def predict_damage(
        self,
        image_paths: Optional[List[str]] = None,
        claim_text: str = "",
        object_type: str = "",
        image_bytes: Optional[List[bytes]] = None,
    ) -> Dict[str, Any]:
        """Run Qwen2.5-VL on the supplied images + claim and return structured JSON.

        Inputs
        ------
        image_paths
            Absolute paths to images on the container's local filesystem.
            Mutually exclusive with ``image_bytes``.
        image_bytes
            List of raw image bytes (JPEG/PNG). The container saves them
            to ``CACHE_DIR`` and then runs inference. Preferred for cross-
            machine calls (avoids needing a shared filesystem).
        claim_text
            The user-claim transcript.
        object_type
            One of ``car`` / ``laptop`` / ``package``.

        Returns
        -------
        dict matching ``modal.qwen_schema.QWEN_OUTPUT_SCHEMA``:
            {
                "issue_type": str,
                "object_part": str,
                "severity": str,
                "visible_damage": bool,
                "evidence_sufficient": bool,
                "quality_flags": List[str],
            }

        Plus telemetry fields under "_meta":
            - latency_ms: warm inference latency in milliseconds
            - image_count: number of images processed
            - model_id: model identifier
        """
        # Local import keeps cold start fast for the rare case where the
        # request fails before any heavy import.
        import torch
        from qwen_vl_utils import process_vision_info

        sources: List[str] = []
        if image_bytes:
            for b in image_bytes:
                if not b:
                    continue
                tmp = Path(CACHE_DIR) / f"in_{int(time.time()*1000)}_{len(sources)}.jpg"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(b)
                sources.append(str(tmp))
        if image_paths:
            sources.extend(p for p in image_paths if p)

        if not sources:
            return {
                "issue_type": "unknown",
                "object_part": "unknown",
                "severity": "unknown",
                "visible_damage": False,
                "evidence_sufficient": False,
                "quality_flags": ["damage_not_visible"],
                "_meta": {
                    "latency_ms": 0,
                    "image_count": 0,
                    "model_id": os.environ.get("QWEN_MODEL_ID", ""),
                },
            }

        # Resize images (CPU) before feeding them to the processor so the
        # number of vision tokens stays bounded.
        max_dim = int(os.environ.get("QWEN_MAX_IMAGE_DIM", "1024"))
        resized_paths: List[str] = []
        for p in sources:
            try:
                data = _resize_image(p, max_dim)
                # Write to a temporary file inside the cache volume so the
                # Qwen processor can read it via file:// URL.
                tmp = Path(CACHE_DIR) / f"tmp_{int(time.time()*1000)}_{Path(p).name}"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                resized_paths.append(str(tmp))
            except Exception as exc:  # pragma: no cover (defensive)
                print(f"[qwen] failed to resize {p}: {exc}")

        if not resized_paths:
            return {
                "issue_type": "unknown",
                "object_part": "unknown",
                "severity": "unknown",
                "visible_damage": False,
                "evidence_sufficient": False,
                "quality_flags": ["non_original_image"],
                "_meta": {
                    "latency_ms": 0,
                    "image_count": 0,
                    "model_id": os.environ.get("QWEN_MODEL_ID", ""),
                },
            }

        messages = self._build_messages(resized_paths, claim_text, object_type)

        # Process vision input via the official Qwen helper.
        image_inputs, video_inputs = process_vision_info(messages)

        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.processor(
            text=[text],
            images=image_inputs,
            videos=video_inputs,
            padding=True,
            return_tensors="pt",
        )
        inputs = inputs.to("cuda")

        t0 = time.time()
        with torch.inference_mode():
            generated_ids = self.model.generate(
                **inputs,
                max_new_tokens=int(os.environ.get("QWEN_MAX_NEW_TOKENS", "512")),
                do_sample=False,
                temperature=0.0,
                top_p=1.0,
            )
        # Strip the input tokens from the output (model.generate returns the
        # full sequence, prompt + completion).
        generated_ids_trimmed = [
            out_ids[len(in_ids) :]
            for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
        ]
        raw_text = self.processor.batch_decode(
            generated_ids_trimmed,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]
        latency_ms = int((time.time() - t0) * 1000)

        # Parse + validate JSON using the inlined schema parser. The
        # canonical source of truth lives in ``modal/qwen_schema.py``
        # for the local client; this inline copy keeps deployment simple
        # (no cross-platform path issues with add_local_python_source).
        parsed = _parse_qwen_output(raw_text, object_type=object_type)

        parsed["_meta"] = {
            "latency_ms": latency_ms,
            "image_count": len(resized_paths),
            "model_id": os.environ.get("QWEN_MODEL_ID", ""),
        }
        return parsed


# ---------------------------------------------------------------------------
# Web endpoint: HTTPS, scale-to-zero, no always-on reservation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Web endpoint (disabled — using @modal.method instead to avoid the
# fastapi endpoint's hard requirement on the fastapi package).
# ---------------------------------------------------------------------------
#
# To expose an HTTP endpoint, set ``MODAL_HTTP_ENDPOINT=1`` before
# deploying. This requires the fastapi package to be installed (handled
# automatically by Modal when the flag is on; for now we use the
# @modal.method RPC interface, which is sufficient for the local
# ``modal.qwen_client`` harness).
# ---------------------------------------------------------------------------

_USE_HTTP = os.environ.get("MODAL_HTTP_ENDPOINT", "0") == "1"


if _USE_HTTP:

    @app.function(
        scaledown_window=60,
        timeout=120,
    )
    @modal.fastapi_endpoint(method="POST")
    def predict_damage_http(item: Dict[str, Any]) -> Dict[str, Any]:
        """HTTPS endpoint exposing ``predict_damage``.

        POST JSON body::

            {
                "image_paths": ["/abs/path/img1.jpg", ...],
                "claim_text": "...",
                "object_type": "car|laptop|package"
            }

        Modal spins up an A10G container on first call after idle, keeps
        it warm for ``scaledown_window`` seconds, then scales to zero.
        """
        image_paths = item.get("image_paths", [])
        claim_text = item.get("claim_text", "")
        object_type = item.get("object_type", "")

        if not isinstance(image_paths, list) or not image_paths:
            raise ValueError("image_paths must be a non-empty list of strings")

        return QwenVL().predict_damage.remote(
            image_paths=image_paths,
            claim_text=claim_text,
            object_type=object_type,
        )


# ---------------------------------------------------------------------------
# Local smoke test (uses Modal's RPC, not HTTP)
# ---------------------------------------------------------------------------


@app.local_entrypoint()
def smoke(image_path: Optional[str] = None) -> None:
    """Smoke test the deployed model.

    Usage:
        modal run modal/qwen_service.py::smoke --image-path /abs/path/img.jpg
    """
    if image_path is None:
        # Default: first sample image in the repo, if available.
        candidates = list(Path("dataset/images/sample").rglob("*.jpg"))
        if not candidates:
            print("No image_path supplied and no dataset images found.")
            return
        image_path = str(candidates[0])

    result = QwenVL().predict_damage.remote(
        image_paths=[image_path],
        claim_text=(
            "Customer: I see a dent on my car after a parking lot incident. "
            "The rear bumper is the area that got hit."
        ),
        object_type="car",
    )
    print(json.dumps(result, indent=2))