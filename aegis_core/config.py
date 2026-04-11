import os
import re

# ----------------------------
# Branding
# ----------------------------
APP_NAME = os.environ.get("AEGIS_APP_NAME", "Aegis AI")
APP_VERSION = os.environ.get("AEGIS_APP_VERSION", "v1.1.7")
APP_TITLE = f"{APP_NAME} {APP_VERSION}"

# ----------------------------
# Config / Paths
# ----------------------------
DIRECTOR_DIR = os.environ.get("AEGIS_DIRECTOR_DIR", r"C:\\AI\\director")
XTTS_DIR = os.environ.get("AEGIS_XTTS_DIR", r"C:\\AI\\xtts")

TMP_DIR = os.environ.get("AEGIS_TMP_DIR", os.path.join(DIRECTOR_DIR, "tmp"))
SESSIONS_DIR = os.environ.get("AEGIS_SESSIONS_DIR", os.path.join(DIRECTOR_DIR, "sessions"))
STATIC_DIR = os.environ.get("AEGIS_STATIC_DIR", os.path.join(DIRECTOR_DIR, "static"))

REF_WAV = os.environ.get("SPEAKER_WAV") or os.path.join(XTTS_DIR, "ref.wav")

OLLAMA_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

# Voice
VOICE_SR = int(os.environ.get("VOICE_SR", "24000"))
XTTS_GPU = os.environ.get("XTTS_GPU", "0").strip().lower() in ("1", "true", "yes")
XTTS_PREWARM = os.environ.get("XTTS_PREWARM", "0").strip().lower() in ("1", "true", "yes")

# UI and cleanup
UI_MAX_BUBBLES = int(os.environ.get("UI_MAX_BUBBLES", "300"))
TMP_MAX_FILES = int(os.environ.get("TMP_MAX_FILES", "250"))
TMP_MAX_AGE_SEC = int(os.environ.get("TMP_MAX_AGE_SEC", str(6 * 3600)))

AUTO_VOICE_DEFAULT_ON = os.environ.get("AUTO_VOICE_DEFAULT_ON", "1").strip().lower() in ("1", "true", "yes")

# Agent
AEGIS_MODE = os.environ.get("AEGIS_MODE", "semi").strip().lower()
AGENT_MAX_ITERS = int(os.environ.get("AGENT_MAX_ITERS", "5"))

# Tool safety
_ALLOWED_ROOTS_RAW = os.environ.get("AEGIS_ALLOWED_ROOTS", DIRECTOR_DIR)
ALLOWED_ROOTS = [p.strip() for p in re.split(r"[;|]", _ALLOWED_ROOTS_RAW) if p.strip()]

MAX_READ_BYTES = int(os.environ.get("AEGIS_MAX_READ_BYTES", str(200_000)))
MAX_WRITE_BYTES = int(os.environ.get("AEGIS_MAX_WRITE_BYTES", str(400_000)))
PROCESS_TIMEOUT_SEC = int(os.environ.get("AEGIS_PROCESS_TIMEOUT_SEC", "45"))
MAX_PROCESS_OUTPUT_CHARS = int(os.environ.get("AEGIS_MAX_PROCESS_OUTPUT_CHARS", "8000"))

# Patch constraints
MAX_PATCH_CHARS = int(os.environ.get("AEGIS_MAX_PATCH_CHARS", "250000"))
MAX_PATCH_TARGET_BYTES = int(os.environ.get("AEGIS_MAX_PATCH_TARGET_BYTES", "2000000"))
PATCH_STRIP_PREFIX = int(os.environ.get("AEGIS_PATCH_STRIP_PREFIX", "0"))

# Deterministic tools
DIRECT_RETURN_TOOLS = {"list_dir", "read_file"}
