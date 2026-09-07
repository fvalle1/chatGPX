import asyncio
import json
import os

import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.session import ClientRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CreateMessageRequestParams,
    CreateMessageResult,
    SamplingMessage,
    TextContent,
    LoggingMessageNotificationParams,
)
from openai import AsyncOpenAI

load_dotenv()
openai_client = AsyncOpenAI()  # reads OPENAI_API_KEY / OPENAI_BASE_URL from env
model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
server_params = StdioServerParameters(
    command="uv",
    args=["run", "server.py"],
)


async def chat(
    input_messages: list[SamplingMessage],
    max_tokens=4000,
    system_prompt: str | None = None,
):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for msg in input_messages:
        if msg.content.type == "text":
            messages.append({"role": msg.role, "content": msg.content.text})
    response = await openai_client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


async def logging_callback(params: LoggingMessageNotificationParams):
    print(params.data)


async def print_progress_callback(
    progress: float, total: float | None, message: str | None
):
    if total is not None:
        percentage = (progress / total) * 100
        print(f"Progress: {progress}/{total} ({percentage:.1f}%)")
    else:
        print(f"Progress: {progress}")


async def sampling_callback(
    context: ClientRequestContext, params: CreateMessageRequestParams
):
    text = await chat(
        params.messages,
        max_tokens=params.max_tokens,
        system_prompt=params.system_prompt,
    )
    return CreateMessageResult(
        role="assistant",
        model=model,
        content=TextContent(type="text", text=text),
    )


async def run():
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(
            read,
            write,
            sampling_callback=sampling_callback,
            logging_callback=print_progress_callback,
        ) as session:
            await session.initialize()

            activities = await session.call_tool(name="list_activities", arguments={})
            descriptions = "\n".join(
                json.loads(block.text)["description"]
                for block in activities.content
                if block.type == "text"
            )

            result = await session.call_tool(
                name="summarize",
                arguments={"text_to_summarize": descriptions},
            )
            print(result.content)

            first_file = json.loads(
                next(b.text for b in activities.content if b.type == "text")
            )["file"]
            try:
                video_result = await session.call_tool(
                    name="generate_activity_video",
                    arguments={"filename": first_file},
                )
                for block in video_result.content:
                    if block.type == "text":
                        print(block.text)
            except (MCPError, httpx.ConnectError) as e:
                print(f"generate_activity_video failed: {e}")


if __name__ == "__main__":
    asyncio.run(run())
