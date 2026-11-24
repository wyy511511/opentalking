"""Download model weights into the ``opentalking-models`` Modal Volume.

Runs on a cheap CPU container; weights then mount read-only-ish at /models in
the GPU containers, so they are never baked into images.

    modal run deploy/modal/download_models.py::quicktalk     # step 3 (small)
    modal run deploy/modal/download_models.py::flashtalk     # step 4 (large)
    modal run deploy/modal/download_models.py::list_models   # show what's there

Set HF_TOKEN in the ``opentalking-env`` secret if Hugging Face rate-limits you.
"""

from __future__ import annotations

import os

import modal

SECRET_NAME = os.environ.get("OT_SECRET_NAME", "opentalking-env")

models_volume = modal.Volume.from_name("opentalking-models", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("huggingface_hub[hf_transfer]>=0.30,<1.0")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "HF_HOME": "/models/.hf-cache"})
)
app = modal.App("opentalking-download-models")

common = dict(
    image=image,
    volumes={"/models": models_volume},
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    timeout=3 * 3600,
    cpu=4.0,
    memory=8192,
)


def _snapshot(repo_id: str, local_dir: str) -> None:
    from huggingface_hub import snapshot_download

    print(f"Downloading {repo_id} -> {local_dir}", flush=True)
    snapshot_download(
        repo_id=repo_id, local_dir=local_dir, token=os.environ.get("HF_TOKEN") or None
    )
    models_volume.commit()
    print(f"Done: {repo_id}", flush=True)


@app.function(**common)
def quicktalk() -> None:
    # Includes QuickTalk, HuBERT and InsightFace buffalo_l
    # (docs/en/avatar_models/deployment/quicktalk-local.md).
    _snapshot("datascale-ai/quicktalk", "/models/quicktalk/checkpoints")


@app.function(**common)
def flashtalk() -> None:
    # docs/en/avatar_models/flashtalk.md
    _snapshot("TencentGameMate/chinese-wav2vec2-base", "/models/chinese-wav2vec2-base")
    _snapshot("Soul-AILab/SoulX-FlashTalk-14B", "/models/SoulX-FlashTalk-14B")


@app.function(image=image, volumes={"/models": models_volume})
def list_models() -> None:
    import subprocess

    subprocess.run(
        [
            "bash",
            "-lc",
            "du -sh /models/* 2>/dev/null; find /models -maxdepth 3 -not -path '*/.hf-cache*' | head -80",
        ]
    )
