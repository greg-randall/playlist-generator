"""Local web UI for the playlist generator (v1).

Runs the pipeline in-process on a background thread, streams its progress events
to the browser (polled via /api/events), embeds a Spotify player for the
resulting playlist, and exposes a Stop button.

Designed as the on-ramp to a hosted version: the pieces that would change when
hosting (who Spotify authenticates as, and how the run executes) are isolated
behind `Manager.executor` and the client factories, so hosting swaps those
without touching the console/player/stop plumbing.

Run it:
    uvicorn app:app --reload --port 8000
    # then open http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import debuglog
import lrclib
import tempo_store
import reorder
import tracklist

HERE = Path(__file__).parent


# --------------------------------------------------------------------------- #
# Run state
# --------------------------------------------------------------------------- #

class RunParams(BaseModel):
    query: str
    title: Optional[str] = None
    threshold: float = 3.0
    saved: bool = True      # search the lyric cache first (no lyric requests)
    spotify: bool = True    # Spotify title search as the discovery engine
    genius: bool = True     # Genius as a discovery engine; lyrics always use Genius
    matches: Optional[List[str]] = None


class TracklistParams(BaseModel):
    text: str                     # one track per line, "Title - Artist"
    title: Optional[str] = None   # name of the playlist to create
    dry_run: bool = False         # look the tracks up but create nothing
    known: Optional[Dict[str, str]] = None   # line -> track id, from a loaded playlist


class ReorderParams(BaseModel):
    text: str                                # one track per line, "Title - Artist"
    known: Optional[Dict[str, str]] = None   # line -> track id, from a loaded playlist
    tempos: Optional[Dict[str, float]] = None   # line -> BPM the user typed in
    keys: Optional[Dict[str, str]] = None       # line -> key the user typed in ("8A", "A minor"...)


class PlaylistLink(BaseModel):
    link: str


class Run:
    """A single playlist-building run and its collected progress events."""

    def __init__(self, run_id: str, params: BaseModel):
        self.id = run_id
        self.params = params
        self.events: List[dict] = []
        self.status = "running"  # running | complete | stopped | error
        self.playlist_id: Optional[str] = None
        self.stop_flag = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def emit(self, event: dict) -> None:
        """on_event sink: append a structured event (thread-safe)."""
        with self._lock:
            self.events.append(event)

    def should_stop(self) -> bool:
        return self.stop_flag.is_set()

    def slice(self, cursor: int) -> List[dict]:
        with self._lock:
            return self.events[cursor:]


def default_executor(run: Run) -> None:
    """Real execution: build clients, resolve the playlist, run the engines.

    Imported lazily so the web app (and its tests) load without spotipy/genius.
    """
    from generate_playlist import (
        PlaylistBuilder, assemble_terms, build_genius_client, resolve_playlist,
    )
    from lyric_store import build_lyric_store

    p = run.params
    searches = assemble_terms(p.query, p.matches)  # comma-separated query supported
    sp = _spotify_client(run)
    genius = build_genius_client()
    store = debuglog.logged(build_lyric_store(), "lyrics_db")  # Postgres if DATABASE_URL is set, else None

    builder = PlaylistBuilder(
        sp=sp, genius=genius, searches=searches, threshold=p.threshold,
        on_event=run.emit, stop_flag=run.stop_flag, lyric_store=store,
        lyrics_fallback=lrclib.lookup_lyrics,
    )
    args = SimpleNamespace(playlistId=None, title=p.title, query=p.query)
    builder.playlist_id = resolve_playlist(sp, builder, args)
    run.playlist_id = builder.playlist_id
    # track_count reflects songs already in the playlist (seeded) -> lets the UI
    # mount the player immediately when re-running against a non-empty playlist.
    run.emit({"kind": "playlist", "message": "Playlist ready.",
              "playlist_id": builder.playlist_id, "track_count": len(builder.track_ids)})

    if p.saved:
        builder.from_saved_lyrics()   # all terms at once; no lyric requests
    # Discover candidates for every term; matching still uses the whole set.
    for term in searches:
        if p.spotify:
            builder.from_spotify(term)
        if p.genius:
            builder.from_genius(term)
    builder.report_misses()


# A Spotify login in progress: the run thread waits on ``done`` while Spotify's
# redirect arrives at this app's own /callback route (see spotify_callback).
_login = {"done": threading.Event(), "waiting": False, "code": None, "error": None}
LOGIN_TIMEOUT_SECONDS = 300
_login_listener_up = False  # set at startup; see _start_login_listener


def _redirect_port() -> Optional[int]:
    """Port of SPOTIPY_REDIRECT_URI when it is a loopback http .../callback, else None."""
    uri = urlparse(os.environ.get("SPOTIPY_REDIRECT_URI", ""))
    try:
        if uri.scheme == "http" and uri.hostname in ("127.0.0.1", "localhost") \
                and uri.path == "/callback":
            return uri.port
    except ValueError:
        pass
    return None


def _start_login_listener() -> bool:
    """Keep a small server on the redirect URI's port for as long as the app runs.

    Spotify sends the browser to that address after login. spotipy's own listener
    answers one request and then closes, leaving "can't reach this page"; this one
    stays up and shows "Logged in to Spotify". Returns True when something this app
    controls will answer there (the listener, or the app itself on its own port).
    """
    import time

    import uvicorn

    port = _redirect_port()
    if port is None:
        return False
    if port == int(os.environ.get("APP_PORT", "8000")):
        return True  # the main app already serves /callback on this port
    callback_app = FastAPI()
    callback_app.add_api_route("/callback", spotify_callback, response_class=HTMLResponse)
    server = uvicorn.Server(uvicorn.Config(callback_app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(40):  # up to ~2s to bind; a taken port leaves started False
        if server.started:
            return True
        time.sleep(0.05)
    return False


def _wait_for_login_code() -> str:
    """Block the run until /callback receives Spotify's code; raise if it never does."""
    try:
        if not _login["done"].wait(LOGIN_TIMEOUT_SECONDS):
            raise TimeoutError("The Spotify login wasn't finished within {} minutes.".format(
                LOGIN_TIMEOUT_SECONDS // 60))
    finally:
        _login["waiting"] = False
    if _login["error"] or not _login["code"]:
        raise RuntimeError("Spotify login failed: {}".format(_login["error"] or "no code returned"))
    return _login["code"]


def _spotify_client(run: Optional[Run] = None):
    """Spotify client for list runs; a seam so tests can swap in a fake.

    With a run, a needed Spotify login is sent to the page as an ``auth`` event
    (the page opens it) rather than left to spotipy's own browser launch.
    """
    from generate_playlist import build_spotify_client

    in_app = _login_listener_up

    def announce_login(url: str) -> None:
        if in_app:
            _login.update(done=threading.Event(), waiting=True, code=None, error=None)
        run.emit({"kind": "auth", "url": url,
                  "message": "Spotify needs you to log in. A new tab should open; if not, "
                             "open this link and approve: " + url})

    return build_spotify_client(on_auth_url=announce_login if run else None,
                                wait_for_code=_wait_for_login_code if run and in_app else None)


def tracklist_executor(run: Run) -> None:
    """Look up a pasted track list and write it to a new playlist, in order."""
    p = run.params

    def emit(kind: str, message: str, **data) -> None:
        run.emit({"kind": kind, "message": message, **data})

    summary = tracklist.run(
        _spotify_client(run), p.text, title=p.title, dry_run=p.dry_run,
        emit=emit, should_stop=run.should_stop, known=p.known,
    )
    run.playlist_id = summary["playlist_id"]


# Every tempo answer so far (see reorder.lookup), so reordering again after
# typing in a missing tempo doesn't ask the services about every track twice.
_tempo_cache: dict = {}


def _tempo_store():
    """Saved tempo lookups (Postgres when DATABASE_URL is set); a seam tests replace."""
    return debuglog.logged(tempo_store.build_tempo_store(), "tempo_db")


def reorder_executor(run: Run) -> None:
    """Look a track list up, get tempos, and emit the list in its new order.

    Changes nothing on Spotify. The final "reorder" event carries the new text
    for the Track list box and the tracks that still need a tempo from the user.
    """
    p = run.params

    def emit(kind: str, message: str, **data) -> None:
        run.emit({"kind": kind, "message": message, **data})

    entries = tracklist.parse_tracklist(p.text)
    emit("search", "Looking up {} track(s) on Spotify...".format(len(entries)))
    resolved, skipped = tracklist.resolve_tracklist(
        _spotify_client(run), entries, emit=emit, should_stop=run.should_stop, known=p.known)
    if run.should_stop():
        return
    tracks = [{"line": label, "id": track_id} for track_id, label in resolved]

    api_key = os.environ.get("GETSONGBPM_API_KEY")
    store = _tempo_store()
    loaded = tempo_store.preload(store, tracks, _tempo_cache)
    emit("search", "\nGetting tempos (the first time takes about a second per track)...")
    try:
        recco, gsb = reorder.lookup(tracks, api_key, log=lambda message: emit("error", message),
                                    progress=lambda message: emit("search", message),
                                    should_stop=run.should_stop, cache=_tempo_cache)
        if run.should_stop():
            return
        clips = reorder.measure_missing(
            tracks, recco, gsb, log=lambda message: emit("error", message),
            progress=lambda message: emit("search", message),
            should_stop=run.should_stop, cache=_tempo_cache)
    finally:
        try:
            tempo_store.save_new(store, _tempo_cache, loaded)   # keep what was found, even if stopped
        except Exception as exc:  # noqa: BLE001 -- saving is a bonus; don't fail the reorder
            emit("error", "Couldn't save tempo lookups ({})".format(type(exc).__name__))
    if run.should_stop():
        return

    for line, text in (p.keys or {}).items():
        if text.strip() and reorder.parse_key(text) is None:
            emit("error", "Couldn't read the key '{}' for {} -- try 8A, A minor or Am.".format(
                text, line))
    ordered, unplaced = reorder.plan(tracks, recco, gsb, overrides=p.tempos, clips=clips,
                                     key_overrides=p.keys)
    reorder.report(ordered, unplaced, cross_checked=bool(api_key), paste=False,
                   log=lambda message: emit("tempo", message))

    lines = reorder.paste_lines(ordered, unplaced)
    not_found = [miss["line"] for miss in skipped if miss["reason"] != "duplicate"]
    if not_found:
        lines += ["# Not found on Spotify"] + not_found
    emit("reorder", "\nThe track list box now holds the new order.",
         text="\n".join(lines),
         known={track["line"]: track["id"] for track in tracks},
         needs=[{"line": e.line, "flag": e.flag, "readings": [round(r) for r in e.readings]}
                for e in ordered + unplaced if e.flag],
         no_key=[e.line for e in ordered + unplaced if e.key is None],
         typed_keys={e.line: reorder.camelot(e.key, e.mode) for e in ordered + unplaced
                     if e.key_yours})


def _run_thread(run: Run, executor) -> None:
    started = time.perf_counter()
    debuglog.record("app", "run started: {}".format(getattr(executor, "__name__", "executor")),
                    run_id=run.id, params=run.params.model_dump())
    try:
        executor(run)
        run.status = "stopped" if run.should_stop() else "complete"
        debuglog.record("app", "run {} in {:.1f}s".format(run.status, time.perf_counter() - started),
                        run_id=run.id, status=run.status, events=len(run.events))
        run.emit({"kind": "done", "message": "Stopped." if run.should_stop() else "Done.",
                  "status": run.status})
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the console
        run.status = "error"
        debuglog.record("app", "run failed after {:.1f}s: {}: {}".format(
            time.perf_counter() - started, type(exc).__name__, exc),
            run_id=run.id, error=type(exc).__name__, detail=str(exc),
            traceback=traceback.format_exc())
        run.emit({"kind": "error", "message": "Run failed: {}".format(exc)})
        run.emit({"kind": "done", "message": "Ended with error.", "status": "error"})


class Manager:
    """Owns the single active run (local, single-user)."""

    def __init__(self):
        self.run: Optional[Run] = None
        self.executor = default_executor  # swappable (tests, hosting)

    def start(self, params: BaseModel, executor=None) -> Run:
        if self.run is not None and self.run.status == "running":
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        run = Run(uuid.uuid4().hex, params)
        debuglog.begin_run()
        run.thread = threading.Thread(
            target=_run_thread, args=(run, executor or self.executor), daemon=True)
        self.run = run
        run.thread.start()
        return run

    def stop(self) -> bool:
        if self.run is None or self.run.status != "running":
            return False
        self.run.stop_flag.set()
        return True


manager = Manager()


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(title="Playlist Generator")
app.state.manager = manager


@app.on_event("startup")
def _open_login_listener() -> None:
    debuglog.install_http_logging()
    debuglog.install_output_capture()
    global _login_listener_up
    _login_listener_up = _start_login_listener()
    if not _login_listener_up and _redirect_port() is not None:
        print("Could not listen on port {} for the Spotify login redirect; falling back to "
              "spotipy's own one-shot listener.".format(_redirect_port()), flush=True)


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.post("/api/run")
def start_run(params: RunParams):
    run = manager.start(params)
    return {"run_id": run.id, "status": run.status}


@app.post("/api/tracklist")
def start_tracklist(params: TracklistParams):
    if not tracklist.parse_tracklist(params.text):
        raise HTTPException(status_code=400, detail="The track list is empty.")
    if not params.dry_run and not (params.title or "").strip():
        raise HTTPException(status_code=400, detail="Give the playlist a title.")
    run = manager.start(params, executor=tracklist_executor)
    return {"run_id": run.id, "status": run.status}


@app.post("/api/reorder")
def start_reorder(params: ReorderParams):
    if not tracklist.parse_tracklist(params.text):
        raise HTTPException(status_code=400, detail="The track list is empty.")
    run = manager.start(params, executor=reorder_executor)
    return {"run_id": run.id, "status": run.status}


@app.post("/api/playlist-tracks")
def playlist_tracks(params: PlaylistLink):
    """Read an existing playlist's tracks, in order, for the Track list box."""
    playlist_id = tracklist.parse_playlist_id(params.link)
    if playlist_id is None:
        raise HTTPException(status_code=400, detail="That doesn't look like a Spotify playlist link.")
    debuglog.record("app", "load playlist {}".format(playlist_id), playlist_id=playlist_id)
    try:
        name, tracks = tracklist.fetch_playlist(_spotify_client(), playlist_id)
        debuglog.record("app", "loaded {} track(s) from '{}'".format(len(tracks), name),
                        playlist_id=playlist_id, name=name, tracks=len(tracks))
    except Exception as exc:  # noqa: BLE001 -- report whatever Spotify said
        debuglog.record("app", "load playlist failed: {}: {}".format(type(exc).__name__, exc),
                        playlist_id=playlist_id, error=type(exc).__name__, detail=str(exc),
                        traceback=traceback.format_exc())
        raise HTTPException(
            status_code=502,
            detail="Spotify wouldn't return that playlist ({}). "
                   "Private playlists can't be read yet.".format(type(exc).__name__),
        )
    return {"playlist_id": playlist_id, "name": name, "tracks": tracks}


_CALLBACK_PAGE = """<!doctype html><meta charset="utf-8"><title>Spotify login</title>
<body style="font:18px/1.5 system-ui,sans-serif;max-width:32em;margin:15vh auto;padding:0 1em;
background:#121212;color:#eee"><h2>{heading}</h2><p>{body}</p>{script}</body>"""


@app.get("/callback", response_class=HTMLResponse)
def spotify_callback(code: Optional[str] = None, error: Optional[str] = None):
    """Where Spotify sends the browser after login (set SPOTIPY_REDIRECT_URI to this route).

    Hands the code to the run that is waiting for it, then shows a plain page. Served
    by the app itself, so unlike a one-shot listener it is always there to answer.
    """
    if not _login["waiting"]:
        return _CALLBACK_PAGE.format(
            heading="Nothing was waiting for a login",
            body="Go back to the app and start the run again. If Spotify asks you to log in again, "
                 "that is expected.", script="")
    _login.update(code=code, error=error)
    _login["done"].set()
    if error or not code:
        return _CALLBACK_PAGE.format(
            heading="Spotify login failed",
            body="Spotify said: {}. Go back to the app and try again.".format(
                (error or "no code returned").replace("<", "")), script="")
    return _CALLBACK_PAGE.format(
        heading="Logged in to Spotify",
        body="You can close this tab and go back to the app.",
        script="<script>setTimeout(function(){window.close()},1500)</script>")


@app.post("/api/stop")
def stop_run():
    return {"stopped": manager.stop()}


@app.get("/api/events")
def get_events(cursor: int = 0, debug_cursor: int = 0, generation: Optional[int] = None):
    """Run events after ``cursor``, plus the server's debug records after ``debug_cursor``."""
    debug, new_debug_cursor, current_generation = debuglog.since(debug_cursor, generation)
    run = manager.run
    if run is None:
        return {"events": [], "cursor": 0, "status": "idle", "playlist_id": None,
                "debug": debug, "debug_cursor": new_debug_cursor, "generation": current_generation}
    events = run.slice(cursor)
    return {
        "events": events,
        "cursor": cursor + len(events),
        "status": run.status,
        "playlist_id": run.playlist_id,
        "debug": debug,
        "debug_cursor": new_debug_cursor,
        "generation": current_generation,
    }


@app.get("/api/debug")
def get_debug(cursor: int = 0, generation: Optional[int] = None):
    """The server's debug records after ``cursor`` (for requests that aren't runs)."""
    records, new_cursor, current_generation = debuglog.since(cursor, generation)
    return {"debug": records, "debug_cursor": new_cursor, "generation": current_generation,
            "server_uptime_seconds": debuglog.uptime_seconds()}
