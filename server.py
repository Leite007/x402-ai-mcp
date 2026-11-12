# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2,<2",
#   "x402[httpx,evm]>=2.27",
#   "eth-account>=0.13",
#   "httpx>=0.27",
# ]
# ///
"""MCP server: pay-per-call AI tools over x402 (USDC on Base).

Gives any MCP client (Claude Desktop, Claude Code, Cursor, ...) four paid tools
backed by https://x402.llnuvemlocal.com.br — no account, no API key:

  chat           $0.005   short LLM completion (up to 512 tokens)
  chat_mini      $0.002   classify / extract / yes-no (up to 128 tokens)
  semantic_rank  $0.002   rank up to 15 texts by similarity to a query
  embed          $0.002   multilingual embeddings (BGE-M3, 1024-dim), saved to a file
  read_image     $0.01    describe an image / OCR (PNG or JPEG, up to 4 MB)

and two free ones: `pricing` and `wallet`.

Payment comes from YOUR wallet, configured by environment variable:

  X402_PRIVATE_KEY_FILE   path to a file holding the private key (recommended), or
  X402_PRIVATE_KEY        the private key itself
  X402_MAX_SPEND_USD      spend cap for this server process (default 0.50)
  X402_MAX_PER_CALL_USD   refuse any single quote above this (default 0.02)
  X402_BASE_URL           service URL (default https://x402.llnuvemlocal.com.br)

Use a dedicated wallet holding a few dollars of USDC on Base. Never your main wallet.
Run:  uv run server.py
"""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("X402_BASE_URL", "https://x402.llnuvemlocal.com.br").rstrip("/")
NETWORK = "eip155:8453"  # Base mainnet
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RPC = os.environ.get("X402_BASE_RPC", "https://mainnet.base.org")
MAX_SPEND = float(os.environ.get("X402_MAX_SPEND_USD", "0.50"))
MAX_PER_CALL = float(os.environ.get("X402_MAX_PER_CALL_USD", "0.02"))
MAX_IMAGE_BYTES = 4 * 1024 * 1024

PRICES = {"/v1/chat": 0.005, "/v1/chat-mini": 0.002, "/v1/embed": 0.002, "/v1/vision": 0.01}

mcp = FastMCP("x402-ai")
_spent = 0.0
_calls = 0


class ToolError(Exception):
    pass


def _account():
    """Loads the buyer wallet, or returns None when no key is configured."""
    from eth_account import Account

    key = os.environ.get("X402_PRIVATE_KEY", "").strip()
    keyfile = os.environ.get("X402_PRIVATE_KEY_FILE", "").strip()
    if not key and keyfile:
        key = Path(keyfile).expanduser().read_text().strip()
    return Account.from_key(key) if key else None


NO_WALLET = (
    "No wallet configured, so nothing was paid and nothing was called. "
    "Set X402_PRIVATE_KEY_FILE (or X402_PRIVATE_KEY) in this MCP server's environment to the key of a "
    "dedicated wallet holding a little USDC on Base, then restart the server. "
    "The `pricing` tool works without a wallet."
)


