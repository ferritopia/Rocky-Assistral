from __future__ import annotations

import base64
import json
import re
from typing import AsyncIterable

import fastapi_poe as fp
from openai import AsyncOpenAI

from config import (
    MAX_TOOL_ITERATIONS, MISTRAL_API_KEY, MISTRAL_BASE_URL,
    MODEL, POE_ACCESS_KEY, WRITE_TOOLS,
)
from mcp_client import execute_tool, get_all_tools
from system_prompt import SYSTEM_PROMPT

mistral = AsyncOpenAI(api_key=MISTRAL_API_KEY, base_url=MISTRAL_BASE_URL)


# ── Pending action helpers ────────────────────────────────────────────────────

def _encode_pending(tool_name: str, args: dict) -> str:
    payload = json.dumps({"tool": tool_name, "args": args})
    encoded = base64.b64encode(payload.encode()).decode()
    return f"<!-- PENDING:{encoded} -->"


def _decode_pending(text: str) -> dict | None:
    match = re.search(r"<!-- PENDING:([A-Za-z0-9+/=]+) -->", text)
    if not match:
        return None
    try:
        return json.loads(base64.b64decode(match.group(1)).decode())
    except Exception:
        return None


def _is_confirmation(text: str) -> bool:
    words = {"ya", "yes", "oke", "ok", "konfirmasi", "confirm", "kirim", "send", "lanjut"}
    return text.strip().lower() in words


def _is_cancellation(text: str) -> bool:
    words = {"tidak", "no", "batal", "cancel", "stop", "jangan"}
    return text.strip().lower() in words


def _last_bot_message(query: list[fp.ProtocolMessage]) -> str | None:
    for msg in reversed(query[:-1]):
        if msg.role == "bot":
            return msg.content
    return None


def _build_openai_messages(query: list[fp.ProtocolMessage]) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in query:
        role = "user" if msg.role == "user" else "assistant"
        content = re.sub(r"<!-- PENDING:[A-Za-z0-9+/=]+ -->", "", msg.content).strip()
        if content:
            messages.append({"role": role, "content": content})
    return messages


def _format_confirmation(tool_name: str, args: dict) -> str:
    args_str = json.dumps(args, indent=2, ensure_ascii=False)
    return (
        f"⚠️ **Konfirmasi Diperlukan**\n\n"
        f"Tool: `{tool_name}`\n\n"
        f"```json\n{args_str}\n```\n\n"
        f"Ketik **konfirmasi** untuk melanjutkan atau **batal** untuk membatalkan.\n\n"
        f"{_encode_pending(tool_name, args)}"
    )


# ── Bot ───────────────────────────────────────────────────────────────────────

class AIAssistantBot(fp.PoeBot):

    async def get_response(
        self, request: fp.QueryRequest
    ) -> AsyncIterable[fp.PartialResponse]:
        user_msg = request.query[-1].content.strip()

        last_bot = _last_bot_message(request.query)
        if last_bot:
            pending = _decode_pending(last_bot)
            if pending:
                if _is_confirmation(user_msg):
                    async for chunk in self._run_confirmed(pending):
                        yield chunk
                    return
                if _is_cancellation(user_msg):
                    yield fp.PartialResponse(text="❌ Dibatalkan.")
                    return

        async for chunk in self._run_agent(request):
            yield chunk

    async def _run_confirmed(
        self, pending: dict
    ) -> AsyncIterable[fp.PartialResponse]:
        tool_name = pending["tool"]
        args = pending["args"]
        yield fp.PartialResponse(text=f"⚡ Menjalankan `{tool_name}`...\n\n")
        result = await execute_tool(tool_name, args)
        yield fp.PartialResponse(text=f"✅ Selesai\n\n{result}")

    async def _run_agent(
        self, request: fp.QueryRequest
    ) -> AsyncIterable[fp.PartialResponse]:
        tools = await get_all_tools()
        messages = _build_openai_messages(request.query)

        if not tools:
            yield fp.PartialResponse(
                text="⚠️ Tidak bisa connect ke MCP server. "
                     "Pastikan workflow n8n aktif.\n\n"
                     "Saya tetap bisa membantu untuk pertanyaan umum."
            )

        for _ in range(MAX_TOOL_ITERATIONS):
            call_kwargs: dict = {"model": MODEL, "messages": messages}
            if tools:
                call_kwargs["tools"] = tools
                call_kwargs["tool_choice"] = "auto"

            response = await mistral.chat.completions.create(**call_kwargs)
            choice = response.choices[0]
            msg = choice.message

            if not msg.tool_calls:
                yield fp.PartialResponse(text=msg.content or "")
                return

            assistant_entry: dict = {"role": "assistant", "content": msg.content or ""}
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
            messages.append(assistant_entry)

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                if tool_name in WRITE_TOOLS:
                    yield fp.PartialResponse(text=_format_confirmation(tool_name, tool_args))
                    return

                yield fp.PartialResponse(text=f"🔧 `{tool_name}`...\n")
                result = await execute_tool(tool_name, tool_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        yield fp.PartialResponse(text="⚠️ Terlalu banyak langkah. Sederhanakan permintaannya.")

    async def get_settings(self, setting: fp.SettingsRequest) -> fp.SettingsResponse:
        return fp.SettingsResponse()


app = fp.make_app(AIAssistantBot(), access_key=POE_ACCESS_KEY)
