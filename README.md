# chatgpx

Chat about your own Komoot activities. You log in with your Komoot email and
password, the app pulls your tours as GPX files, and you can then chat with
an LLM (over OpenRouter) that has tool access to that GPX data via an MCP
server.

## How it fits together

- `server.py` — an MCP server exposing tools over your downloaded GPX
  activities (`list_activities`, `read_activity`, `summarize`,
  `generate_activity_video`) plus a `docs://traces` resource.
- `gpx_sync.py` — logs in to Komoot and downloads all tours as GPX files
  (`download_all_gpx(email, password, output_dir=...) -> list[Path]`),
  compatible with what `server.py` reads.
- `chat_backend.py` — connects to `server.py` over MCP stdio, exposes its
  tools to an LLM as function calls, and runs the conversation through
  OpenRouter's free tier (`chat(message: str) -> str`).
- `app.py` — the web app: serves the static frontend in `static/` and glues
  it to the two modules above via `POST /api/login` and `POST /api/chat`.
- `client.py` / `main.py` — a standalone MCP client demo, unrelated to the
  web app above.

This is a single-user demo: there's no real account system or database.
Sessions are an in-memory dict keyed by a cookie. Your Komoot password is
only held in memory for the duration of the login request and is never
logged or persisted by this app (see `gpx_sync.py`'s docstring for one
caveat inherited from the vendored `komootgpx` library itself).

## Prerequisite: the vendored `komootgpx` library

`gpx_sync.py` imports `komootgpx.api.KomootApi` from a vendored copy of
[fvalle1/komootgpx](https://github.com/fvalle1/komootgpx) at `./komootgpx/`.
That directory is gitignored (it also doubles as the GPX output directory,
so it fills up with your personal activity data) and is **not** included in
a fresh `git clone` of this repo. If `./komootgpx/` doesn't already contain
`api.py`, `gpxcompiler.py`, `utils.py`, etc., install the same library first:

```bash
uv run pip install --target komootgpx komootgpx==1.6.2
```

(or clone/copy github.com/fvalle1/komootgpx into `./komootgpx/` yourself).
Once that's done, `docker compose up --build` picks it up too, since Docker
only respects `.dockerignore`, not `.gitignore`.

## Configuration

Copy `.env.example` to `.env` and fill in at least `OPENROUTER_API_KEY`
(see that file for what each variable does and which are optional).

## Run with Docker (recommended)

```bash
cp .env.example .env   # then edit it
docker compose up --build
```

Open http://localhost:8000.

## Run without Docker

Requires [uv](https://docs.astral.sh/uv/) and Python 3.10+.

```bash
cp .env.example .env   # then edit it
uv sync
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000.

## Development notes

- `app.py` imports `gpx_sync` and `chat_backend` defensively: if either
  module isn't importable yet, the corresponding endpoint (`/api/login` or
  `/api/chat`) returns `503` instead of crashing the app, and a warning is
  logged at startup.
- `server.py` is spawned as a subprocess (over MCP stdio) by `chat_backend.py`
  the first time `/api/chat` is used (or eagerly at app startup); you don't
  run it separately.