async def _paid_post(path: str, body: dict) -> dict:
    """POSTs to a paid route, settling the x402 quote from the configured wallet."""
    global _spent, _calls
    price = PRICES[path]
    acct = _account()
    if acct is None:
        raise ToolError(NO_WALLET)
    if _spent + price > MAX_SPEND + 1e-9:
        raise ToolError(
            f"Spend cap reached: ${_spent:.3f} spent of ${MAX_SPEND:.2f} allowed in this session "
            f"(X402_MAX_SPEND_USD). Nothing was paid."
        )

    from x402 import max_amount, x402Client
    from x402.http.clients import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner

    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(acct), networks=NETWORK)
    # Hard ceiling on what a single 402 quote may ask for (USDC has 6 decimals).
    client.register_policy(max_amount(int(round(MAX_PER_CALL * 1_000_000))))

    try:
        async with x402HttpxClient(client, timeout=300) as http:
            r = await http.post(BASE_URL + path, json=body)
    except httpx.HTTPError as e:
        raise ToolError(f"Network error calling {path}: {e}. Nothing was charged.") from e
    except Exception as e:  # payment refused by policy, signing problems, ...
        raise ToolError(f"Payment was not made for {path}: {e}") from e

    if r.status_code == 402:
        raise ToolError(
            f"The service still answered 402 for {path}: the payment was not accepted "
            f"(most often: not enough USDC on Base in {acct.address}). Nothing was charged."
        )
    if r.status_code != 200:
        raise ToolError(f"{path} answered HTTP {r.status_code}: {r.text[:300]}. Failed calls are not charged.")
    if r.headers.get("PAYMENT-RESPONSE"):
        _spent += price
        _calls += 1
    return r.json()


def _footer(price: float) -> str:
    return f"\n\n[paid ${price:.3f} USDC · session total ${_spent:.3f} of ${MAX_SPEND:.2f}]"


async def _run(path: str, body: dict, render) -> str:
    try:
        out = await _paid_post(path, body)
    except ToolError as e:
        return f"ERROR: {e}"
    return render(out) + _footer(PRICES[path])


@mcp.tool()
async def chat(prompt: str, max_tokens: int = 256) -> str:
    """Short LLM completion from an open-weights model (Qwen3 14B). Costs $0.005 USDC per call.

    Good for delegating cheap sub-tasks: summarize, rewrite, translate, draft. English and Portuguese.
    prompt: up to 8000 characters. max_tokens: 1..512.
    """
    if not 1 <= len(prompt) <= 8000:
        return "ERROR: prompt must have 1..8000 characters. Nothing was charged."
    body = {"prompt": prompt, "max_tokens": max(1, min(int(max_tokens), 512))}
    return await _run("/v1/chat", body, lambda o: o.get("text", ""))


@mcp.tool()
async def chat_mini(prompt: str, max_tokens: int = 128) -> str:
    """Micro LLM completion for classify, extract, yes/no, one-line answers. Costs $0.002 USDC per call.

    prompt: up to 2000 characters. max_tokens: 1..128.
    """
    if not 1 <= len(prompt) <= 2000:
        return "ERROR: prompt must have 1..2000 characters. Nothing was charged."
    body = {"prompt": prompt, "max_tokens": max(1, min(int(max_tokens), 128))}
    return await _run("/v1/chat-mini", body, lambda o: o.get("text", ""))


def _check_texts(texts: list[str], limit: int) -> str | None:
    if not 1 <= len(texts) <= limit:
        return f"ERROR: send 1..{limit} texts per call. Nothing was charged."
    if any(not t or len(t) > 8000 for t in texts):
        return "ERROR: each text must have 1..8000 characters. Nothing was charged."
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@mcp.tool()
async def semantic_rank(query: str, candidates: list[str]) -> str:
    """Ranks up to 15 texts by semantic similarity to a query (multilingual). Costs $0.002 USDC per call.

    Use it to pick the most relevant passages, deduplicate, or match across languages
    (BGE-M3 embeddings, 100+ languages). Returns candidates sorted by cosine similarity.
    """
    err = _check_texts([query] + list(candidates), 16)
    if err:
        return err
    if not candidates:
        return "ERROR: send at least one candidate. Nothing was charged."

    def render(o: dict) -> str:
        vecs = o["embeddings"]
        scored = sorted(((_cosine(vecs[0], v), i) for i, v in enumerate(vecs[1:])), reverse=True)
        return json.dumps(
            [{"rank": n + 1, "index": i, "score": round(s, 4), "text": candidates[i]}
             for n, (s, i) in enumerate(scored)],
            ensure_ascii=False, indent=1)

    return await _run("/v1/embed", {"input": [query] + list(candidates)}, render)


