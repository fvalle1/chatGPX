"""Reusable chat backend for chatgpx.

Connects to the MCP server in server.py over stdio (same
stdio_client + ClientSession pattern as client.py), exposes its tools
(list_activities, read_activity, get_activity_stats,
get_stats_summary, generate_activity_video, summarize) to an
LLM as OpenAI-style function/tool calls, and runs the actual conversation
through whatever OpenAI-compatible endpoint OPENAI_BASE_URL points to -
same OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL env vars as client.py,
so one provider (Mistral, OpenRouter, OpenAI, ...) and one key cover both.
The model just needs to support OpenAI-style tool/function calling.

Library usage:
    from chat_backend import chat
    reply = await chat("summarize my longest run")

HTTP usage (mount into another FastAPI app, or run this one directly):
    from chat_backend import router          # APIRouter, mount with app.include_router(router)
    from chat_backend import app             # standalone FastAPI app exposing POST /chat
"""

import json
import os
from contextlib import AsyncExitStack

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool as MCPTool
from openai import AsyncOpenAI
from pydantic import BaseModel

load_dotenv()

# Same client construction as client.py: AsyncOpenAI() reads OPENAI_API_KEY /
# OPENAI_BASE_URL from the environment itself, so this works with Mistral,
# OpenRouter, OpenAI, or any other OpenAI-compatible endpoint - whatever
# .env already points client.py at.
openai_client = AsyncOpenAI()
CHAT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

server_params = StdioServerParameters(
    command="uv",
    args=["run", "server.py"],
)

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools for reading and "
    "summarizing a user's Komoot GPX activity data. Use the tools to ground "
    "your answers in the actual data instead of guessing. For any question "
    "involving numbers - distance, elevation, pace, totals, or trends over "
    "time - call get_activity_stats or get_stats_summary rather than "
    "estimating from descriptions; both compute figures directly from GPS "
    "track points."
)

MAX_TOOL_ROUNDS = 8


def _mcp_tool_to_openai(tool: MCPTool) -> dict:
    """Convert an mcp.types.Tool into an OpenAI/OpenRouter tool-call schema."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema or {"type": "object", "properties": {}},
        },
    }


class MCPChatSession:
    """Owns one long-lived MCP stdio connection to server.py and drives the
    OpenAI/OpenRouter tool-use loop against it.
    """

    def __init__(self):
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self._tools: list[dict] = []

    async def start(self):
        if self.session is not None:
            return
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(server_params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self._tools = [_mcp_tool_to_openai(t) for t in listed.tools]

    async def stop(self):
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self.session = None
        self._tools = []

    async def _call_tool(self, name: str, arguments: dict) -> str:
        result = await self.session.call_tool(name=name, arguments=arguments)
        text = "\n".join(block.text for block in result.content if block.type == "text")
        if result.is_error:
            return f"Error calling tool {name}: {text}"
        return text

    async def chat(self, message: str) -> str:
        if self.session is None:
            await self.start()

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            response = await openai_client.chat.completions.create(
                model=CHAT_MODEL,
                messages=messages,
                tools=self._tools or None,
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )

            for tc in msg.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                tool_result = await self._call_tool(tc.function.name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )

        return "Sorry, I couldn't finish that request after several tool calls."


# Single shared MCP connection reused across chat() calls / requests, started
# lazily on first use (or eagerly via the FastAPI app's startup hook below).
_default_session = MCPChatSession()


async def chat(message: str) -> str:
    """Send a user message through the OpenRouter-backed LLM, letting it call
    the MCP tools exposed by server.py as needed, and return the final reply.
    """
    return await _default_session.chat(message)


router = APIRouter()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest) -> ChatResponse:
    reply = await chat(request.message)
    return ChatResponse(reply=reply)


app = FastAPI(title="chatgpx chat backend")
app.include_router(router)


@app.on_event("startup")
async def _startup():
    await _default_session.start()


@app.on_event("shutdown")
async def _shutdown():
    await _default_session.stop()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
