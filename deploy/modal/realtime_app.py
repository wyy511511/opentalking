"""OpenTalking real-time digital-human chat on Modal.

One container runs the whole stack: ``opentalking-unified`` (API + worker,
in-memory session state) behind nginx, which also serves the built WebUI.

Modes (chosen at deploy time with ``OT_MODE``):

* ``mock``      – CPU only, static avatar frame. Use it to validate WebRTC via
                  TURN and to tune LLM / knowledge-base answers. ~$0.05/h.
* ``quicktalk`` – L4 GPU (override with ``OT_GPU``), QuickTalk lip-sync. Needs
                  weights in the ``opentalking-models`` Volume (run
                  ``modal run deploy/modal/download_models.py::quicktalk`` once).

Deploy from the repo root::

    modal deploy deploy/modal/realtime_app.py                      # mock
    OT_MODE=quicktalk modal deploy deploy/modal/realtime_app.py    # QuickTalk on L4

Everything secret (LLM key, STT key, TURN credentials, basic-auth password)
lives in the Modal secret ``opentalking-env``; see deploy/modal/README.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

MODE = os.environ.get("OT_MODE", "mock").strip().lower()
if MODE not in {"mock", "quicktalk"}:
    raise SystemExit(f"OT_MODE must be mock or quicktalk, got {MODE!r}")
GPU = None if MODE == "mock" else os.environ.get("OT_GPU", "L4")
SECRET_NAME = os.environ.get("OT_SECRET_NAME", "opentalking-env")
APP_NAME = os.environ.get("OT_APP_NAME", f"opentalking-{MODE}")

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

# Persistent state: knowledge bases, LightRAG index, memory DBs, exports,
# uploaded avatars. Mounted at /app/data so the repo's ./data defaults apply.
data_volume = modal.Volume.from_name("opentalking-data", create_if_missing=True)
# Model weights (QuickTalk now, FlashTalk later). Filled by download_models.py.
models_volume = modal.Volume.from_name("opentalking-models", create_if_missing=True)

IGNORE = [
    ".git",
    ".venv",
    "**/node_modules",
    "apps/web/dist",
    "data",
    "models",
    "**/__pycache__",
    "**/*.pyc",
    ".env",
    "site",
]

extras = "dev" if MODE == "mock" else "dev,models,quicktalk-cuda"

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "ffmpeg", "nginx", "curl", "ca-certificates", "gnupg", "openssl", "libgl1", "libglib2.0-0"
    )
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
        "apt-get install -y nodejs",
        "rm -f /etc/nginx/sites-enabled/default",
    )
    .pip_install("uv")
    # Dependencies first (cached layers that only rebuild when pyproject.toml /
    # package-lock.json change), then the source tree.
    .add_local_file(REPO_ROOT / "pyproject.toml", "/build/pyproject.toml", copy=True)
    .add_local_file(
        HERE / "requirements_from_pyproject.py", "/build/requirements_from_pyproject.py", copy=True
    )
    .run_commands(
        f"python /build/requirements_from_pyproject.py /build/pyproject.toml {extras} > /build/requirements.txt",
        "uv pip install --system -r /build/requirements.txt",
    )
    .add_local_file(
        REPO_ROOT / "apps" / "web" / "package.json", "/build/web/package.json", copy=True
    )
    .add_local_file(
        REPO_ROOT / "apps" / "web" / "package-lock.json", "/build/web/package-lock.json", copy=True
    )
    .run_commands("cd /build/web && npm ci --no-audit --no-fund")
    .add_local_dir(REPO_ROOT, "/app", copy=True, ignore=IGNORE)
    .run_commands(
        "cd /app && uv pip install --system --no-deps -e .",
        "mv /build/web/node_modules /app/apps/web/node_modules",
        "cd /app/apps/web && VITE_API_BASE=/api npm run build",
    )
    .add_local_file(HERE / "nginx.conf", "/etc/nginx/conf.d/opentalking.conf", copy=True)
    .env(
        {
            "OT_APP_ROOT": "/app",
            "OPENTALKING_UNIFIED_HOST": "127.0.0.1",
            "OPENTALKING_UNIFIED_PORT": "8000",
            "OPENTALKING_AVATARS_DIR": "/app/data/avatars",
            "OPENTALKING_EXPORTS_DIR": "/app/data/exports",
            "OPENTALKING_MODEL_ROOT": "/models",
            "OPENTALKING_FFMPEG_BIN": "/usr/bin/ffmpeg",
            "OPENTALKING_TTS_STREAMING_DECODE": "1",
            "OPENTALKING_TTS_SAMPLE_RATE": "16000",
            "PYTHONUNBUFFERED": "1",
            "HF_HOME": "/models/.hf-cache",
        }
    )
)

if MODE == "mock":
    mode_env = {
        "OPENTALKING_DEFAULT_MODEL": "mock",
        "OMNIRT_ENDPOINT": "",
        "OPENTALKING_OMNIRT_ENDPOINT": "",
        "OPENTALKING_TORCH_DEVICE": "cpu",
    }
else:
    mode_env = {
        "OPENTALKING_DEFAULT_MODEL": "quicktalk",
        "OPENTALKING_QUICKTALK_BACKEND": "local",
        "OPENTALKING_QUICKTALK_ASSET_ROOT": "/models/quicktalk",
        "OPENTALKING_QUICKTALK_WORKER_CACHE": "1",
        "OPENTALKING_TORCH_DEVICE": "cuda:0",
        "OMNIRT_ENDPOINT": "",
        "OPENTALKING_OMNIRT_ENDPOINT": "",
    }
image = image.env(mode_env)

app = modal.App(APP_NAME)


@app.function(
    image=image,
    gpu=GPU,
    cpu=4.0,
    memory=8192 if MODE == "mock" else 16384,
    volumes={"/app/data": data_volume, "/models": models_volume},
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    # Sessions, the task queue and WebRTC peers live in one process: never
    # let Modal route a user to a second container.
    max_containers=1,
    min_containers=0,
    scaledown_window=int(os.environ.get("OT_SCALEDOWN_SEC", "300")),
    timeout=24 * 3600,
)
@modal.concurrent(max_inputs=200)
@modal.web_server(port=8080, startup_timeout=900)
def web() -> None:
    import sys

    sys.path.insert(0, "/app/deploy/modal")
    from modal_entrypoint import start_services

    os.chdir("/app")
    start_services(wait_sec=840.0)


@app.local_entrypoint()
def main() -> None:
    print(f"mode={MODE} gpu={GPU} app={APP_NAME} secret={SECRET_NAME}")
    print(
        "Deploy with: modal deploy deploy/modal/realtime_app.py  (or `modal serve` for a dev URL)"
    )
