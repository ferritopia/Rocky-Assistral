import os

# ── LLM ──────────────────────────────────────────────────────────────────────
MODEL            = os.environ["MISTRAL_MODEL"]       # mistral-small-2603
MISTRAL_API_KEY  = os.environ["MISTRAL_API_KEY"]
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"

# ── Poe ───────────────────────────────────────────────────────────────────────
POE_ACCESS_KEY = os.environ["POE_ACCESS_KEY"]

# ── MCP Servers ───────────────────────────────────────────────────────────────
# Comma-separated di Railway env var MCP_SERVER_URLS
# Kalau mau tambah server baru, edit value di Railway saja — tidak perlu ubah code
MCP_SERVERS: list[str] = [
    url.strip()
    for url in os.environ["MCP_SERVER_URLS"].split(",")
    if url.strip()
]

# ── Write Tools ───────────────────────────────────────────────────────────────
# Tools yang butuh konfirmasi user sebelum dieksekusi
WRITE_TOOLS: set[str] = {
    "create_ticket",
    "update_ticket",
    "add_note",
    "add_reply",
}

# ── Misc ──────────────────────────────────────────────────────────────────────
MAX_TOOL_ITERATIONS = 10
MCP_CACHE_TTL       = 300
