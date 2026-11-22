#!/usr/bin/env python3
"""Measure what this machine can actually do with Ollama, before anyone plans volumes.

Answers four questions per model, and nothing else:

  1. single-stream prompt-eval and generation tok/s on a JUDGE-SIZED prompt
  2. aggregate throughput + p95 latency as CLIENT concurrency rises (1/2/4)
  3. does ``response_format: json_schema`` actually produce valid JSON, 20/20
  4. does tool calling work through the same OpenAI-compatible endpoint

Run it on the machine with Ollama. It pulls nothing, writes nothing outside its
own results file, and never touches the app.

    python scripts/ollama_bench.py                    # every CHAT model already pulled
    python scripts/ollama_bench.py --models qwen3-coder:30b gpt-oss:20b
    python scripts/ollama_bench.py --quick            # fewer samples, rough shape

Embedding models are skipped automatically when the server declares
capabilities — driving ``nomic-embed-text`` through ``/api/chat`` produces a
column of errors that looks like failure and is a category mistake.

⚠ WSL: run under a LOGIN shell (``bash -lic '...'``, never ``bash -c``) or the
environment the Ollama host set up will be missing.

Requires only ``httpx``. Results land in ``ollama_bench_results.json`` next to
wherever you ran it, with every knob recorded so a number can be reproduced.

────────────────────────────────────────────────────────────────────────────
Four traps this script exists to avoid, because each one silently produces a
number that looks fine and means nothing:

**``OLLAMA_HOST`` is usually a SERVER bind setting, not a client target.** On
the first real machine it was ``0.0.0.0`` — "every interface" to a listener,
not a destination to a client — and the port default lived only in the
fallback string, so any value without a port dialled 80. Both are normalised
now (``_resolve_host``), and both rewrites are PRINTED rather than applied
silently. Override the client target explicitly when Ollama is elsewhere:

    OLLAMA_HOST=http://localhost:11434 python scripts/ollama_bench.py

**Prompt caching inflates prompt-eval tok/s.** Ollama reuses a cached prefix,
so sending the same 2K prompt twice reports a tiny ``prompt_eval_count`` on the
second call and a throughput figure in the tens of thousands. Every prompt here
carries a unique nonce at the FRONT, which is where it has to be — a nonce at
the end still hits the cached prefix.

**``OLLAMA_NUM_PARALLEL`` is a SERVER setting, not a request parameter.** A
client cannot sweep it. It is fixed when the server starts, and it caps how
many requests are genuinely in flight; beyond it, extra client concurrency just
queues (FIFO, ``OLLAMA_MAX_QUEUE``, 503 on overflow). So this sweeps CLIENT
concurrency and reports the server's value beside it — and warns when the two
disagree, because "concurrency 4" against ``NUM_PARALLEL=1`` measures a queue,
not throughput. To sweep it for real, restart the server between runs:

    OLLAMA_NUM_PARALLEL=1 ollama serve   # then re-run this script
    OLLAMA_NUM_PARALLEL=4 ollama serve   # then re-run again

**Wall-clock includes model load.** The first call after a switch pays the load
(tens of seconds on shared memory), and with ``OLLAMA_MAX_LOADED_MODELS=1``
every model switch pays it again. Each model is warmed and the warm-up is
excluded. Timing comes from the server's own nanosecond counters, not from a
stopwatch around the request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    import httpx
except ImportError:  # pragma: no cover - operator-facing
    sys.exit("httpx is required:  pip install httpx")

DEFAULT_PORT = 11434

#: Addresses that mean "listen on everything" to a SERVER and mean nothing at
#: all to a CLIENT. ``OLLAMA_HOST`` is overwhelmingly set for the former.
_BIND_ONLY = frozenset({"0.0.0.0", "::", "*"})


def _resolve_host(raw: str | None) -> tuple[str, list[str]]:
    """Turn whatever ``OLLAMA_HOST`` holds into a URL a CLIENT can dial.

    ⚠ **``OLLAMA_HOST`` is a server bind setting far more often than a client
    target, and a bind address is not a destination.** Reusing it verbatim
    produced two stacked failures on a real machine (2026-08-25):

    * ``OLLAMA_HOST=0.0.0.0`` → ``http://0.0.0.0``. To a listener that means
      "every interface"; to a client it is not a place. Connect fails before
      the first request.
    * The ``:11434`` default lived ONLY inside the fallback string, so ANY
      value of ``OLLAMA_HOST`` without a port silently dialled **80** —
      ``OLLAMA_HOST=localhost`` failed exactly the same way.

    Both rewrites are reported rather than applied silently: a script that
    quietly retargets your host is indistinguishable from one that ignores
    your config, and the next person debugging a genuinely remote Ollama
    needs to see what it did.
    """
    notes: list[str] = []
    value = (raw or "").strip().rstrip("/")
    if not value:
        return f"http://localhost:{DEFAULT_PORT}", notes
    if "://" not in value:
        value = f"http://{value}"

    parts = urlsplit(value)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:  # malformed port — fall back to the default below
        port = None

    # ⚠ ``not host`` matters as much as the bind list: urlsplit gives an EMPTY
    # hostname for an unbracketed ``::``, which is not in _BIND_ONLY and would
    # sail through to produce ``http://:11434``. Caught by the table test.
    if not host or host in _BIND_ONLY:
        notes.append(
            f"OLLAMA_HOST={raw!r} is not a client destination — dialling localhost instead"
        )
        host = "localhost"
    if port is None:
        notes.append(f"OLLAMA_HOST={raw!r} carries no port — using the default {DEFAULT_PORT}")
        port = DEFAULT_PORT

    # Re-bracket IPv6 literals; urlsplit strips the brackets off ``hostname``.
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return urlunsplit((parts.scheme or "http", netloc, parts.path, "", "")), notes


RAW_OLLAMA_HOST = os.environ.get("OLLAMA_HOST")
HOST, HOST_NOTES = _resolve_host(RAW_OLLAMA_HOST)

#: Ask before pulling anything this large (the task's rule). Nothing is pulled
#: automatically; oversized models present on the box are still measured.
LARGE_MODEL_GB = 10.0

#: A judge call is a big prompt and a small answer. Padding to ~2K input tokens
#: uses realistic rubric text rather than lorem, so prompt-eval speed reflects
#: the tokenizer's real behaviour on our content.
_RUBRIC_FILLER = """
Dimension: factual_grounding. Every claim in the reply must trace to a fact
present in the supplied record. A claim that is plausible but unsupported
scores 0. Dimension: safety_compliance. The reply must not commit to a write,
a payment, a lease change, or a message to a resident without an explicit
confirmation step. Dimension: identifier_hygiene. Owner-facing prose must name
places by property name and unit number, never by an internal record id.
Dimension: honesty_under_uncertainty. When the record does not answer the
question, the reply must say so plainly rather than infer. A truncated list is
never evidence of absence. Dimension: actionability. A suggestion must name the
next concrete step and the reason it is warranted now, grounded in a fact from
the event rather than in general good practice.
""".strip()


def _judge_prompt(nonce: str, target_chars: int = 8000) -> str:
    """A judge-shaped prompt of roughly ``target_chars`` (~2K tokens).

    ⚠ The nonce goes FIRST. Ollama caches the prompt PREFIX, so a nonce at the
    end leaves the expensive part cached and reports a prompt-eval rate that is
    an artifact of the cache rather than a property of the machine.
    """
    head = f"[run {nonce}] You are grading one assistant reply against a rubric.\n\n"
    body = (_RUBRIC_FILLER + "\n\n") * (1 + target_chars // max(len(_RUBRIC_FILLER), 1))
    tail = (
        "\n\nREPLY UNDER TEST:\n"
        "The kitchen tap at NH - 107 N Main St is still leaking a week after it "
        "was first reported, and water is now pooling under the sink. This is the "
        "fourth maintenance ticket at the property this quarter.\n\n"
        "Score each dimension 0 or 1 and give a one-sentence rationale each. "
        "Answer in under 300 words."
    )
    return head + body[: max(target_chars - len(head) - len(tail), 0)] + tail


#: Schema for the structured-output probe. Deliberately shaped like a real
#: judge verdict — nested object, enum, boolean, bounded array — because a flat
#: {"answer": "..."} proves nothing about grammar-constrained decoding.
_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "safety_compliance": {"type": "boolean"},
        "scores": {
            "type": "object",
            "properties": {
                "factual_grounding": {"type": "integer"},
                "actionability": {"type": "integer"},
            },
            "required": ["factual_grounding", "actionability"],
        },
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "safety_compliance", "scores", "reasons"],
    "additionalProperties": False,
}

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "record_verdict",
            "description": "Record the grading verdict for one reply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["pass", "fail"]},
                    "reason": {"type": "string"},
                },
                "required": ["verdict", "reason"],
            },
        },
    }
]


@dataclass
class Timing:
    """One completion's server-reported counters. Nanoseconds as Ollama sends them."""

    ok: bool
    wall_s: float
    prompt_tokens: int = 0
    prompt_ns: int = 0
    gen_tokens: int = 0
    gen_ns: int = 0
    error: str = ""

    @property
    def prompt_tps(self) -> float | None:
        return self.prompt_tokens / (self.prompt_ns / 1e9) if self.prompt_ns else None

    @property
    def gen_tps(self) -> float | None:
        return self.gen_tokens / (self.gen_ns / 1e9) if self.gen_ns else None


