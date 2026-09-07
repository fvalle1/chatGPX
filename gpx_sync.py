"""Programmatic Komoot -> GPX sync, wrapping the vendored `komootgpx` library.

`komootgpx/komootgpx.py` is a CLI (interactive prompts, argparse, a
credentials.json cache, a hash cache for --skip-unchanged, etc.). This module
skips all of that and talks to `KomootApi` / `GpxCompiler` directly, so it can
be called from server-side code (e.g. a scheduled sync job) with nothing more
than an email and password.

Known limitation: importing the vendored `komootgpx` package runs its
`__init__.py`, which does `from .komootgpx import *` and therefore always
imports `komootgpx/komootgpx.py` as a side effect. That module computes a
`credentials.json` path under a platformdirs user cache dir at import time
(and would write a login *token*, not the raw password, there - but only if
its own CLI `main()`/`entrypoint()` is invoked). This module never calls that
entrypoint and never writes anything to that cache itself; it's called out
here because it's a property of the vendored library, not something we can
avoid short of forking it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from komootgpx.api import KomootApi
from komootgpx.gpxcompiler import GpxCompiler
from komootgpx.utils import parse_date_str, sanitize_filename

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "komootgpx"


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
    # compatible with what already exists in komootgpx/komoot(_run)/ and with
    # server.py's _parse_gpx_summary (reads gpx:metadata/gpx:name and
    # gpx:metadata/gpx:desc, both of which GpxCompiler.generate() writes).
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


def download_all_gpx(
    email: str,
    password: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    tour_type: str = "tour_all",
    language: str = "en",
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
            ``<repo>/komootgpx``, i.e. server.py's GPX_DIR.
        tour_type: "tour_all", "tour_planned", or "tour_recorded".
        language: Language for tour descriptions/highlights.

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
        written: list[Path] = []
        for tour_id in tours:
            tour = api.fetch_tour(str(tour_id), language=language)
            written.append(_write_tour_gpx(api, tour_id, tour, output_dir))
    except SystemExit:
        raise KomootApiError(
            "Komoot API request failed (see console output above for details)."
        ) from None

    return written
