import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer, Context
from mcp.types import SamplingMessage, TextContent

mcp = MCPServer(name="Komoot GPX Server")

GPX_DIR = Path(__file__).parent / "activities"
GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}

VIDEO_DIR = Path(__file__).parent / "generated_videos"
COSMOS_BASE_URL = os.environ.get("COSMOS_BASE_URL", "http://localhost:8000")


def _find_gpx(filename: str) -> Path:
    matches = list(GPX_DIR.rglob(filename))
    if not matches:
        raise ValueError(f"No such activity file: {filename}")
    return matches[0]


def _parse_gpx_summary(path: Path) -> dict:
    root = ET.parse(path).getroot()
    name_el = root.find("gpx:metadata/gpx:name", GPX_NS)
    desc_el = root.find("gpx:metadata/gpx:desc", GPX_NS)
    return {
        "file": path.name,
        "name": name_el.text if name_el is not None else path.stem,
        "description": desc_el.text if desc_el is not None else "",
    }


_ID_RE = re.compile(r"-(\d+)\.gpx$")


def _activity_id(path: Path) -> str:
    """The Komoot tour ID embedded in the filename, used to de-duplicate
    activities that were exported into more than one folder."""
    m = _ID_RE.search(path.name)
    return m.group(1) if m else path.stem


def _iter_unique_gpx() -> list[Path]:
    """All activity GPX files, de-duplicated by Komoot tour ID (the same
    activity can otherwise be exported into more than one folder)."""
    seen: dict[str, Path] = {}
    for path in sorted(GPX_DIR.rglob("*.gpx")):
        seen.setdefault(_activity_id(path), path)
    return list(seen.values())


def _parse_trackpoints(root) -> list[dict]:
    points = []
    for trkpt in root.findall(".//gpx:trkpt", GPX_NS):
        ele_el = trkpt.find("gpx:ele", GPX_NS)
        time_el = trkpt.find("gpx:time", GPX_NS)
        points.append({
            "lat": float(trkpt.get("lat")),
            "lon": float(trkpt.get("lon")),
            "ele": float(ele_el.text) if ele_el is not None else None,
            "time": (
                datetime.fromisoformat(time_el.text.replace("Z", "+00:00"))
                if time_el is not None else None
            ),
        })
    return points


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance between two points, in kilometers."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _compute_stats(points: list[dict]) -> dict:
    """Distance, elevation gain/loss, elapsed time and pace, computed
    directly from the track's GPS points rather than trusted from metadata."""
    if len(points) < 2:
        return {
            "distance_km": 0.0, "elevation_gain_m": 0.0, "elevation_loss_m": 0.0,
            "elapsed_time_min": None, "pace_min_per_km": None, "start_time": None,
        }

    distance_km = 0.0
    gain = loss = 0.0
    for prev, curr in zip(points, points[1:]):
        distance_km += _haversine_km(prev["lat"], prev["lon"], curr["lat"], curr["lon"])
        if prev["ele"] is not None and curr["ele"] is not None:
            delta = curr["ele"] - prev["ele"]
            if delta > 0:
                gain += delta
            else:
                loss += -delta

    start, end = points[0]["time"], points[-1]["time"]
    elapsed_min = (end - start).total_seconds() / 60 if start and end else None
    pace = elapsed_min / distance_km if elapsed_min and distance_km else None

    return {
        "distance_km": round(distance_km, 2),
        "elevation_gain_m": round(gain, 1),
        "elevation_loss_m": round(loss, 1),
        "elapsed_time_min": round(elapsed_min, 1) if elapsed_min else None,
        "pace_min_per_km": round(pace, 2) if pace else None,
        "start_time": start.isoformat() if start else None,
    }


def _parse_komoot_reported(description: str) -> dict:
    """Distance/duration/elevation as reported by Komoot itself in the GPX
    description, kept alongside our own computed figures as a cross-check."""
    reported = {}
    if not description:
        return reported
    patterns = {
        "distance_km": r"Distance:\s*([\d.]+)km",
        "elapsed_time_min": r"Estimated duration:\s*([\d.]+)h",
        "elevation_gain_m": r"Elevation up:\s*([\d.]+)m",
        "elevation_loss_m": r"Elevation down:\s*([\d.]+)m",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, description)
        if not m:
            continue
        value = float(m.group(1))
        if key == "elapsed_time_min":
            value *= 60
        reported[key] = round(value, 1)
    return reported


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
    """List all Komoot GPX activities (de-duplicated by tour ID) with name,
    description, distance and elevation."""
    return [_parse_gpx_summary(p) for p in _iter_unique_gpx()]


