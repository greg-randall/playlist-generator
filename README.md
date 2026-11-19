# playlist-generator

Ever wanted a themed playlist for every situation in life? Give it a word, and it
finds songs whose lyrics use that word a lot, then builds a Spotify playlist out of
them. It can also turn a plain list of songs into a playlist, and put a playlist in
an order that flows by tempo and key.

Everything runs from a small web app on your own computer.

## Getting started

### 1. Install

You need Python 3.9 or newer. From this folder:

    pip3 install -r requirements.txt

### 2. Get your keys

The app reads its keys from environment variables. Put them in your shell profile
(`~/.zshrc` on a Mac) and open a new terminal afterwards.

**Spotify (required).** At the [Spotify developer dashboard](https://developer.spotify.com/dashboard),
create an app and add `http://127.0.0.1:8888/callback` as a redirect URI. Then:

    export SPOTIPY_CLIENT_ID=...
    export SPOTIPY_CLIENT_SECRET=...
    export SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback

The first time the app needs Spotify, it opens the login in a new browser tab from the
page itself. While the app runs it keeps a small listener on the redirect URI's port
(8888 here), so the tab ends on a "Logged in to Spotify" page you can close. If the
app is itself served on that port (set `APP_PORT` if it isn't 8000), its own
`/callback` route answers instead.

**Genius (required).** At [genius.com/api-clients](https://genius.com/api-clients),
create an API client and click **Generate Access Token**. Use that *client access
token*, not the client secret:

    export GENIUS_TOKEN=...

**A database (optional, recommended).** With one, every song's lyrics and every
tempo lookup is saved, so nothing is fetched twice and you can search the lyrics
you've already collected instantly. A free [Neon](https://neon.tech) database is
plenty; [SETUP_DATABASE.md](SETUP_DATABASE.md) walks through it.

    export DATABASE_URL=postgresql://...

**GetSongBPM (optional).** A free key from [getsongbpm.com/api](https://getsongbpm.com/api)
adds a second tempo and key source when reordering.

    export GETSONGBPM_API_KEY=...

### 3. Start the app

    python3 -m uvicorn app:app --port 8000

Then open **http://127.0.0.1:8000**. The first time the app talks to Spotify, it opens a
login tab for you and remembers the login after that. It asks for permission to read and
change your playlists (public and private); if the permissions the app needs ever change,
you'll be asked to log in once more.

## Using the app

The app has two tabs. Both share the console (live progress, one line per song),
the **Stop** button, and a Spotify player showing the playlist being built. The player
appears a few seconds after the first song is added, because a brand-new playlist isn't
ready to play at once, and it only reloads at the end of a run if songs were added after
it loaded (a reload would cut off a song that is playing).

**Playlists are private.** The app asks Spotify for public playlists, but Spotify makes
the ones it creates private. In testing, a private playlist still played in the page's
player (with Spotify logged in in that browser), but Spotify's public embed can't see it.
To share one, choose **Make public** in Spotify's menu for it.

### Search lyrics

Builds a playlist from songs whose lyrics use your word.

![The Search lyrics tab: search terms, playlist title, threshold, and where to look for songs](docs/screenshots/search-lyrics.jpg)

- **Search terms** -- one word or phrase, or several separated by commas
  (`kristen, kristin`). A song counts if all the terms together make up enough of
  its lyrics.
- **Playlist title** -- defaults to the search term. If you already have a public
  playlist with that name, songs are added to it, and songs already in it are skipped.
  *Known issue:* because Spotify reports the playlists this app creates as private, a
  repeat search may create a new playlist instead of adding to the earlier one.
- **Threshold %** -- how much of the lyrics (by characters) the terms must make up.
  The default of 3% means the word really is a theme of the song, not a passing
  mention.
- **Discover with** -- where to look for songs:
  - **Saved lyrics**: every song whose lyrics are already in your database. Instant,
    and makes no lyric lookups. Needs a database.
  - **Spotify search**: songs with the word in the *title*. Spotify now allows only 10
    results per request, so this walks through the results in many small steps.
  - **Genius search**: songs with the word in the *lyrics*. Usually the best source,
    especially for unusual words.

  For a rare word, unchecking Spotify search saves time: title matches rarely use
  the word enough in their lyrics.

Songs are added to the playlist as they're found, so **Stop** keeps everything
found so far. Each song's line in the console says what happened:

| Console says | Meaning |
|---|---|
| `MATCH` | Clears the threshold, added to the playlist |
| `below` | Has lyrics, but uses the word too little |
| `not found` | No lyrics source has this song |
| `instrumental` | The song has no lyrics |
| `no lyrics` | The song exists but has no lyrics posted |
| `couldn't check` | A lookup failed; it's tried again next time |

At the end, any matches that couldn't be found on Spotify are listed, so nothing
disappears silently.

### Track list

Turns a list of songs into a playlist, and can reorder it first.

![The Track list tab after Reorder by tempo + key, asking which tempo to use where two sources disagree](docs/screenshots/track-list-reorder.jpg)

- **Tracks** -- one per line, as `Title - Artist`. `Artist - Title`, numbering and
  bullets work too.
- **Start from an existing playlist** -- paste a Spotify playlist link and click
  **Load tracks** to fill the box with its tracks, in order. Rearrange them however
  you like.
- **Create playlist** -- makes a new playlist with the title you give, in the
  box's order.
- **Check only** -- looks every track up on Spotify and reports what it can't
  find, without creating anything.
- **Reorder by tempo + key** -- rearranges the box so the playlist flows: it starts
  from the slowest track and steps through nearby tempos and keys. It changes only
  the box, nothing on Spotify. Tracks whose tempo can't be found go to a
  "place these by hand" list at the bottom. When two sources disagree on a tempo, you
  can pick one or type your own. Tracks with no known key get a box too: type the
  key however you have it (`8A`, `A minor`, `Am`, `C#m`). Then **Reorder again**.
  Everything you type is remembered in your browser. **Undo reorder** puts the
  box back.

The first reorder of a playlist takes a few minutes (about a second per track, plus
a few seconds for each track measured from its preview clip). With a database,
every reorder after that is nearly instant.

## Where the data comes from

**Lyrics** come from Genius. When Genius doesn't have a song, or won't answer,
LRCLIB is used instead. As of November 2026, Genius refuses this app's lyric
requests, so lyrics currently come from LRCLIB. LRCLIB has about three in four of
the songs Genius knows, and fewer obscure ones. Genius search still works for
finding songs, and the app goes back to Genius lyrics on its own if they're
available again.

**Tempo and key** used to come from Spotify, which no longer provides them to
apps like this one. They now come from ReccoBeats and GetSongBPM, cross-checked
against each other. For tracks neither knows, the tempo is measured from the
song's 30-second preview clip on Deezer (tempo only: key from a short clip is
too unreliable).

* [Spotify](https://developer.spotify.com/documentation/web-api) - song search and playlists
* [Genius](https://genius.com) - lyrics and lyric search
* [LRCLIB](https://lrclib.net) - lyrics, the backup when Genius has none or won't serve them
* [ReccoBeats](https://reccobeats.com) - tempo (BPM) and musical key
* [GetSongBPM](https://getsongbpm.com) - tempo (BPM) and musical key
* [Deezer](https://developers.deezer.com) - 30-second preview clips, measured with [librosa](https://librosa.org) for tracks neither tempo service knows

## Command line

Everything in the app also works from a terminal, which is handy for long runs.

    # Lyric search: "hotdog" playlist from songs whose lyrics say hotdog
    python3 generate_playlist.py -q hotdog -t "hotdog playlist"

    # Several terms, a stricter threshold, Genius search only
    python3 generate_playlist.py -q kristen -m kristin -bt 4 --spotify False

    # A track list file (one "Title - Artist" per line) to a new playlist
    python3 tracklist.py songs.txt -t "Bike ride"

    # Check a list without creating anything
    python3 tracklist.py songs.txt --dry-run

    # Print a playlist in a tempo + key order (changes nothing)
    python3 reorder.py "https://open.spotify.com/playlist/..."

    # Each track's BPM and Camelot key, in the playlist's current order
    python3 reorder.py "https://open.spotify.com/playlist/..." --table

Add `-h` to any of them for every option.

## Tests

    python3 -m unittest discover -s tests

The 270 tests never contact Spotify, Genius, or any other service (they use stand-ins),
so running them can't use up rate limits or get you blocked. (`tests/__init__.py` is
empty on purpose: without it, another package named `tests` installed in your Python can
shadow this folder and the run fails with `No module named 'tests...'`.)

### Checking the real app in a real browser

The tests above can't notice when a service changes (Spotify has retired endpoints,
tightened limits and changed answer shapes). `e2e/ui_check.py` drives the running app in
Chrome with [Playwright](https://playwright.dev/python/), against the real services:

    pip3 install playwright
    python3 e2e/ui_check.py                  # page loads, Load tracks, Check only (creates nothing)
    python3 e2e/ui_check.py --create         # also Create playlist; checks the player loads once
    python3 e2e/ui_check.py --search poland  # also a real lyric search, then Stop

Start the app and log in to Spotify once first. `--create` and `--search` make real
playlists in your account. Each step prints PASS or FAIL, and the browser console is
saved to `e2e/out/console.log`. It uses Google Chrome at `/usr/bin/google-chrome` (change
`CHROME` at the top of the script if yours is elsewhere). Other options: `--headed` shows
the window, `--screenshots` saves a picture of each failure (it can hang where headless
Chrome can't draw, so it's off by default).

## Debugging in the browser

Open the browser's developer tools (F12, then **Console**). Everything the app does is
logged there:

- every request the page makes, with what it sent and what came back;
- what the server does for each one: every call to Spotify, Genius, LRCLIB, ReccoBeats,
  GetSongBPM and Deezer (method, URL, status, time, and the full body of an error), every
  database call, library log messages, and the lines the app prints;
- every run event, click and field change, and anything saved in your browser;
- a summary when a run ends: events by kind, outbound calls by host, and the slowest ones.

In the console, `debugState()` shows the page's state, `runDebug` holds the current run's
events and server records, and `debugVerbose = true` also logs each poll. Secret-looking
values in URLs are masked, and request headers, request bodies and successful answers'
contents are never recorded.

## More

- [SETUP_DATABASE.md](SETUP_DATABASE.md) -- setting up the free database
- [CHANGELOG.md](CHANGELOG.md) -- what's changed, with measurements
- [ROADMAP.md](ROADMAP.md) -- ideas for what's next
