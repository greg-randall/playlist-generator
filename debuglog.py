"""Server-side debug record for the browser console.

Everything the server does while working is appended here as small dicts, and the web
app hands them to the page (see ``/api/events`` and ``/api/debug`` in app.py), which
prints them in the browser's devtools console. It records:

* every outbound HTTP call made through ``requests`` (so Spotify, Genius, LRCLIB,
  ReccoBeats, GetSongBPM and Deezer are all covered): method, URL, status, time, size,
  the top-level keys of a JSON answer, and the full body of any error answer;
* every call into the lyric / tempo stores (the database), with time and result size;
* log messages from the libraries, and the app's own ``print`` output.

Secrets: URLs have secret-looking query values masked, request headers and bodies are
never recorded, and the body of a successful answer is never recorded (only its keys),
because token answers contain the token.

``begin_run()`` starts a fresh record for a run; the ``generation`` number tells a page
that its position (``cursor``) in the old record no longer means anything.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_lock = threading.Lock()
_records: List[dict] = []
_generation = 0
_started = time.time()

_SECRET_NAME = re.compile(r"token|key|secret|code|password|auth|sig|session", re.IGNORECASE)
_ERROR_BODY_LIMIT = 20000  # an error answer is small; past this we say so rather than cut silently


def record(category: str, message: str, **data: Any) -> None:
    """Append one debug record. Safe to call from any thread."""
    entry = {"t": time.time(), "cat": category, "msg": message}
    entry.update(data)
    with _lock:
        _records.append(entry)


def begin_run() -> int:
    """Start a fresh record for a new run; returns the new generation number."""
    global _generation
    with _lock:
        _records.clear()
        _generation += 1
        return _generation


def since(cursor: int, generation: Optional[int] = None) -> Tuple[List[dict], int, int]:
    """``(records after cursor, new cursor, generation)``.

    If the caller's generation is not the current one, its cursor is for a record that
    no longer exists, so it gets everything from the start of the current one.
    """
    with _lock:
        if generation is not None and generation != _generation:
            cursor = 0
        return list(_records[cursor:]), len(_records), _generation


def redact_url(url: str) -> str:
    """The URL with secret-looking query values (and any user:password@) masked."""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        query = urlencode([(k, "***" if _SECRET_NAME.search(k) else v)
                           for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
    except Exception:  # noqa: BLE001 -- never let logging break a request
        return "(unparseable url)"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

_http_installed = False


def install_http_logging() -> None:
    """Record every ``requests`` call (idempotent). Covers every library built on it."""
    global _http_installed
    if _http_installed:
        return
    import requests

    original = requests.Session.request

    def logged_request(self, method, url, *args, **kwargs):
        shown = redact_url(str(url))
        params = kwargs.get("params")
        if params:
            try:
                shown += ("&" if "?" in shown else "?") + urlencode(
                    [(k, "***" if _SECRET_NAME.search(str(k)) else v)
                     for k, v in (params.items() if hasattr(params, "items") else params)])
            except Exception:  # noqa: BLE001
                pass
        started = time.perf_counter()
        try:
            response = original(self, method, url, *args, **kwargs)
        except Exception as exc:
            record("http", "{} {} -> {}".format(str(method).upper(), shown, type(exc).__name__),
                   method=str(method).upper(), url=shown, ok=False, error=type(exc).__name__,
                   detail=str(exc), ms=round((time.perf_counter() - started) * 1000))
            raise
        elapsed = round((time.perf_counter() - started) * 1000)
        entry = {"method": str(method).upper(), "url": shown, "status": response.status_code,
                 "ok": response.ok, "ms": elapsed,
                 "content_type": response.headers.get("content-type"),
                 "bytes": len(response.content) if not kwargs.get("stream") else None,
                 "from_cache": getattr(response, "from_cache", None)}
        if response.ok:
            try:
                body = response.json()
                if isinstance(body, dict):
                    entry["json_keys"] = list(body.keys())
                elif isinstance(body, list):
                    entry["json_items"] = len(body)
            except ValueError:
                pass
        elif not kwargs.get("stream"):
            text = response.text
            entry["body"] = text[:_ERROR_BODY_LIMIT]
            entry["body_truncated"] = len(text) > _ERROR_BODY_LIMIT
        record("http", "{} {} -> {} in {}ms".format(entry["method"], shown, response.status_code, elapsed),
               **entry)
        return response

    requests.Session.request = logged_request
    _http_installed = True


# --------------------------------------------------------------------------- #
# Stores (the database)
# --------------------------------------------------------------------------- #

def _summary(value: Any) -> str:
    """Short, readable description of an argument or result (never the whole lyrics)."""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        return repr(value) if len(value) <= 120 else "str of {} chars".format(len(value))
    try:
        return "{} of {}".format(type(value).__name__, len(value))
    except TypeError:
        return type(value).__name__


class LoggedStore:
    """Wraps a lyric or tempo store and records each call (name, arguments, time, result size)."""

    def __init__(self, store: Any, label: str):
        self._store = store
        self._label = label

    def __getattr__(self, name: str):
        attribute = getattr(self._store, name)
        if not callable(attribute) or name.startswith("_"):
            return attribute

        def call(*args, **kwargs):
            started = time.perf_counter()
            try:
                result = attribute(*args, **kwargs)
            except Exception as exc:
                record("db", "{}.{} raised {}".format(self._label, name, type(exc).__name__),
                       store=self._label, call=name, ok=False, error=type(exc).__name__,
                       detail=str(exc), ms=round((time.perf_counter() - started) * 1000))
                raise
            ms = round((time.perf_counter() - started) * 1000)
            record("db", "{}.{}({}) -> {} in {}ms".format(
                self._label, name, ", ".join(_summary(a) for a in args), _summary(result), ms),
                store=self._label, call=name, ms=ms, ok=True,
                args=[_summary(a) for a in args], result=_summary(result))
            return result

        return call


def logged(store: Any, label: str) -> Any:
    """``store`` wrapped for logging, or None if there is no store."""
    return None if store is None else LoggedStore(store, label)


# --------------------------------------------------------------------------- #
# Logging and print
# --------------------------------------------------------------------------- #

class _RecordHandler(logging.Handler):
    def emit(self, log_record: logging.LogRecord) -> None:
        try:
            record("log", "{} {}: {}".format(log_record.levelname, log_record.name, log_record.getMessage()),
                   level=log_record.levelname, logger=log_record.name)
        except Exception:  # noqa: BLE001
            pass


class _Tee:
    """Passes writes through to the real stream and records each complete line."""

    def __init__(self, stream):
        self._stream = stream
        self._partial = ""

    def write(self, text):
        written = self._stream.write(text)
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            if line.strip():
                record("print", line)
        return written

    def __getattr__(self, name):
        return getattr(self._stream, name)


_capture_installed = False


def install_output_capture() -> None:
    """Record library log messages and the app's own print output (idempotent)."""
    global _capture_installed
    if _capture_installed:
        return
    handler = _RecordHandler(level=logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    # INFO from these (not DEBUG: spotipy's DEBUG lines include authorization headers).
    for name in ("spotipy", "lyricsgenius"):
        logging.getLogger(name).setLevel(logging.INFO)
    sys.stdout = _Tee(sys.stdout)
    _capture_installed = True


def uptime_seconds() -> float:
    return round(time.time() - _started, 1)
