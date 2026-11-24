"""Batch-evaluate customer-service answers through a running OpenTalking.

Drives the real session pipeline (persona/system prompt, knowledge base,
LLM, TTS) over the HTTP API, so what you measure is what users get:

    python deploy/modal/qa_eval.py \
        --base-url https://<workspace>--opentalking-mock-web.modal.run/api \
        --cases deploy/modal/qa_cases.example.yaml \
        --auth user:password \
        --out qa_results.jsonl

For each case it creates a session, sends the question with POST /speak,
and listens on the SSE stream for the first subtitle chunk and the final
``speech.ended`` reply. It reports latency and simple keyword checks
(``expect_any`` / ``expect_all`` / ``forbid``); read the replies yourself too:
keyword hits are a smoke test, not a grade.

Only needs ``httpx`` and ``PyYAML`` locally (``pip install httpx pyyaml``).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
import yaml


def load_cases(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(data, list):
        return {}, data
    return dict(data.get("defaults") or {}), list(data.get("cases") or [])


async def sse_reader(client: httpx.AsyncClient, url: str, queue: asyncio.Queue) -> None:
    async with client.stream("GET", url, timeout=None) as resp:
        resp.raise_for_status()
        event, data_lines = "message", []
        async for line in resp.aiter_lines():
            if line == "":
                if data_lines:
                    raw = "\n".join(data_lines)
                    try:
                        payload: Any = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = raw
                    await queue.put((event, payload, time.perf_counter()))
                event, data_lines = "message", []
            elif line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())


def check_reply(reply: str, case: dict[str, Any]) -> dict[str, Any]:
    text = reply.lower()
    expect_any = [str(x).lower() for x in case.get("expect_any") or []]
    expect_all = [str(x).lower() for x in case.get("expect_all") or []]
    forbid = [str(x).lower() for x in case.get("forbid") or []]
    result: dict[str, Any] = {}
    if expect_any:
        result["expect_any_ok"] = any(x in text for x in expect_any)
    if expect_all:
        missing = [x for x in expect_all if x not in text]
        result["expect_all_ok"] = not missing
        if missing:
            result["missing"] = missing
    if forbid:
        hits = [x for x in forbid if x in text]
        result["forbid_ok"] = not hits
        if hits:
            result["forbidden_hits"] = hits
    checks = [v for k, v in result.items() if k.endswith("_ok")]
    result["pass"] = all(checks) if checks else None
    return result


async def run_case(
    client: httpx.AsyncClient,
    base: str,
    defaults: dict[str, Any],
    case: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    create = {
        "avatar_id": case.get("avatar_id", defaults.get("avatar_id", "office-woman")),
        "model": case.get("model", defaults.get("model", "mock")),
        "tts_provider": case.get("tts_provider", defaults.get("tts_provider")),
        "tts_voice": case.get("tts_voice", defaults.get("tts_voice")),
        "llm_system_prompt": case.get("system_prompt", defaults.get("system_prompt")),
        "persona_id": case.get("persona_id", defaults.get("persona_id")),
        "knowledge_base_ids": case.get("knowledge_base_ids", defaults.get("knowledge_base_ids")),
        "knowledge_enabled": case.get("knowledge_enabled", defaults.get("knowledge_enabled", True)),
    }
    create = {k: v for k, v in create.items() if v not in (None, "", [])}
    turns = case.get("turns") or [case["question"]]

    resp = await client.post(f"{base}/sessions", json=create, timeout=120)
    resp.raise_for_status()
    sid = resp.json()["session_id"]
    queue: asyncio.Queue = asyncio.Queue()
    reader = asyncio.create_task(sse_reader(client, f"{base}/sessions/{sid}/events", queue))
    out: dict[str, Any] = {"id": case.get("id"), "session_id": sid, "turns": []}
    try:
        (await client.post(f"{base}/sessions/{sid}/start", timeout=180)).raise_for_status()
        for turn in turns:
            while not queue.empty():
                queue.get_nowait()
            t0 = time.perf_counter()
            (
                await client.post(f"{base}/sessions/{sid}/speak", json={"text": turn}, timeout=60)
            ).raise_for_status()
            first_chunk_ms = None
            reply = None
            out_ms: float | None = None
            subtitle_parts: list[str] = []
            deadline = t0 + timeout
            while time.perf_counter() < deadline:
                try:
                    event, payload, ts = await asyncio.wait_for(
                        queue.get(), timeout=deadline - time.perf_counter()
                    )
                except asyncio.TimeoutError:
                    break
                if event == "subtitle.chunk":
                    if first_chunk_ms is None:
                        first_chunk_ms = (ts - t0) * 1000
                    if isinstance(payload, dict) and payload.get("text"):
                        subtitle_parts.append(str(payload["text"]))
                elif event == "speech.ended":
                    reply = (payload.get("text") if isinstance(payload, dict) else None) or "".join(
                        subtitle_parts
                    )
                    out_ms = (ts - t0) * 1000
                    break
                elif event == "error":
                    reply = f"<error: {payload}>"
                    out_ms = (ts - t0) * 1000
                    break
            if reply is None:
                reply, out_ms = "".join(subtitle_parts) or "<timeout>", None
            out["turns"].append(
                {
                    "question": turn,
                    "reply": reply,
                    "first_subtitle_ms": round(first_chunk_ms)
                    if first_chunk_ms is not None
                    else None,
                    "speech_ended_ms": round(out_ms) if out_ms is not None else None,
                }
            )
        final_reply = out["turns"][-1]["reply"]
        out.update(check_reply(final_reply, case))
    finally:
        reader.cancel()
        try:
            await client.delete(f"{base}/sessions/{sid}", timeout=30)
        except httpx.HTTPError:
            pass
    return out


async def main_async(args: argparse.Namespace) -> None:
    defaults, cases = load_cases(Path(args.cases))
    if args.only:
        cases = [c for c in cases if str(c.get("id")) in set(args.only.split(","))]
    headers = {}
    if args.auth:
        headers["Authorization"] = "Basic " + base64.b64encode(args.auth.encode()).decode()
    base = args.base_url.rstrip("/")
    results: list[dict[str, Any]] = []
    out_path = Path(args.out)
    async with httpx.AsyncClient(headers=headers) as client:
        with out_path.open("w", encoding="utf-8") as fh:
            for case in cases:
                try:
                    res = await run_case(client, base, defaults, case, args.timeout)
                except Exception as exc:  # noqa: BLE001 - keep going, record the failure
                    res = {"id": case.get("id"), "error": repr(exc), "pass": False}
                results.append(res)
                fh.write(json.dumps(res, ensure_ascii=False) + "\n")
                fh.flush()
                last = (res.get("turns") or [{}])[-1]
                status = {True: "PASS", False: "FAIL", None: "----"}[res.get("pass")]
                print(
                    f"[{status}] {res.get('id')}: first_subtitle={last.get('first_subtitle_ms')}ms ended={last.get('speech_ended_ms')}ms"
                )
                print(f"        Q: {last.get('question', '')}")
                print(f"        A: {(last.get('reply') or res.get('error', ''))[:300]}")

    firsts = [
        t["first_subtitle_ms"]
        for r in results
        for t in r.get("turns", [])
        if t.get("first_subtitle_ms") is not None
    ]
    graded = [r["pass"] for r in results if r.get("pass") is not None]
    print("\n=== summary ===")
    print(f"cases: {len(results)}  graded: {len(graded)}  passed: {sum(graded)}")
    if firsts:
        firsts.sort()
        p90 = firsts[min(len(firsts) - 1, int(len(firsts) * 0.9))]
        print(
            f"first subtitle ms: median={statistics.median(firsts):.0f} p90={p90:.0f} max={max(firsts):.0f}"
        )
    print(f"details: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", required=True, help="API base, e.g. https://...modal.run/api")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--auth", default="", help="user:password for nginx basic auth")
    parser.add_argument("--out", default="qa_results.jsonl")
    parser.add_argument(
        "--timeout", type=float, default=90.0, help="seconds to wait for each reply"
    )
    parser.add_argument("--only", default="", help="comma-separated case ids")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
