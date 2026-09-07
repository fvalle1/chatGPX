"""Programmatic Komoot -> GPX sync, wrapping the `komootgpx` library that
lives in the `KomootGPX/` git submodule.

`KomootGPX/komootgpx/komootgpx.py` is a CLI (interactive prompts, argparse, a
credentials.json cache, a hash cache for --skip-unchanged, etc.). This module
skips all of that and talks to `KomootApi` / `GpxCompiler` directly, so it can
be called from server-side code (e.g. a scheduled sync job) with nothing more
than an email and password.

The submodule isn't installed as a package, so `KomootGPX/` (not
`KomootGPX/komootgpx/`) is added to `sys.path` below, letting `import
komootgpx` resolve to `KomootGPX/komootgpx/` exactly as if it had been pip
installed.

Downloaded activity GPX files go in `./activities/` (server.py's GPX_DIR),
a plain data folder unrelated to the submodule above. Deliberately *not*
named anything close to "komootgpx"/"KomootGPX": this repo is developed on
a case-insensitive filesystem (default macOS), where those two names
collide as the same path - an earlier version of this file discovered that
the hard way, when `rm -rf ./komootgpx` silently deleted the `KomootGPX/`
submodule checkout too.

Known limitation: importing the `komootgpx` package runs its `__init__.py`,
which does `from .komootgpx import *` and therefore always imports
`komootgpx/komootgpx.py` as a side effect. That module computes a
`credentials.json` path under a platformdirs user cache dir at import time
(and would write a login *token*, not the raw password, there - but only if
its own CLI `main()`/`entrypoint()` is invoked). This module never calls that
entrypoint and never writes anything to that cache itself; it's called out
here because it's a property of the vendored library, not something we can
avoid short of forking it.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
_KOMOOTGPX_SUBMODULE = REPO_ROOT / "KomootGPX"
if _KOMOOTGPX_SUBMODULE.is_dir() and str(_KOMOOTGPX_SUBMODULE) not in sys.path:
    sys.path.insert(0, str(_KOMOOTGPX_SUBMODULE))

try:
    from komootgpx.api import KomootApi
    from komootgpx.gpxcompiler import GpxCompiler
    from komootgpx.utils import parse_date_str, sanitize_filename
except ImportError as exc:  # pragma: no cover - clearer error than a bare ModuleNotFoundError
    raise ImportError(
        "Could not import the komootgpx library from the KomootGPX/ "
        "submodule. Did you run `git submodule update --init` after "
        "cloning this repo?"
    ) from exc

DEFAULT_OUTPUT_DIR = REPO_ROOT / "activities"


class KomootAuthError(RuntimeError):
    """Raised when logging in to Komoot fails (bad credentials, network error)."""


class KomootApiError(RuntimeError):
    """Raised when a Komoot API request fails after a successful login."""


def _login(api: KomootApi, email: str, password: str) -> None:
    # KomootApi.login() calls sys.exit(1) on a non-200 response instead of
    # raising (it's written for a CLI), so translate that into a normal,
    # catchable exception here. Never include the password/token below.
    try:
        api.login(email, password)
    except SystemExit:
        raise KomootAuthError(
            "Komoot login failed: check the email/password "
            "(see console output above for the underlying API error)."
        ) from None
    except Exception as exc:  # network errors, malformed response, etc.
        raise KomootAuthError(f"Komoot login failed: {exc}") from exc


def _write_tour_gpx(api: KomootApi, tour_id: Any, tour: dict, output_dir: Path) -> Path:
    # Mirrors komootgpx.py:make_gpx's default naming/content so files stay
    # compatible with server.py's _parse_gpx_summary (reads
    # gpx:metadata/gpx:name and gpx:metadata/gpx:desc, both of which
    # GpxCompiler.generate() writes).
    title = sanitize_filename(tour["name"])
    filename = sanitize_filename(f"{title}-{tour_id}.gpx")
    path = output_dir / filename

    gpx = GpxCompiler(tour, api)
    path.write_text(gpx.generate(), encoding="utf-8")

    changed_at = tour.get("changed_at")
    if changed_at:
        ts = parse_date_str(changed_at).timestamp()
        os.utime(path, (ts, ts))

    return path


def _parse_date(value: str | None) -> "datetime.date | None":
    """Parse a "YYYY-MM-DD" string (as sent by the web UI's date inputs)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}, expected YYYY-MM-DD") from exc


def _tour_date(tour: dict) -> "datetime.date | None":
    raw = tour.get("date") or tour.get("changed_at")
    if not raw:
        return None
    return datetime.strptime(raw[:10], "%Y-%m-%d").date()


def _filter_tours(
    tours: dict[Any, dict],
    start_date: str | None,
    end_date: str | None,
    sport: str | None,
) -> dict[Any, dict]:
    """Mirrors komootgpx.py's own date_filter/sport_filter (client-side
    filters applied after KomootApi.fetch_tours(), which only filters by
    tour_type server-side)."""
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if not start and not end and not sport:
        return tours

    filtered = {}
    for tour_id, tour in tours.items():
        if sport and tour.get("sport") != sport:
            continue
        tour_date = _tour_date(tour)
        if start and tour_date and tour_date < start:
            continue
        if end and tour_date and tour_date > end:
            continue
        filtered[tour_id] = tour
    return filtered


def download_all_gpx(
    email: str,
    password: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    tour_type: str = "tour_all",
    language: str = "en",
    start_date: str | None = None,
    end_date: str | None = None,
    sport: str | None = None,
) -> list[Path]:
    """Log in to Komoot and download every tour of the account as GPX files.

    Writes one ``.gpx`` file per tour into `output_dir` (created if missing),
    using the "{title}-{id}.gpx" naming and gpx:metadata name/desc layout
    that komootgpx.py's CLI produces.

    Args:
        email: Komoot account email.
        password: Komoot account password. Not persisted anywhere by this
            function (see module docstring for the one caveat in the
            vendored library's own CLI code path, which this function does
            not use).
        output_dir: Directory to write .gpx files into. Defaults to
            ``<repo>/activities``, i.e. server.py's GPX_DIR.
        tour_type: "tour_all", "tour_planned", or "tour_recorded".
        language: Language for tour descriptions/highlights.
        start_date: Only tours on or after this date ("YYYY-MM-DD"), if set.
        end_date: Only tours on or before this date ("YYYY-MM-DD"), if set.
        sport: Only tours with this exact Komoot sport type (e.g. "hike",
            "running"), if set.

    Returns:
        Paths of every GPX file written.

    Raises:
        KomootAuthError: Login failed (bad credentials or network error).
        KomootApiError: A request after login failed (e.g. fetching tours).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    api = KomootApi()
    _login(api, email, password)

    try:
        tours = api.fetch_tours(tour_type=tour_type)
        tours = _filter_tours(tours, start_date, end_date, sport)
        written: list[Path] = []
        for tour_id in tours:
            tour = api.fetch_tour(str(tour_id), language=language)
            written.append(_write_tour_gpx(api, tour_id, tour, output_dir))
    except SystemExit:
        raise KomootApiError(
            "Komoot API request failed (see console output above for details)."
        ) from None

    return written
