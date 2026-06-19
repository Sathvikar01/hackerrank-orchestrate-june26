"""Local ``modal/`` package.

This package implements the Qwen2.5-VL deployment for the evidence review
pipeline.

Why ``__init__.py`` exists
--------------------------
The folder is named ``modal/`` to match the brief ("Create: modal/qwen_service.py"),
but the installed Modal Labs CLI also ships a top-level Python package called
``modal``. Without an ``__init__.py`` here, Python's namespace-package rules
prefer the installed SDK over this folder, and ``from modal.qwen_client
import ...`` silently imports the SDK's ``qwen_client`` attribute (which does
not exist).

By giving this folder an ``__init__.py`` it becomes a regular package, and
when ``modal/`` is on ``sys.path`` *before* site-packages, Python picks it
over the SDK. Scripts in here manage their own ``sys.path`` ordering so
local imports win, while ``qwen_service.py`` continues to use the SDK when
executed under ``modal deploy``.
"""

__all__ = [
    "qwen_client",
    "qwen_schema",
    "ab_test",
    "multicrop",
    "verifier",
]