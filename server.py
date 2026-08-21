import xml.etree.ElementTree as ET
from pathlib import Path

from mcp.server.mcpserver import MCPServer, Context
from mcp.types import SamplingMessage, TextContent

mcp = MCPServer(name="Komoot GPX Server")

GPX_DIR = Path(__file__).parent / "komootgpx"
GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _parse_gpx_summary(path: Path) -> dict:
    root = ET.parse(path).getroot()
    name_el = root.find("gpx:metadata/gpx:name", GPX_NS)
    desc_el = root.find("gpx:metadata/gpx:desc", GPX_NS)
    return {
        "file": path.name,
        "name": name_el.text if name_el is not None else path.stem,
        "description": desc_el.text if desc_el is not None else "",
    }


@mcp.tool()
async def list_roots(ctx: Context):
    """
    List all directories that are accessible to this server.
    These are the root directories where files can be read from or written to.
    """
    roots_result = await ctx.session.list_roots()
    client_roots = roots_result.roots
    return [Path(root.uri) for root in client_roots]


@mcp.tool()
async def summarize(text_to_summarize: str, ctx: Context):
    prompt = f"""
    Please summarize the descriptions of the following activities:
    {text_to_summarize}
    """
    await ctx.info("About to do summarise...")
    await ctx.report_progress(20, 100)

    result = await ctx.session.create_message(
        messages=[
            SamplingMessage(role="user", content=TextContent(type="text", text=prompt))
        ],
        max_tokens=4000,
        system_prompt="You are a helpful research assistant",
    )
    await ctx.report_progress(80, 100)

    if result.content.type == "text":
        return result.content.text
    else:
        raise ValueError("Sampling failed")


@mcp.tool()
def list_activities() -> list[dict]:
    """List all Komoot GPX activities with name, description, distance and elevation."""
    return [_parse_gpx_summary(p) for p in sorted(GPX_DIR.rglob("*.gpx"))]


@mcp.tool()
def read_activity(filename: str) -> str:
    """Read the raw GPX contents of a single activity file by its filename."""
    matches = list(GPX_DIR.rglob(filename))
    if not matches:
        raise ValueError(f"No such activity file: {filename}")
    return matches[0].read_text()


@mcp.resource("docs://traces", mime_type="application/json")
def list_docs() -> list[str]:
    return [str(f) for f in GPX_DIR.rglob("*.gpx")]


if __name__ == "__main__":
    mcp.run(transport="stdio")