@mcp.tool()
async def embed(texts: list[str], save_to: str) -> str:
    """Embeds up to 16 texts (BGE-M3, 1024 dimensions) and saves the vectors as JSON. Costs $0.002 USDC per call.

    Vectors are written to the file `save_to` (a new path on this machine) instead of being
    returned, because 1024 numbers per text would flood the conversation.
    """
    err = _check_texts(list(texts), 16)
    if err:
        return err
    dest = Path(save_to).expanduser()
    if dest.exists():
        return f"ERROR: {dest} already exists; choose a new file path. Nothing was charged."
    if not dest.parent.is_dir():
        return f"ERROR: folder {dest.parent} does not exist. Nothing was charged."

    def render(o: dict) -> str:
        dest.write_text(json.dumps(
            {"model": o["model"], "dimensions": o["dimensions"],
             "items": [{"text": t, "embedding": v} for t, v in zip(texts, o["embeddings"])]},
            ensure_ascii=False))
        return f"Saved {len(o['embeddings'])} vectors of {o['dimensions']} dimensions ({o['model']}) to {dest}"

    return await _run("/v1/embed", {"input": list(texts)}, render)


@mcp.tool()
async def read_image(image_path: str, prompt: str = "Describe this image and transcribe any text in it.",
                     max_tokens: int = 256) -> str:
    """Describes an image or reads its text (OCR) with a vision LLM. Costs $0.01 USDC per call.

    image_path: a local PNG or JPEG file, up to 4 MB (screenshots, receipts, documents, photos).
    The image is uploaded to the service to be read. prompt: optional question about the image.
    """
    p = Path(image_path).expanduser()
    if not p.is_file():
        return f"ERROR: file not found: {p}. Nothing was charged."
    raw = p.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        return "ERROR: image larger than 4 MB. Nothing was charged."
    if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
        return "ERROR: only PNG and JPEG are accepted. Nothing was charged."
    body = {"image_base64": base64.b64encode(raw).decode(), "prompt": prompt[:2000],
            "max_tokens": max(1, min(int(max_tokens), 512))}
    return await _run("/v1/vision", body, lambda o: o.get("text", ""))


@mcp.tool()
async def pricing() -> str:
    """Free. Lists the paid tools, their live prices and limits, and what this session has spent."""
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            live = (await http.get(BASE_URL + "/agents.json")).json().get("endpoints", [])
    except Exception as e:
        live = [{"error": f"could not fetch live catalog: {e}"}]
    return json.dumps({
        "service": BASE_URL, "network": "Base mainnet (eip155:8453)", "currency": "USDC",
        "endpoints": [{k: e.get(k) for k in ("url", "price_usd", "description") if k in e} or e for e in live],
        "session": {"calls_paid": _calls, "spent_usd": round(_spent, 4), "cap_usd": MAX_SPEND,
                    "max_per_call_usd": MAX_PER_CALL},
    }, ensure_ascii=False, indent=1)


@mcp.tool()
async def wallet() -> str:
    """Free. Shows the paying wallet's address and its USDC balance on Base. Never shows the key."""
    try:
        acct = _account()
    except Exception as e:
        return f"ERROR: could not load the wallet key: {type(e).__name__}"
    if acct is None:
        return NO_WALLET
    data = "0x70a08231" + acct.address[2:].lower().rjust(64, "0")
    try:
        async with httpx.AsyncClient(timeout=20, headers={"user-agent": "curl/8"}) as http:
            r = await http.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                           "params": [{"to": USDC, "data": data}, "latest"]})
            balance = f"{int(r.json()['result'], 16) / 1e6:.6f} USDC"
    except Exception as e:
        balance = f"unknown (RPC error: {e})"
    return json.dumps({"address": acct.address, "network": "Base mainnet", "balance": balance,
                       "session_spent_usd": round(_spent, 4), "session_cap_usd": MAX_SPEND}, indent=1)


if __name__ == "__main__":
    mcp.run()
