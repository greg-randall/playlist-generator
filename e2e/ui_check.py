"""Drives the real web app in a real browser (Playwright + the installed Chrome).

Unlike the unit tests, nothing here is faked: it clicks the actual page, which calls the
actual server, which calls the actual Spotify. It needs the server running and a saved
Spotify login (cache.txt) that covers the app's scopes.

    python3 e2e/ui_check.py                 # page loads, Load tracks, Check only (no side effects)
    python3 e2e/ui_check.py --create        # also Create playlist (makes a real Spotify playlist)
    python3 e2e/ui_check.py --search poland # also Search lyrics for a term (makes a real playlist)

Each step prints PASS or FAIL with the reason; a screenshot and the full browser console
are saved under e2e/out/ for any failure. Exit code is 1 if anything failed.
"""

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

HERE = Path(__file__).parent
OUT = HERE / "out"
CHROME = "/usr/bin/google-chrome"
SAMPLE_PLAYLIST = "https://open.spotify.com/playlist/17cChw6aXGTWXWI15Ffh9q"


def console_text(page) -> str:
    return page.inner_text("#console")


def wait_for_console(page, needle: str, seconds: float) -> None:
    """Wait until the on-page console contains ``needle``; stop early if the run reports an error."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        text = console_text(page)
        if needle in text:
            return
        if page.locator("#console .error").count():
            raise AssertionError("the run reported an error: " + page.locator("#console .error").first.inner_text())
        time.sleep(0.3)
    raise AssertionError("timed out after {}s waiting for {!r}; console says:\n{}".format(
        seconds, needle, console_text(page)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--playlist", default=SAMPLE_PLAYLIST, help="a playlist link that has at least one track")
    parser.add_argument("--create", action="store_true", help="also create a real playlist from one track")
    parser.add_argument("--search", metavar="TERM", help="also run a lyric search (creates a real playlist)")
    parser.add_argument("--screenshots", action="store_true",
                        help="save a screenshot of each failure (can hang where headless Chrome can't draw)")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()

    OUT.mkdir(exist_ok=True)
    failures = []
    messages = []

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=not args.headed,
                                    args=["--no-sandbox", "--disable-gpu"])
        page = browser.new_page()
        page.on("console", lambda m: messages.append("[{}] {}".format(m.type, m.text)))
        page.on("pageerror", lambda e: messages.append("[pageerror] {}".format(e)))
        page.on("response", lambda r: messages.append("[http {}] {} {}".format(
            r.status, r.request.method, r.url)) if r.url.startswith(args.url) and "/api/events" not in r.url
            else None)

        def click(selector):
            # force=True skips Playwright's "wait for a stable animation frame" check. Headless Chrome
            # under WSL produces frames erratically, which made ordinary clicks time out.
            page.click(selector, force=True, timeout=10000)

        def step(name, fn):
            started = time.time()
            try:
                fn()
                print("PASS  {} ({:.1f}s)".format(name, time.time() - started))
            except (AssertionError, PlaywrightTimeout) as exc:
                failures.append(name)
                detail = str(exc).replace("\n", "\n      ")
                print("FAIL  {} ({:.1f}s)\n      {}".format(name, time.time() - started, detail))
                try:
                    if not args.screenshots:
                        raise PlaywrightTimeout("skipped")
                    page.screenshot(path=str(OUT / "{}.png".format(name.replace(" ", "_"))), timeout=5000)
                except PlaywrightTimeout:
                    pass  # no screenshot (not asked for, or the browser couldn't draw one)

        def loads():
            page.goto(args.url)
            page.wait_for_selector("#start")
            time.sleep(1)
            errors = [m for m in messages if m.startswith("[pageerror]")]
            assert not errors, "page errors: {}".format(errors)
            assert any("page loaded" in m for m in messages), "the debug layer didn't announce itself"

        def load_tracks():
            click("#modeList")
            page.fill("#sourceLink", args.playlist)
            click("#loadList")
            wait_for_console(page, "Loaded", 30)
            box = page.input_value("#tracks")
            assert box.strip(), "the track box is empty after Load tracks"
            assert "Loaded 0 track" not in console_text(page), "loaded 0 tracks: " + console_text(page)

        def check_only():
            page.fill("#tracks", "poland - lil yachty")
            click("#checkList")
            wait_for_console(page, "1 of 1 found", 60)

        def create():
            page.fill("#listTitle", "e2e-check-{}".format(time.strftime("%H%M%S")))
            click("#createList")
            wait_for_console(page, "Done.", 60)
            assert "open.spotify.com/playlist/" in console_text(page) or page.locator("#playerWrap iframe").count(), \
                "no playlist link or player after Create"

        def search():
            click("#modeSearch")
            page.fill("#query", args.search)
            click("#start")
            wait_for_console(page, "Added:", 180)   # the first matching song went into the playlist
            click("#stop")                          # a full search pages through ~1000 results
            wait_for_console(page, "Stopped.", 60)

        step("page loads, no errors", loads)
        step("load tracks from a playlist", load_tracks)
        step("check only (nothing created)", check_only)
        if args.create:
            step("create playlist", create)
        if args.search:
            step("search lyrics: " + args.search, search)

        (OUT / "console.log").write_text("\n".join(messages) + "\n")
        browser.close()

    bad = [m for m in messages if m.startswith(("[error]", "[pageerror]"))
           and "favicon" not in m and "Failed to load resource" not in m]
    if bad:
        print("\nbrowser console errors seen:")
        for message in bad:
            print("   " + message[:300])
    print("\nfull browser console saved to e2e/out/console.log")
    print("RESULT: {}".format("FAILED: " + ", ".join(failures) if failures else "all steps passed"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
