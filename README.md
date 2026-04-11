# Aegis AI — Local Voice Agent

![Status](https://img.shields.io/badge/status-active%20development-yellow)

A locally-running AI agent with voice I/O, an agentic tool loop, and a sandboxed filesystem. All inference runs on-device via Ollama — no cloud APIs required.

## What it does

- **Voice in / voice out** — records mic input, transcribes with Whisper, responds with XTTS text-to-speech
- **Agentic loop** — the LLM can call tools (read/write files, run shell commands, propose patches, search sessions) up to a configurable iteration limit
- **Session management** — persistent named conversation sessions with search and summarization tools
- **Guardrails** — filesystem access is sandboxed to configurable allowed roots; read/write byte limits and process timeouts are enforced
- **Web UI** — minimal browser interface served by FastAPI

## Stack

| Layer | Technology |
|---|---|
| API server | FastAPI + Uvicorn |
| LLM backend | Ollama (default: `llama3.1:8b`) |
| Text-to-speech | Coqui XTTS v2 |
| Speech-to-text | Whisper (via Ollama or local) |

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure via environment variables (see Configuration below)

# 3. Start Ollama and pull the model
ollama pull llama3.1:8b

# 4. Run
uvicorn app:app --host 127.0.0.1 --port 8000

# 5. Open http://127.0.0.1:8000/ui
```

## Configuration

All settings are environment variables with safe defaults:

| Variable | Default | Description |
|---|---|---|
| `AEGIS_DIRECTOR_DIR` | `C:\AI\director` | Working directory for sessions and static files |
| `AEGIS_XTTS_DIR` | `C:\AI\xtts` | Path to XTTS installation |
| `OLLAMA_CHAT_URL` | `http://127.0.0.1:11434/api/chat` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3.1:8b` | Model to use |
| `AEGIS_ALLOWED_ROOTS` | `DIRECTOR_DIR` | Semicolon-separated paths the agent can access |
| `AEGIS_MODE` | `semi` | Agent mode: `auto` or `semi` (requires approval for destructive tools) |
| `AGENT_MAX_ITERS` | `5` | Max tool-call iterations per turn |
| `SPEAKER_WAV` | `<xtts_dir>/ref.wav` | Reference audio for voice cloning |

## Architecture

```
app.py                  # FastAPI app, agent loop, session endpoints
aegis_core/
  config.py             # All settings via os.environ
  guardrails.py         # Path sandboxing and access control
  tools.py              # Tool definitions and permission levels
  tools_registry.py     # Tool registry and system prompt builder
  scanner.py            # Project version/pattern scanner
  patch_engine.py       # Unified diff patch application
static/
  index.html            # Browser UI
```

## Planned

- [ ] Whisper integration for local STT (currently relying on browser MediaRecorder + server-side decode)
- [ ] Multi-model support (swap LLM backend per session)
- [ ] Docker container for cross-platform setup
- [ ] Tests
