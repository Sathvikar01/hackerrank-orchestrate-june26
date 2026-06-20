import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = PROJECT_ROOT / "dataset"

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
MIMO_API_KEY = os.getenv("MIMO_API_KEY")

NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")

PRIMARY_VLM_MODEL = os.getenv("PRIMARY_VLM_MODEL", "mimo-v2.5")
ENSEMBLE_VLM_MODEL = os.getenv("ENSEMBLE_VLM_MODEL", "mimo-v2.5-pro")
FAST_VLM_MODEL = os.getenv("FAST_VLM_MODEL", "nvidia/nemotron-nano-12b-v2-vl")
TEXT_JUDGE_MODEL = os.getenv("TEXT_JUDGE_MODEL", "mimo-v2-flash")

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))
CACHE_DIR = Path(os.getenv("CACHE_DIR", str(PROJECT_ROOT / ".cache"))).resolve()

if not NVIDIA_API_KEY:
    raise RuntimeError("NVIDIA_API_KEY is not set. Add it to the .env file.")
if not MIMO_API_KEY:
    raise RuntimeError("MIMO_API_KEY is not set. Add it to the .env file.")
