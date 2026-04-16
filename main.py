"""
main.py
───────
FastAPI app + Poe bot handler.

Alur per request:
1. Cek apakah user sedang konfirmasi pending action → eksekusi jika ya
2. Kalau tidak, jalankan tool calling loop dengan Mistral
3. Tiap tool call: cek apakah write tool → minta konfirmasi, atau langsung eksekusi
4. Stream response ke Poe

Ini yang "berpikir" — mcp_client.py hanya eksekutor.
"""

import json
import base64
import re
from typing import AsyncIterable

import fastapi_poe as fp
from openai import AsyncOpenAI

from config import (
    MODEL, MISTRAL_API_KEY, MISTRAL_BASE_URL,
    POE_ACCESS_KEY, WRITE_TOOLS, MAX_TOOL_ITERATIONS
)
from mcp_client import get_all_tools, get_read_only_tools, execute_tool
from system_prompt import SYSTEM_PROMPT

# ── Mistral client (OpenAI-compatible) ───────────────────────────────────────
mistral = AsyncOpenAI(
    api_key=MISTRAL_API_KEY,
    base_url=MISTRAL_BASE_URL,
)


# ── Pending Action Helpers ────────────────────────────────────────────────────
# Saat AI mau eksekusi write tool, kita ENCODE action-nya ke HTML comment
# tersembunyi di response. Request berikutnya, kita decode dan cek konfirmasi.

def _encode_pending(tool_name: str, args: dict) -> str:
    """Encode pending action ke HTML comment tersembunyi."""
    payload = json.dumps({"tool": tool_name, "args": args})
    encoded = base64.b64encode(payload.encode()).decode()
    return f"<!-- PENDING:{encoded} -->"


def _decode_pending(text: str) -> dict | None:
    """Decode pending action dari HTML comment. Return None kalau tidak ada."""
    match = re.search(r"<!-- PENDING:([A-Za-z0-9+/=]+) -->", text)
    if not match:
        return None
    try:
        payload = base64.b64decode(match.group(1)).decode()
        return json.loads(payload)
    except Exception:
        return None


def _is_confirmation(text: str) -> bool:
    """Apakah pesan user adalah konfirmasi?"""
    confirm_words = {"ya", "yes", "oke", "ok", "konfirmasi", "confirm", "kirim", "send", "lanjut", "go"}
    return text.strip().lower() in confirm_words


def _is_cancellation(text: str) -> bool:
    """Apakah pesan user adalah pembatalan?"""
    cancel_words = {"tidak", "no", "batal", "cancel", "stop", "jangan"}
    return text.strip().lower() in cancel_words


def _get_last_bot_message(query: list[fp.ProtocolMessage]) -> str | None:
    """Ambil pesan bot terakhir dari history (bukan current user message)."""
    for msg in reversed(query[:-1]):  # Exclude current user message
        if msg.role == "bot":
            return msg.content
    return None


def _poe_to_openai_messages(query: list[fp.ProtocolMessage]) -> list[dict]:
    """
    Convert Poe message format ke OpenAI messages format.
    Poe kirim full history tiap request — kita butuh ini untuk context.
    Tapi kita skip HTML comments (PENDING markers) dari bot messages.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for msg in query:
        role = "user" if msg.role == "user" else "assistant"
        # Bersihkan pending marker dari bot messages agar tidak confuse LLM
        content = re.sub(r"<!-- PENDING:[A-Za-z0-9+/=]+ -->", "", msg.content).strip()
        if content:
            messages.append({"role": role, "content": content})

    return messages


def _format_confirmation_request(tool_name: str, args: dict) -> str:
    """
    Format pesan konfirmasi yang akan ditampilkan ke user.
    Termasuk PENDING marker tersembunyi.
    """
    # Format args jadi readable
    args_display = json.dumps(args, indent=2, ensure_ascii=False)

    # Label berdasarkan tool
    action_labels = {
        "add_reply": "📤 Mengirim balasan ke tiket",
        "add_note": "📝 Menambahkan catatan ke tiket",
        "create_ticket": "🎫 Membuat tiket baru",
        "update_ticket": "✏️ Mengupdate tiket",
    }
    label = action_labels.get(tool_name, f"⚡ Menjalankan {tool_name}")

    msg = f"""⚠️ **Konfirmasi Diperlukan**

{label}

```json
{args_display}
```

Ketik **konfirmasi** untuk melanjutkan, atau **batal** untuk membatalkan.

