"""Container-side bootstrap for running OpenTalking on Modal.

Kept free of any ``modal`` import so it can also be run locally for a smoke
test::

    OT_APP_ROOT=$PWD OT_NGINX=0 python deploy/modal/modal_entrypoint.py

Responsibilities:

1. Resolve WebRTC ICE servers. Modal containers have no inbound UDP, so media
   must go through a TURN relay. With Cloudflare Realtime TURN credentials in
   the environment we mint short-lived ICE credentials at startup; otherwise
   any ``OPENTALKING_WEBRTC_*`` variables you set are used as-is.
2. Write the optional nginx basic-auth snippet.
3. Seed persistent avatar storage on first boot.
4. Start ``opentalking-unified`` (API + worker in one process) and nginx, then
   wait until the API answers ``/models``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_ROOT = Path(os.environ.get("OT_APP_ROOT", "/app"))
API_PORT = int(os.environ.get("OPENTALKING_UNIFIED_PORT", "8000"))
AUTH_CONF = Path(os.environ.get("OT_NGINX_AUTH_CONF", "/etc/nginx/opentalking-auth.conf"))
HTPASSWD = Path(os.environ.get("OT_NGINX_HTPASSWD", "/etc/nginx/opentalking.htpasswd"))

CLOUDFLARE_TURN_URL = (
    "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
)


def log(msg: str) -> None:
    print(f"[opentalking-modal] {msg}", flush=True)


# ---------------------------------------------------------------------------
# ICE / TURN
# ---------------------------------------------------------------------------


def _as_url_list(urls: object) -> list[str]:
    if isinstance(urls, str):
        return [urls]
    if isinstance(urls, list):
        return [str(u) for u in urls if str(u).strip()]
    return []


def normalize_ice_servers(payload: object) -> list[dict[str, object]]:
    """Accept Cloudflare's old (dict) and new (list) response shapes."""
    if isinstance(payload, dict) and "iceServers" in payload:
        payload = payload["iceServers"]
    if isinstance(payload, dict):
        payload = [payload]
    servers: list[dict[str, object]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        # Browsers refuse TURN/STUN on port 53; Cloudflare advertises it anyway.
        urls = [
            u
            for u in _as_url_list(item.get("urls") or item.get("url"))
            if ":53?" not in u and not u.endswith(":53")
        ]
        if not urls:
            continue
        server: dict[str, object] = {"urls": urls}
        if item.get("username"):
            server["username"] = item["username"]
        if item.get("credential"):
            server["credential"] = item["credential"]
        servers.append(server)
    return servers


def pick_server_turn(servers: list[dict[str, object]], transport: str) -> list[dict[str, object]]:
    """aiortc only uses the first TURN URL it sees, so hand it exactly one.

    ``transport`` is ``tls`` (turns:...?transport=tcp, safest from a cloud
    container), ``tcp`` or ``udp`` (lowest latency, needs outbound UDP).
    """

    def matches(url: str) -> bool:
        if transport == "tls":
            return url.startswith("turns:")
        if transport == "tcp":
            return url.startswith("turn:") and "transport=tcp" in url
        return url.startswith("turn:") and ("transport=udp" in url or "transport=" not in url)

    preferred: list[tuple[int, str, dict[str, object]]] = []
    for server in servers:
        for url in _as_url_list(server.get("urls")):
            if matches(url):
                # Prefer port 443: it passes almost every egress firewall.
                preferred.append((0 if ":443" in url else 1, url, server))
    if not preferred:
        return []
    preferred.sort(key=lambda item: item[0])
    _, url, server = preferred[0]
    out: dict[str, object] = {"urls": url}
    for key in ("username", "credential"):
        if server.get(key):
            out[key] = server[key]
    return [out]


def fetch_cloudflare_ice_servers(key_id: str, api_token: str, ttl: int) -> list[dict[str, object]]:
    req = urllib.request.Request(
        CLOUDFLARE_TURN_URL.format(key_id=key_id),
        data=json.dumps({"ttl": ttl}).encode(),
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return normalize_ice_servers(json.loads(resp.read().decode()))


def configure_ice() -> None:
    key_id = os.environ.get("CLOUDFLARE_TURN_KEY_ID", "").strip()
    api_token = os.environ.get("CLOUDFLARE_TURN_API_TOKEN", "").strip()
    transport = os.environ.get("OT_SERVER_TURN_TRANSPORT", "tls").strip().lower()
    if key_id and api_token:
        ttl = int(os.environ.get("OT_TURN_TTL_SEC", "172800"))
        servers = fetch_cloudflare_ice_servers(key_id, api_token, ttl)
        if not servers:
            raise RuntimeError("Cloudflare TURN returned no ICE servers")
        os.environ["OPENTALKING_WEBRTC_ICE_SERVERS"] = json.dumps(servers)
        log(
            f"Cloudflare TURN: {sum(len(_as_url_list(s['urls'])) for s in servers)} browser ICE URLs (ttl={ttl}s)"
        )
    elif (
        os.environ.get("OPENTALKING_WEBRTC_ICE_SERVERS")
        or os.environ.get("OPENTALKING_WEBRTC_TURN_URL")
        or os.environ.get("OPENTALKING_WEBRTC_TURN_URLS")
    ):
        log("Using OPENTALKING_WEBRTC_* ICE settings from the environment")
    else:
        log(
            "WARNING: no TURN server configured. Modal has no inbound UDP, so browser "
            "WebRTC media will most likely fail. Set CLOUDFLARE_TURN_KEY_ID / "
            "CLOUDFLARE_TURN_API_TOKEN (or OPENTALKING_WEBRTC_ICE_SERVERS) in the Modal secret."
        )
        return

    if not os.environ.get("OPENTALKING_WEBRTC_SERVER_ICE_SERVERS"):
        # Reuse the adapter's own parser so env-style TURN settings are honoured too.
        sys.path.insert(0, str(APP_ROOT))
        from opentalking.providers.rtc.aiortc.adapter import get_webrtc_ice_config_payload

        browser = normalize_ice_servers(get_webrtc_ice_config_payload())
        server = pick_server_turn(browser, transport)
        if server:
            os.environ["OPENTALKING_WEBRTC_SERVER_ICE_SERVERS"] = json.dumps(server)
            log(f"Server-side aiortc TURN ({transport}): {server[0]['urls']}")
        else:
            log(
                f"WARNING: no TURN URL matching transport={transport}; server keeps browser ICE list"
            )


# ---------------------------------------------------------------------------
# nginx auth, storage seeding, process supervision
# ---------------------------------------------------------------------------


def configure_basic_auth() -> None:
    user = os.environ.get("OT_BASIC_AUTH_USER", "").strip()
    password = os.environ.get("OT_BASIC_AUTH_PASSWORD", "")
    AUTH_CONF.parent.mkdir(parents=True, exist_ok=True)
    if not (user and password):
        AUTH_CONF.write_text("# basic auth disabled\n")
        log(
            "WARNING: OT_BASIC_AUTH_USER/OT_BASIC_AUTH_PASSWORD unset; the public URL is open to anyone"
        )
        return
    hashed = subprocess.run(
        ["openssl", "passwd", "-apr1", "-stdin"],
        input=password,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    HTPASSWD.write_text(f"{user}:{hashed}\n")
    AUTH_CONF.write_text(f'auth_basic "OpenTalking";\nauth_basic_user_file {HTPASSWD};\n')
    log(f"nginx basic auth enabled for user {user!r}")


def seed_avatars() -> None:
    target = Path(os.environ.get("OPENTALKING_AVATARS_DIR", str(APP_ROOT / "examples" / "avatars")))
    source = APP_ROOT / "examples" / "avatars"
    if target.resolve() == source.resolve():
        return
    marker = target / ".seeded"
    if marker.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        dest = target / item.name
        if dest.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    log(f"Seeded built-in avatars into {target}")


def _api_ready() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/models", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_services(*, wait_sec: float = 600.0, start_nginx: bool = True) -> list[subprocess.Popen]:
    configure_ice()
    seed_avatars()
    procs: list[subprocess.Popen] = []

    env = dict(os.environ)
    env.setdefault("OPENTALKING_UNIFIED_HOST", "127.0.0.1")
    env.setdefault("OPENTALKING_UNIFIED_PORT", str(API_PORT))
    log(
        f"Starting opentalking-unified on 127.0.0.1:{API_PORT} (default model={env.get('OPENTALKING_DEFAULT_MODEL', 'mock')})"
    )
    procs.append(subprocess.Popen(["opentalking-unified"], cwd=APP_ROOT, env=env))

    if start_nginx:
        configure_basic_auth()
        subprocess.run(["nginx", "-t"], check=True)
        procs.append(subprocess.Popen(["nginx", "-g", "daemon off;"]))
        log("nginx listening on :8080")

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if procs[0].poll() is not None:
            raise RuntimeError(f"opentalking-unified exited early with code {procs[0].returncode}")
        if _api_ready():
            log("OpenTalking API is ready")
            return procs
        time.sleep(1)
    raise TimeoutError(f"OpenTalking API not ready after {wait_sec:.0f}s")


if __name__ == "__main__":
    os.chdir(APP_ROOT)
    running = start_services(start_nginx=os.environ.get("OT_NGINX", "1") == "1")
    try:
        running[0].wait()
    finally:
        for proc in running:
            proc.terminate()