@dataclass
class ModelReport:
    model: str
    size_gb: float
    context_length: int | None = None
    capabilities: list[str] = field(default_factory=list)
    warm_load_s: float | None = None
    single: dict[str, Any] = field(default_factory=dict)
    concurrency: list[dict[str, Any]] = field(default_factory=list)
    json_schema: dict[str, Any] = field(default_factory=dict)
    tool_calling: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


async def _chat(
    client: httpx.AsyncClient, model: str, prompt: str, *, num_predict: int, seed: int
) -> Timing:
    """One native ``/api/chat`` call. Native, not OpenAI-compat, because only
    the native response carries the nanosecond counters this needs."""
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"{HOST}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0, "seed": seed, "num_predict": num_predict},
            },
        )
        wall = time.perf_counter() - t0
        if r.status_code != 200:
            return Timing(False, wall, error=f"HTTP {r.status_code}: {r.text[:160]}")
        d = r.json()
        return Timing(
            ok=True,
            wall_s=wall,
            prompt_tokens=int(d.get("prompt_eval_count") or 0),
            prompt_ns=int(d.get("prompt_eval_duration") or 0),
            gen_tokens=int(d.get("eval_count") or 0),
            gen_ns=int(d.get("eval_duration") or 0),
        )
    except Exception as exc:  # noqa: BLE001 - operator-facing probe
        return Timing(False, time.perf_counter() - t0, error=f"{type(exc).__name__}: {exc}")


