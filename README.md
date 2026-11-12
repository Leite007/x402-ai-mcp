# x402-ai-mcp

An MCP server that gives your agent cheap, pay-per-call AI tools. No account, no API key, no subscription: each call is paid in USDC on Base through [x402](https://www.x402.org), from a wallet you control.

| Tool | Price per call | What it does |
|---|---|---|
| `chat` | $0.005 | Short LLM completion (Qwen3 14B, up to 512 tokens) |
| `chat_mini` | $0.002 | Classify, extract, yes/no (up to 128 tokens) |
| `semantic_rank` | $0.002 | Rank up to 15 texts by similarity to a query, 100+ languages |
| `embed` | $0.002 | BGE-M3 embeddings (1024-dim) for up to 16 texts, saved to a JSON file |
| `read_image` | $0.01 | Describe an image or OCR it (PNG/JPEG, up to 4 MB) |
| `pricing` | free | Live prices and what this session has spent |
| `wallet` | free | Paying address and its USDC balance |

The service behind it runs on a single dedicated machine: `https://x402.llnuvemlocal.com.br` ([llms.txt](https://x402.llnuvemlocal.com.br/llms.txt), [openapi.json](https://x402.llnuvemlocal.com.br/openapi.json)).

## Setup

You need [uv](https://docs.astral.sh/uv/) and a wallet with a few dollars of USDC **on Base**. Use a dedicated wallet, never your main one.

1. Save `server.py` somewhere, for example `~/x402-ai-mcp/server.py`.
2. Put the wallet's private key in a file only you can read:

   ```bash
   install -m 600 /dev/null ~/.x402-buyer.key && $EDITOR ~/.x402-buyer.key
   ```

3. Register the server in your MCP client.

   Claude Code:

   ```bash
   claude mcp add x402-ai -e X402_PRIVATE_KEY_FILE=$HOME/.x402-buyer.key -- uv run $HOME/x402-ai-mcp/server.py
   ```

   Claude Desktop, Cursor and others (`mcpServers` block):

   ```json
   {
     "mcpServers": {
       "x402-ai": {
         "command": "uv",
         "args": ["run", "/absolute/path/to/server.py"],
         "env": { "X402_PRIVATE_KEY_FILE": "/absolute/path/to/.x402-buyer.key" }
       }
     }
   }
   ```

Without a key the server still starts: `pricing` works and paid tools explain what is missing.

## Spending limits

| Variable | Default | Meaning |
|---|---|---|
| `X402_MAX_SPEND_USD` | `0.50` | Total the server may spend before it refuses (per process) |
| `X402_MAX_PER_CALL_USD` | `0.02` | Any single quote above this is refused before signing |
| `X402_PRIVATE_KEY_FILE` | — | File holding the buyer key (preferred) |
| `X402_PRIVATE_KEY` | — | The key itself, if you cannot use a file |

Failed calls (5xx, validation errors, unpaid 402) are not charged: settlement only happens after a successful response.

## What leaves your machine

Prompts, texts and images you pass to a paid tool are sent to the service to be processed. The private key never leaves your machine; it only signs a USDC transfer authorization (EIP-3009) for the exact quoted amount.

## Limits worth knowing

- One GPU, one generation at a time. Under load the service answers 503 and you are not charged.
- The chat model is a 14B open-weights model. It is good for cheap sub-tasks, not a replacement for a frontier model.
- Images must be local PNG or JPEG files. The service never fetches URLs.