{_encode_pending(tool_name, args)}"""

    return msg


# ── Poe Bot Class ─────────────────────────────────────────────────────────────

class AIAssistantBot(fp.PoeBot):

    async def get_response(
        self, request: fp.QueryRequest
    ) -> AsyncIterable[fp.PartialResponse]:
        """
        Dipanggil Poe setiap ada pesan baru dari user.
        """
        current_user_msg = request.query[-1].content.strip()

        # ── 1. Cek apakah ini konfirmasi untuk pending action ─────────────────
        last_bot_msg = _get_last_bot_message(request.query)
        if last_bot_msg:
            pending = _decode_pending(last_bot_msg)

            if pending and _is_confirmation(current_user_msg):
                # User konfirmasi → eksekusi pending action
                async for chunk in self._execute_confirmed_action(pending):
                    yield chunk
                return

            if pending and _is_cancellation(current_user_msg):
                yield fp.PartialResponse(text="❌ Action dibatalkan.")
                return

        # ── 2. Jalankan tool calling loop normal ──────────────────────────────
        async for chunk in self._run_agent(request):
            yield chunk

    async def _execute_confirmed_action(
        self, pending: dict
    ) -> AsyncIterable[fp.PartialResponse]:
        """Eksekusi action yang sudah dikonfirmasi user."""
        tool_name = pending["tool"]
        args = pending["args"]

        yield fp.PartialResponse(text=f"⚡ Mengeksekusi `{tool_name}`...\n\n")

        result = await execute_tool(tool_name, args)

        # Format result
        yield fp.PartialResponse(text=f"✅ **Selesai**\n\n{result}")

    async def _run_agent(
        self, request: fp.QueryRequest
    ) -> AsyncIterable[fp.PartialResponse]:
        """
        Main agent loop:
        - Load tools dari MCP
        - Kirim ke Mistral
        - Handle tool calls (read: langsung, write: minta konfirmasi)
        - Stream final response
        """
        # Load tools dari semua MCP servers
        tools = await get_all_tools()

        if not tools:
            yield fp.PartialResponse(
                text="⚠️ Tidak bisa connect ke MCP server. "
                     "Pastikan n8n workflow aktif dan URL benar.\n\n"
                     "Saya tetap bisa membantu untuk pertanyaan umum."
            )
            # Lanjut tanpa tools
            tools = None

        # Build messages dari Poe conversation history
        messages = _poe_to_openai_messages(request.query)

        # ── Tool calling loop ─────────────────────────────────────────────────
        for iteration in range(MAX_TOOL_ITERATIONS):

            # Panggil Mistral
            response = await mistral.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools if tools else fp.UNSET,
                tool_choice="auto" if tools else fp.UNSET,
            )

            choice = response.choices[0]
            assistant_message = choice.message

            # ── Tidak ada tool call → final response ─────────────────────────
            if not assistant_message.tool_calls:
                content = assistant_message.content or ""
                yield fp.PartialResponse(text=content)
                return

            # ── Ada tool call(s) ──────────────────────────────────────────────
            # Tambahkan assistant message ke history
            messages.append(assistant_message.model_dump(exclude_unset=True))

            for tool_call in assistant_message.tool_calls:
                tool_name = tool_call.function.name
                try:
                    tool_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                # ── Write tool → minta konfirmasi ────────────────────────────
                if tool_name in WRITE_TOOLS:
                    confirmation_msg = _format_confirmation_request(tool_name, tool_args)
                    yield fp.PartialResponse(text=confirmation_msg)
                    return  # Stop loop, tunggu konfirmasi user

                # ── Read tool → langsung eksekusi ─────────────────────────────
                yield fp.PartialResponse(text=f"🔧 `{tool_name}`...\n")

                result = await execute_tool(tool_name, tool_args)

                # Tambahkan tool result ke messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

            # Loop lagi → Mistral memproses hasil tools dan memutuskan next step

        # Kalau sudah max iterations
        yield fp.PartialResponse(
            text="⚠️ Terlalu banyak langkah. Mohon sederhanakan permintaannya."
        )

    async def get_settings(
        self, setting: fp.SettingsRequest
    ) -> fp.SettingsResponse:
        return fp.SettingsResponse(
            allow_attachments=False,
            server_bot_dependencies={},
        )


# ── FastAPI App ───────────────────────────────────────────────────────────────
app = fp.make_app(AIAssistantBot(), access_key=POE_ACCESS_KEY)