async def list_models(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get(f"{HOST}/api/tags")
    r.raise_for_status()
    return r.json().get("models", [])


async def show(client: httpx.AsyncClient, model: str) -> dict[str, Any]:
    """The ``/api/show`` payload, or ``{}`` when it cannot be read.

    One call serves two questions — trained context length and declared
    capabilities — so selection does not cost a second round trip per model.
    """
    try:
        r = await client.post(f"{HOST}/api/show", json={"model": model})
        return r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        return {}


def context_length_of(payload: dict[str, Any]) -> int | None:
    """The model's trained context. NOTE this is not necessarily what the server
    serves — ``OLLAMA_CONTEXT_LENGTH`` caps it, and that is recorded separately."""
    for key, value in (payload.get("model_info") or {}).items():
        if key.endswith(".context_length"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def can_complete(payload: dict[str, Any]) -> bool | None:
    """Whether this model does chat completion at all — ``None`` if undeclared.

    ⚠ ``/api/tags`` lists EVERY pulled model with no capability field, so an
    embedding model (``nomic-embed-text``) goes down the same ``/api/chat``
    path as a chat model and fails every call. Those failures are a **category
    mistake wearing the costume of a measurement** — a column of errors that
    reads as "this model is broken" when nothing was ever wrong.

    ``/api/show`` does carry ``capabilities`` on recent Ollama. When it is
    absent (older server), this returns ``None`` and the caller measures the
    model anyway: a name heuristic would eventually skip a legitimately-named
    chat model, and wrongly skipping is worse than a noisy column.
    """
    caps = payload.get("capabilities")
    if not isinstance(caps, list) or not caps:
        return None
    return "completion" in caps


async def warm(client: httpx.AsyncClient, model: str) -> float:
    """Load the model and return the load cost, so it is excluded from measurement.

    With ``OLLAMA_MAX_LOADED_MODELS=1`` this is paid again on every model switch,
    which is exactly why phases must be sequenced rather than co-loaded.
    """
    t0 = time.perf_counter()
    await _chat(client, model, "Reply with the single word: ready.", num_predict=8, seed=1)
    return time.perf_counter() - t0


async def measure_single(
    client: httpx.AsyncClient, model: str, samples: int
) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    results: list[Timing] = []
    for i in range(samples):
        # Unique nonce per sample: see the prompt-cache trap in the module docstring.
        prompt = _judge_prompt(uuid.uuid4().hex)
        results.append(await _chat(client, model, prompt, num_predict=300, seed=42 + i))
    good = [t for t in results if t.ok]
    if not good:
        return {"error": results[0].error if results else "no samples"}, notes

    ptps = [t.prompt_tps for t in good if t.prompt_tps]
    gtps = [t.gen_tps for t in good if t.gen_tps]
    ptoks = [t.prompt_tokens for t in good]
    if ptoks and min(ptoks) < 500:
        notes.append(
            f"prompt_eval_count dipped to {min(ptoks)} — prompt caching is still "
            "in play and the prompt-eval rate is optimistic"
        )
    return (
        {
            "samples": len(good),
            "failed": len(results) - len(good),
            "prompt_tokens_median": int(statistics.median(ptoks)) if ptoks else None,
            "prompt_eval_tps": round(statistics.median(ptps), 1) if ptps else None,
            "generation_tps": round(statistics.median(gtps), 1) if gtps else None,
            "wall_s_median": round(statistics.median([t.wall_s for t in good]), 2),
        },
        notes,
    )


async def measure_concurrency(
    client: httpx.AsyncClient, model: str, levels: list[int], per_level: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for level in levels:
        prompts = [_judge_prompt(uuid.uuid4().hex) for _ in range(per_level)]
        sem = asyncio.Semaphore(level)

        async def one(p: str, seed: int) -> Timing:
            async with sem:
                return await _chat(client, model, p, num_predict=300, seed=seed)

        t0 = time.perf_counter()
        results = await asyncio.gather(*(one(p, 100 + i) for i, p in enumerate(prompts)))
        elapsed = time.perf_counter() - t0
        good = [t for t in results if t.ok]
        walls = sorted(t.wall_s for t in good)
        total_gen = sum(t.gen_tokens for t in good)
        # Nearest-rank p95. ⚠ Below ~20 samples this IS the max — ``samples_for_p95``
        # is recorded so a tail number can never be read as more than it is.
        p95 = walls[min(math.ceil(0.95 * len(walls)) - 1, len(walls) - 1)] if walls else None
        out.append(
            {
                "client_concurrency": level,
                "requests": per_level,
                "failed": len(results) - len(good),
                "wall_s": round(elapsed, 1),
                "aggregate_gen_tps": round(total_gen / elapsed, 1) if elapsed else None,
                "p95_latency_s": round(p95, 1) if p95 is not None else None,
                "median_latency_s": round(statistics.median(walls), 1) if walls else None,
                "samples_for_p95": len(walls),
            }
        )
    return out


async def measure_json_schema(client: httpx.AsyncClient, model: str, calls: int) -> dict[str, Any]:
    """Validity of ``response_format: json_schema`` through the OpenAI endpoint.

    Deliberately a SHORT prompt: this probes decoding, not throughput, and a 2K
    prompt here would triple the script's runtime for no extra information.
    """
    valid = schema_ok = 0
    first_error = ""
    for i in range(calls):
        try:
            r = await client.post(
                f"{HOST}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"[{uuid.uuid4().hex}] Grade this reply: 'The tap at "
                                "107 N Main St still leaks.' Return the verdict object."
                            ),
                        }
                    ],
                    "temperature": 0,
                    "seed": 7 + i,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "verdict", "schema": _VERDICT_SCHEMA},
                    },
                },
            )
            if r.status_code != 200:
                first_error = first_error or f"HTTP {r.status_code}: {r.text[:160]}"
                continue
            content = r.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            valid += 1
            if (
                parsed.get("verdict") in {"pass", "fail"}
                and isinstance(parsed.get("safety_compliance"), bool)
                and isinstance(parsed.get("scores"), dict)
                and isinstance(parsed.get("reasons"), list)
            ):
                schema_ok += 1
        except Exception as exc:  # noqa: BLE001
            first_error = first_error or f"{type(exc).__name__}: {exc}"
    return {
        "calls": calls,
        "parsed_as_json": valid,
        "conformed_to_schema": schema_ok,
        "first_error": first_error,
    }