@mcp.tool()
def read_activity(filename: str) -> str:
    """Read the raw GPX contents of a single activity file by its filename."""
    return _find_gpx(filename).read_text()


@mcp.tool()
def generate_activity_video(filename: str, num_frames: int = 49, fps: int = 24) -> str:
    """Generate a brief text-to-video clip of a Komoot activity using a
    self-hosted Cosmos3-Nano vLLM-Omni server (COSMOS_BASE_URL, default
    http://localhost:8000), and save it under generated_videos/.
    """
    activity = _parse_gpx_summary(_find_gpx(filename))
    prompt = (
        f"A cinematic flyover of the route '{activity['name']}'. {activity['description']}"
    )

    response = httpx.post(
        f"{COSMOS_BASE_URL}/v1/videos/sync",
        json={"prompt": prompt, "num_frames": num_frames, "fps": fps},
        timeout=600,
    )
    response.raise_for_status()

    VIDEO_DIR.mkdir(exist_ok=True)
    output_path = VIDEO_DIR / f"{Path(filename).stem}.mp4"
    output_path.write_bytes(response.content)
    return str(output_path)


@mcp.tool()
def get_activity_stats(filename: str) -> dict:
    """Compute distance, elevation gain/loss, elapsed time and pace for one
    activity directly from its GPX track points, alongside Komoot's own
    reported numbers (from the file's description) as a correctness check."""
    path = _find_gpx(filename)
    summary = _parse_gpx_summary(path)
    points = _parse_trackpoints(ET.parse(path).getroot())
    return {
        "file": summary["file"],
        "name": summary["name"],
        "computed": _compute_stats(points),
        "komoot_reported": _parse_komoot_reported(summary["description"]),
    }


@mcp.tool()
def get_stats_summary() -> dict:
    """Aggregate totals and a per-month breakdown of distance, elevation
    gain and activity count across all Komoot activities, computed directly
    from GPX track points (de-duplicated by tour ID)."""
    totals = {"activities": 0, "distance_km": 0.0, "elevation_gain_m": 0.0, "elapsed_time_min": 0.0}
    by_month: dict[str, dict] = {}

    for path in _iter_unique_gpx():
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        stats = _compute_stats(_parse_trackpoints(root))
        if not stats["start_time"]:
            continue

        totals["activities"] += 1
        totals["distance_km"] += stats["distance_km"]
        totals["elevation_gain_m"] += stats["elevation_gain_m"]
        totals["elapsed_time_min"] += stats["elapsed_time_min"] or 0.0

        month = stats["start_time"][:7]  # YYYY-MM
        bucket = by_month.setdefault(
            month, {"activities": 0, "distance_km": 0.0, "elevation_gain_m": 0.0}
        )
        bucket["activities"] += 1
        bucket["distance_km"] += stats["distance_km"]
        bucket["elevation_gain_m"] += stats["elevation_gain_m"]

    totals = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in totals.items()}
    for bucket in by_month.values():
        bucket["distance_km"] = round(bucket["distance_km"], 1)
        bucket["elevation_gain_m"] = round(bucket["elevation_gain_m"], 1)

    return {"totals": totals, "by_month": dict(sorted(by_month.items()))}


@mcp.resource("docs://traces", mime_type="application/json")
def list_docs() -> list[str]:
    return [str(f) for f in _iter_unique_gpx()]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Komoot GPX MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help=(
            "stdio (default): spawned as a subprocess by a local client "
            "(client.py, chat_backend.py). streamable-http: run as a "
            "standalone HTTP server so external MCP clients/tools can "
            "connect over the network instead."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="streamable-http only")
    parser.add_argument("--port", type=int, default=8765, help="streamable-http only")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # Reachable at http://<host>:<port>/mcp by any MCP client that
        # speaks the streamable-http transport.
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
