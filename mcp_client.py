"""
mcp_client.py
─────────────
Bertanggung jawab untuk:
1. Connect ke semua MCP SSE servers
2. Load daftar tools (di-cache 5 menit)
3. Eksekusi tool call dari Mistral
4. Convert format MCP → format OpenAI tools

Dipanggil oleh main.py, tidak tahu soal Poe atau LLM.
"""

import time
import json
from mcp import ClientSession
from mcp.client.sse import sse_client
from config import MCP_SERVERS, MCP_CACHE_TTL

# ── In-memory cache ───────────────────────────────────────────────────────────
_tools_openai_format: list[dict] = []     # Tools dalam format OpenAI (dikirim ke Mistral)
_tool_to_server: dict[str, str] = {}      # Mapping: tool_name → MCP server URL
_last_refresh: float = 0                  # Timestamp refresh terakhir


async def _refresh_tools() -> None:
    """
    Connect ke semua MCP servers, ambil daftar tools,
    convert ke format OpenAI, dan build mapping tool→server.
    """
    global _tools_openai_format, _tool_to_server, _last_refresh

    all_tools = []
    tool_map = {}

    for server_url in MCP_SERVERS:
        try:
            async with sse_client(server_url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()

                    for tool in result.tools:
                        # Convert MCP tool schema → format OpenAI function calling
                        openai_tool = {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description or "",
                                "parameters": tool.inputSchema or {
                                    "type": "object",
                                    "properties": {}
                                }
                            }
                        }
                        all_tools.append(openai_tool)
                        tool_map[tool.name] = server_url

        except Exception as e:
            # Jangan crash kalau satu server down — log dan lanjut
            print(f"⚠️  MCP server tidak bisa diakses: {server_url}\n   Error: {e}")

    _tools_openai_format = all_tools
    _tool_to_server = tool_map
    _last_refresh = time.time()

    print(f"✅ Loaded {len(all_tools)} tools dari {len(MCP_SERVERS)} MCP server(s)")


async def get_all_tools(force_refresh: bool = False) -> list[dict]:
    """
    Return semua tools dalam format OpenAI.
    Otomatis refresh kalau cache sudah expired (>5 menit) atau kosong.
    """
    cache_expired = (time.time() - _last_refresh) > MCP_CACHE_TTL
    if force_refresh or not _tools_openai_format or cache_expired:
        await _refresh_tools()
    return _tools_openai_format


async def get_read_only_tools() -> list[dict]:
    """
    Return hanya read tools (tanpa write tools).
    Dipakai di draft mode agar AI tidak bisa accidentally write.
    """
    from config import WRITE_TOOLS
    tools = await get_all_tools()
    return [t for t in tools if t["function"]["name"] not in WRITE_TOOLS]


async def execute_tool(tool_name: str, tool_args: dict) -> str:
    """
    Eksekusi satu tool call ke MCP server yang tepat.
    Return: string hasil (sudah di-extract dari MCP response).
    """
    server_url = _tool_to_server.get(tool_name)

    if not server_url:
        # Tool tidak ditemukan — coba refresh cache dulu
        await _refresh_tools()
        server_url = _tool_to_server.get(tool_name)
        if not server_url:
            return f"❌ Tool '{tool_name}' tidak ditemukan di MCP server manapun."

    try:
        async with sse_client(server_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, tool_args)

                # Extract text content dari MCP response
                if result.content:
                    texts = [
                        item.text
                        for item in result.content
                        if hasattr(item, "text") and item.text
                    ]
                    return "\n".join(texts) if texts else "✅ Tool berhasil dieksekusi (tidak ada output)"

                return "✅ Tool berhasil dieksekusi"

    except Exception as e:
        return f"❌ Error saat eksekusi {tool_name}: {str(e)}"