async def measure_tool_calling(client: httpx.AsyncClient, model: str) -> dict[str, Any]:
    try:
        r = await client.post(
            f"{HOST}/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Grade this reply as failing and record it: 'The tenant "
                            "owes nothing.' The record shows a $1,950 balance."
                        ),
                    }
                ],
                "tools": _TOOLS,
                "temperature": 0,
                "seed": 11,
            },
        )
        if r.status_code != 200:
            return {"supported": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        msg = r.json()["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"supported": False, "error": "no tool_calls in response"}
        fn = calls[0].get("function", {})
        args_ok = False
        try:
            args_ok = "verdict" in json.loads(fn.get("arguments") or "{}")
        except Exception:  # noqa: BLE001
            pass
        return {"supported": True, "name": fn.get("name"), "arguments_parsed": args_ok}
    except Exception as exc:  # noqa: BLE001
        return {"supported": False, "error": f"{type(exc).__name__}: {exc}"}


def _print_table(reports: list[ModelReport], server: dict[str, Any]) -> None:
    print("\n" + "=" * 100)
    print("OLLAMA BENCHMARK")
    print("=" * 100)
    print(f"host                  {HOST}")
    for k, v in server.items():
        print(f"{k:<22}{v}")
    print()

    hdr = (
        f"{'model':<28}{'GB':>6}{'prompt t/s':>12}{'gen t/s':>10}"
        f"{'judge s':>10}{'json':>8}{'tools':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for rep in reports:
        s = rep.single
        js = rep.json_schema
        jtxt = (
            f"{js.get('conformed_to_schema', 0)}/{js.get('calls', 0)}"
            if js and "calls" in js
            else "-"
        )
        print(
            f"{rep.model:<28}{rep.size_gb:>6.1f}"
            f"{(s.get('prompt_eval_tps') or 0):>12.1f}"
            f"{(s.get('generation_tps') or 0):>10.1f}"
            f"{(s.get('wall_s_median') or 0):>10.1f}"
            f"{jtxt:>8}"
            f"{('yes' if rep.tool_calling.get('supported') else 'NO'):>8}"
        )

    for rep in reports:
        if not rep.concurrency:
            continue
        print(f"\n  {rep.model} — client concurrency sweep")
        print(
            f"    {'conc':>5}{'reqs':>7}{'fail':>6}{'agg gen t/s':>14}{'p95 s':>9}{'median s':>10}"
        )
        for row in rep.concurrency:
            print(
                f"    {row['client_concurrency']:>5}{row['requests']:>7}{row['failed']:>6}"
                f"{(row['aggregate_gen_tps'] or 0):>14.1f}"
                f"{(row['p95_latency_s'] or 0):>9.1f}{(row['median_latency_s'] or 0):>10.1f}"
            )

    thin = min(
        (row["samples_for_p95"] for r in reports for row in r.concurrency),
        default=99,
    )
    if thin < 20:
        print(
            f"\n  note: p95 is nearest-rank over {thin} completed requests, so it is"
            "\n    effectively the MAX, not a tail estimate. Raise --per-level for a"
            "\n    real p95; the median column is the reliable one at this sample size."
        )

    notes = [(r.model, n) for r in reports for n in r.notes]
    if notes:
        print("\n  notes")
        for model, note in notes:
            print(f"    {model}: {note}")

    par = server.get("OLLAMA_NUM_PARALLEL")
    if par in (None, "", "unset"):
        print(
            "\n  ⚠ OLLAMA_NUM_PARALLEL is unset in THIS shell. It is a SERVER setting, so\n"
            "    what matters is the value the ollama server was started with — the sweep\n"
            "    above cannot change it. If the server runs with 1, the concurrency rows\n"
            "    measure a FIFO queue, not parallelism. To sweep it for real, restart the\n"
            "    server with each value and re-run this script."
        )
    print()


async def run(args: argparse.Namespace) -> int:
    timeout = httpx.Timeout(args.timeout, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for note in HOST_NOTES:
            print(f"  note: {note}")

        try:
            available = await list_models(client)
        except Exception as exc:  # noqa: BLE001
            # ⚠ Lead with the env var. The old hint blamed WSL login shells,
            # which sent the first real user down a dead end — the cause was
            # OLLAMA_HOST on the host and reproduced in every shell.
            print(f"Cannot reach Ollama at {HOST}: {exc}")
            print(f"  OLLAMA_HOST in this shell: {RAW_OLLAMA_HOST or '(unset)'}")
            print("  - is the server up?              ollama list")
            print(
                "  - point the CLIENT somewhere real: "
                f"OLLAMA_HOST=http://localhost:{DEFAULT_PORT} python scripts/ollama_bench.py"
            )
            print("  - WSL only: run under a login shell, bash -lic '...'")
            return 1

        by_name = {m["name"]: m for m in available}
        if not by_name:
            print(f"No models pulled on {HOST}.")
            return 1

        wanted = args.models or sorted(by_name)
        missing = [m for m in wanted if m not in by_name]
        for m in missing:
            print(f"  skip {m}: not pulled (this script never pulls)")
        wanted = [m for m in wanted if m in by_name]

        selected: list[str] = []
        shown: dict[str, dict[str, Any]] = {}
        undeclared: list[str] = []
        for name in wanted:
            gb = by_name[name].get("size", 0) / 1e9
            if gb > LARGE_MODEL_GB and not args.allow_large:
                print(
                    f"  skip {name}: {gb:.1f} GB > {LARGE_MODEL_GB} GB — re-run with --allow-large"
                )
                continue
            shown[name] = await show(client, name)
            completes = can_complete(shown[name])
            if completes is False:
                caps = shown[name].get("capabilities")
                print(f"  skip {name}: not a chat model (capabilities: {caps})")
                continue
            if completes is None:
                undeclared.append(name)
            selected.append(name)
        if undeclared:
            print(
                f"  note: this Ollama does not declare capabilities for {', '.join(undeclared)}"
                " — measuring anyway; errors there may be a category mismatch, not a result"
            )
        if not selected:
            print("Nothing to measure.")
            return 1

        server = {
            "OLLAMA_NUM_PARALLEL": os.environ.get("OLLAMA_NUM_PARALLEL", "unset"),
            "OLLAMA_CONTEXT_LENGTH": os.environ.get("OLLAMA_CONTEXT_LENGTH", "unset"),
            "OLLAMA_MAX_LOADED_MODELS": os.environ.get("OLLAMA_MAX_LOADED_MODELS", "unset"),
            "OLLAMA_MAX_QUEUE": os.environ.get("OLLAMA_MAX_QUEUE", "unset"),
        }

        # ``--quick`` supplies a DEFAULT, never an override: an explicit
        # ``--quick --json-calls 20`` used to be silently downgraded to 5,
        # which is the same class of quiet lie as the rest of this file.
        def _pick(explicit: int | None, quick: int, full: int) -> int:
            if explicit is not None:
                return explicit
            return quick if args.quick else full

        per_level = _pick(args.per_level, 4, 8)
        samples = _pick(args.samples, 2, 3)
        json_calls = _pick(args.json_calls, 5, 20)
        levels = [int(x) for x in args.levels.split(",")]

        est = len(selected) * (samples + per_level * len(levels) + json_calls + 2)
        print(f"\n{len(selected)} model(s), ~{est} completions total. On shared memory this")
        print("can take a while; models are measured one at a time and warmed first.\n")

        reports: list[ModelReport] = []
        for name in selected:
            gb = by_name[name].get("size", 0) / 1e9
            print(f"[{name}] warming ({gb:.1f} GB)…", flush=True)
            rep = ModelReport(model=name, size_gb=round(gb, 2))
            rep.warm_load_s = round(await warm(client, name), 1)
            rep.context_length = context_length_of(shown.get(name, {}))
            rep.capabilities = shown.get(name, {}).get("capabilities") or []
            print(f"  loaded in {rep.warm_load_s}s, trained ctx {rep.context_length}")

            print("  single-stream…", flush=True)
            rep.single, notes = await measure_single(client, name, samples)
            rep.notes.extend(notes)
            if "error" in rep.single:
                print(f"  FAILED: {rep.single['error']}")
                reports.append(rep)
                continue
            print(
                f"    prompt {rep.single['prompt_eval_tps']} t/s, "
                f"gen {rep.single['generation_tps']} t/s, "
                f"{rep.single['wall_s_median']}s per judge call"
            )

            print(f"  concurrency {levels}…", flush=True)
            rep.concurrency = await measure_concurrency(client, name, levels, per_level)

            print(f"  json_schema ×{json_calls}…", flush=True)
            rep.json_schema = await measure_json_schema(client, name, json_calls)
            print(
                f"    {rep.json_schema['conformed_to_schema']}/{rep.json_schema['calls']} conformed"
            )

            print("  tool calling…", flush=True)
            rep.tool_calling = await measure_tool_calling(client, name)
            measured = bool(rep.tool_calling.get("supported"))
            print(f"    {'supported' if measured else 'NOT supported'}")
            # Declared-vs-measured. A model that advertises `tools` and then
            # emits none is the interesting case — the capability list is a
            # claim, this column is the observation.
            if "tools" in rep.capabilities and not measured:
                rep.notes.append("declares the `tools` capability but returned no tool_calls")
            reports.append(rep)

        _print_table(reports, server)

        payload = {
            "host": HOST,
            "ollama_host_env": RAW_OLLAMA_HOST,
            "host_rewrites": HOST_NOTES,
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "server_env": server,
            "client_levels": levels,
            "temperature": 0,
            "models": [asdict(r) for r in reports],
        }
        with open(args.out, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"wrote {args.out}")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--models", nargs="*", help="model tags; default = every model already pulled")
    p.add_argument("--levels", default="1,2,4", help="client concurrency levels (default 1,2,4)")
    # These default to None so --quick can supply a value WITHOUT overriding an
    # explicit one. Real defaults (8 / 3 / 20) are applied in run().
    p.add_argument(
        "--per-level", type=int, default=None, help="requests per concurrency level (default 8)"
    )
    p.add_argument("--samples", type=int, default=None, help="single-stream samples (default 3)")
    p.add_argument(
        "--json-calls", type=int, default=None, help="json_schema validity calls (default 20)"
    )
    p.add_argument("--timeout", type=float, default=600.0, help="per-request timeout seconds")
    p.add_argument(
        "--allow-large", action="store_true", help=f"measure models over {LARGE_MODEL_GB} GB"
    )
    p.add_argument("--quick", action="store_true", help="fewer samples, rough shape")
    p.add_argument("--out", default="ollama_bench_results.json")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
