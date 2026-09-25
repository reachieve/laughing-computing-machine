"""
Shared logic used by both status_bot.py (Discord custom-status self-bot
version) and rpc_bot.py (Discord Rich Presence version): Spotify auth and
every lyric source (Vercel, Spicy Lyrics, LRCLIB, Musixmatch, Genius,
AZLyrics).

Keeping this in one place means fixes/improvements to lyric fetching only
need to happen once and both bots benefit.
"""

import requests
import os
import re
import random
import base64
import json
import spotipy
import cloudscraper
from spotipy.oauth2 import SpotifyOAuth
from bs4 import BeautifulSoup, Comment
from dotenv import load_dotenv

load_dotenv()

SPOTIFY_ID = os.environ.get("SPOTIFY_ID")
SPOTIFY_SECRET = os.environ.get("SPOTIFY_SECRET")
SPOTIFY_REDIRECT = os.environ.get("SPOTIFY_REDIRECT")
SCOPE = "user-read-currently-playing"

SPICY_LYRICS_TOKEN = os.environ.get("SPICY_LYRICS_TOKEN")

# Musixmatch and AZLyrics need no API key/token (Musixmatch's token is
# fetched automatically from a public unofficial endpoint; AZLyrics has no
# API at all). These just let you flip a source off without touching code.
MUSIXMATCH_ENABLED = os.environ.get("MUSIXMATCH_ENABLED", "true").strip().lower() != "false"
AZLYRICS_ENABLED = os.environ.get("AZLYRICS_ENABLED", "true").strip().lower() != "false"

# NetEase Cloud Music and QQ Music are Chinese platforms, but their
# catalogs include huge amounts of Western/international music with
# genuine synced (LRC) lyrics — both have public, unofficial, no-auth
# APIs that several well-known open-source lyric tools rely on.
NETEASE_ENABLED = os.environ.get("NETEASE_ENABLED", "true").strip().lower() != "false"
QQMUSIC_ENABLED = os.environ.get("QQMUSIC_ENABLED", "true").strip().lower() != "false"

# Optional: route the Genius/AZLyrics page scrapes through a proxy, e.g.
# "http://user:pass@host:port". Useful if your hosting IP gets blocked by
# Cloudflare's reputation checks. Leave unset to scrape directly.
SCRAPER_PROXY = os.environ.get("SCRAPER_PROXY")

# Optional: a whole pool of proxies to rotate through with automatic
# failover, given as "ip:port:user:pass" entries separated by commas
# (or newlines). If the first proxy tried is already flagged, the next
# request just tries the next one in the (randomized) list instead of
# giving up. SCRAPER_PROXY above, if also set, is added to this pool as
# one more option.
def _parse_proxy_pool(raw):
    proxies = []
    if not raw:
        return proxies
    for entry in re.split(r"[,\n]+", raw.strip()):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) == 4:
            ip, port, user, pwd = parts
            proxies.append(f"http://{user}:{pwd}@{ip}:{port}")
        elif len(parts) == 2:
            ip, port = parts
            proxies.append(f"http://{ip}:{port}")
        else:
            print("SCRAPER_PROXIES: skipped an entry that didn't match ip:port or ip:port:user:pass")
    return proxies


SCRAPER_PROXIES = _parse_proxy_pool(os.environ.get("SCRAPER_PROXIES", ""))
if SCRAPER_PROXY:
    SCRAPER_PROXIES.append(SCRAPER_PROXY)

token_info = None
auth_manager = None
musixmatch_token = None


def _scraper_get_with_proxies(url, headers, timeout=15, use_proxies=True):
    """
    GETs a URL through cloudscraper. If use_proxies is True, tries each
    configured proxy in turn (order randomized to spread load) until one
    returns a 200, falling back to a direct (no-proxy) attempt last. If
    use_proxies is False, only the direct attempt is made — useful for
    sites where proxies are known not to help (e.g. Genius, which blocks
    via a Cloudflare challenge type that's unrelated to IP reputation, so
    cycling through a whole proxy pool there just wastes time before
    giving up). Returns the successful response, or None if every
    attempt failed. Proxy credentials are never printed — only a
    "proxy N/M" label, to keep them out of logs.

    Explicitly closes the scraper session before returning. CloudScraper
    is a full requests.Session subclass holding its own open sockets —
    unlike a one-off requests.get() call (which closes itself via an
    internal context manager), a Session stays open until .close() is
    called or it's garbage collected, which isn't prompt in a long-running
    loop. Without this, every call here leaks a socket, and on a script
    that resyncs every 10 seconds for hours, those add up until the
    process hits its open-file limit ("Too many open files").
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

    try:
        if use_proxies:
            pool = list(SCRAPER_PROXIES)
            random.shuffle(pool)
            attempts = [(f"proxy {i + 1}/{len(pool)}", proxy_url) for i, proxy_url in enumerate(pool)]
            attempts.append(("direct (no proxy)", None))
        else:
            attempts = [("direct (no proxy)", None)]

        for label, proxy_url in attempts:
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            try:
                resp = scraper.get(url, headers=headers, timeout=timeout, proxies=proxies)
                print(f"  → {label}: status {resp.status_code}")
                if resp.status_code == 200:
                    # Force the body to be fully read into memory now,
                    # while the session (and its connection) is still
                    # open, so the caller can still use resp.text/.content
                    # after we close the session below.
                    resp.content  # noqa: B018 (accessed for its side effect)
                    return resp
            except Exception as e:
                print(f"  → {label}: request error ({e})")
                continue

        return None
    finally:
        scraper.close()


def get_next_line(lyrics, current_time):
    min_time = float('inf')
    next_line = ""
    for line in lyrics.get("lines", []):
        try:
            start = int(line["startTimeMs"])
            past = current_time - start
            if 0 <= past < min_time:
                min_time = past
                next_line = line["words"]
        except:
            continue
    return next_line


def get_current_playback(sp):
    """
    Lightweight check of what's currently playing on Spotify — a single
    cheap API call, no lyric fetching. Meant to be polled frequently
    (every few seconds) to detect song changes/skips quickly, without the
    cost of re-running the whole lyric-source cascade on every poll.

    Returns (song_dict_or_None, info_dict). info_dict contains
    {"reauth_needed": True} on an expired token, or {} otherwise.
    """
    try:
        song = sp.current_user_playing_track()
        if not song or not song.get('item'):
            return None, {}
        return song, {}
    except spotipy.exceptions.SpotifyException as e:
        if getattr(e, "http_status", None) == 401:
            # Access token expired/invalid. Signal this distinctly so the
            # caller can re-authenticate immediately instead of waiting on
            # a periodic schedule — without this flag, every subsequent
            # call just fails the same way indefinitely with no recovery.
            print("SPOTIFY: Access token expired/invalid (401) — re-authentication needed")
            return None, {"reauth_needed": True}
        print(f"Error getting current song: {e}")
        return None, {}
    except Exception as e:
        print(f"Error getting current song: {e}")
        return None, {}


def fetch_lyrics_for_song(song):
    """
    Runs the full lyric-source cascade (Vercel → LRCLIB → Musixmatch →
    Genius → AZLyrics) for a given song dict from get_current_playback().
    Only call this once per actual song change, not on every poll — the
    scraping fallbacks in particular are slow and shouldn't be re-run for
    a track that's still playing.
    """
    track_id = song["item"]["uri"].split(":")[-1]
    song_name = song['item']['name']
    artist_name = song['item']['artists'][0]['name']
    duration_ms = song['item'].get('duration_ms')
    return get_lyrics(track_id, song_name, artist_name, duration_ms)


def on_new_song(sp):
    """
    Convenience wrapper combining get_current_playback() +
    fetch_lyrics_for_song() in one call — fetches lyrics every time it's
    called, regardless of whether the song actually changed. Kept for
    simplicity/backward compatibility; bots that poll frequently should
    use get_current_playback() + fetch_lyrics_for_song() directly instead
    so lyrics are only fetched once per song change.
    """
    song, info = get_current_playback(sp)
    if info.get("reauth_needed"):
        return None, {"error": True, "reauth_needed": True}
    if not song:
        return None, {"error": True}
    lyrics = fetch_lyrics_for_song(song)
    return song, lyrics


def get_lyrics(track_id, song_name=None, artist_name=None, duration_ms=None):
    # 1. Vercel Spotify Lyrics
    try:
        BYPASS_SECRET = "unw8bMMGgkfygC00Z4XwrAtxV7SCTGGW"
        headers = {"x-vercel-protection-bypass": BYPASS_SECRET}
        url = f"https://lyric-api-spotify-git-main-clutchs-projects.vercel.app/?trackid={track_id}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("syncType") == "LINE_SYNCED" and data.get("lines"):
            print("Lyrics source: Vercel (synced)")
            return data
    except Exception as e:
        print(f"Vercel lyrics failed: {e}")

    # 2. Spicy Lyrics (official Spotify-track API)
    if SPICY_LYRICS_TOKEN:
        spicy_lines = get_spicy_lyrics(track_id)
        if spicy_lines:
            print("Lyrics source: Spicy Lyrics (synced)")
            return {"syncType": "LINE_SYNCED", "lines": spicy_lines, "error": False}

    # 3. Genius (scraped, pseudo-synced)
    if song_name and artist_name:
        genius_lines = scrape_genius_lyrics(song_name, artist_name)
        if genius_lines:
            if duration_ms:
                lines = build_pseudo_synced_lines(genius_lines, duration_ms)
                print("Lyrics source: Genius (scraped, pseudo-synced)")
                return {"syncType": "LINE_SYNCED", "lines": lines, "error": False}
            else:
                print("Lyrics source: Genius (scraped, no duration → static)")
                return {"error": True}

    # 4. LRCLIB (improved)
    if song_name and artist_name:
        try:
            url = f"https://lrclib.net/api/get?track_name={requests.utils.quote(song_name)}&artist_name={requests.utils.quote(artist_name)}"
            response = requests.get(url, timeout=10)

            data = None
            if response.status_code == 200:
                data = response.json()
            elif response.status_code == 404:
                search_url = f"https://lrclib.net/api/search?q={requests.utils.quote(f'{song_name} {artist_name}')}"
                search_resp = requests.get(search_url, timeout=10)
                if search_resp.status_code == 200:
                    results = search_resp.json()
                    for result in results:
                        if result.get("syncedLyrics"):
                            data = result
                            break

            if data and data.get("syncedLyrics"):
                lines = []
                for line in data["syncedLyrics"].splitlines():
                    if line.startswith("[") and "]" in line:
                        try:
                            time_part, words = line.split("]", 1)
                            time_part = time_part[1:].strip()
                            if ":" in time_part:
                                mins, secs = time_part.split(":")
                                ms = int((int(mins) * 60 + float(secs)) * 1000)
                                lines.append({
                                    "startTimeMs": str(ms),
                                    "words": words.strip()
                                })
                        except:
                            continue
                if lines:
                    print("Lyrics source: LRCLIB (synced)")
                    return {"syncType": "LINE_SYNCED", "lines": lines, "error": False}
        except Exception as e:
            print(f"LRCLIB failed: {e}")

    # 5. Musixmatch (unofficial endpoint, can return real synced lyrics)
    if MUSIXMATCH_ENABLED and song_name and artist_name:
        mxm_lines = get_musixmatch_lyrics(song_name, artist_name)
        if mxm_lines:
            print("Lyrics source: Musixmatch (synced)")
            return {"syncType": "LINE_SYNCED", "lines": mxm_lines, "error": False}

    # 6. NetEase Cloud Music (real synced lyrics; huge catalog incl. Western music)
    if NETEASE_ENABLED and song_name and artist_name:
        netease_lines = get_netease_lyrics(song_name, artist_name)
        if netease_lines:
            print("Lyrics source: NetEase (synced)")
            return {"syncType": "LINE_SYNCED", "lines": netease_lines, "error": False}

    # 7. QQ Music (real synced lyrics, same idea as NetEase above)
    if QQMUSIC_ENABLED and song_name and artist_name:
        qq_lines = get_qq_music_lyrics(song_name, artist_name)
        if qq_lines:
            print("Lyrics source: QQ Music (synced)")
            return {"syncType": "LINE_SYNCED", "lines": qq_lines, "error": False}

    # 8. AZLyrics (scraped, pseudo-synced) — last resort
    if AZLYRICS_ENABLED and song_name and artist_name and duration_ms:
        az_lines = scrape_azlyrics(song_name, artist_name)
        if az_lines:
            lines = build_pseudo_synced_lines(az_lines, duration_ms)
            print("Lyrics source: AZLyrics (scraped, pseudo-synced)")
            return {"syncType": "LINE_SYNCED", "lines": lines, "error": False}

    print("No lyrics found from any source")
    return {"error": True}


def _parse_lrc_lines(lrc_text):
    """
    Parses a standard LRC-format string ("[mm:ss.xx]lyric line" per line,
    metadata tags like [ar:...]/[ti:...] ignored) into a list of
    {startTimeMs, words} dicts. Handles lines with multiple leading
    timestamps (the same lyric repeated at more than one time).
    """
    lines = []
    for raw_line in lrc_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("["):
            continue
        timestamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        if not timestamps:
            continue
        words = re.sub(r"^(\[\d+:\d+(?:\.\d+)?\])+", "", line).strip()
        if not words:
            continue
        for mins, secs in timestamps:
            try:
                ms = int((int(mins) * 60 + float(secs)) * 1000)
                lines.append({"startTimeMs": str(ms), "words": words})
            except ValueError:
                continue
    return lines


def get_spicy_lyrics(track_id):
    """Fetches Spicy Lyrics' best timed vocal sync for a Spotify track."""
    try:
        response = requests.get(
            f"https://api.spicylyrics.org/v1/lyrics/{track_id}",
            headers={"Authorization": f"Bearer {SPICY_LYRICS_TOKEN}"},
            timeout=10,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()

        body = response.json().get("Body", {})
        if not isinstance(body, dict) or body.get("Type") == "Static":
            return None

        lines = []
        for content in body.get("Content", []):
            if not isinstance(content, dict):
                continue
            lead = content.get("Lead", {})
            if not isinstance(lead, dict):
                continue

            syllables = lead.get("Syllables", [])
            words = "".join(
                syllable.get("Text", "")
                for syllable in syllables
                if isinstance(syllable, dict)
            ).strip()
            if not words:
                words = (lead.get("TransliteratedText") or lead.get("TranslatedText") or "").strip()
            if not words:
                continue

            start_time = lead.get("StartTime")
            if start_time is None and syllables:
                start_time = syllables[0].get("StartTime")
            if start_time is None:
                continue

            lines.append({"startTimeMs": str(int(start_time)), "words": words})

        return lines or None
    except Exception as e:
        print(f"Spicy Lyrics failed: {e}")
        return None


def get_netease_lyrics(song_name, artist_name):
    """
    NetEase Cloud Music (music.163.com) — public, unofficial, no-auth
    search + lyrics endpoints. Despite being a Chinese platform, its
    catalog includes a large amount of Western/international music with
    genuine synced lyrics. Returns a list of {startTimeMs, words} dicts,
    or None.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://music.163.com/",
        }
        search_resp = requests.get(
            "http://music.163.com/api/search/get",
            params={"type": 1, "s": f"{song_name} {artist_name}", "limit": 5, "offset": 0},
            headers=headers,
            timeout=10,
        )
        search_resp.raise_for_status()
        songs = search_resp.json().get("result", {}).get("songs", [])
        print(f"NetEase: {len(songs)} search result(s) for '{song_name}' by '{artist_name}'")
        if not songs:
            return None

        target_artist = re.sub(r"[^a-z0-9]", "", artist_name.lower())
        song_id = None
        for song in songs:
            for a in song.get("artists", []):
                result_artist = re.sub(r"[^a-z0-9]", "", a.get("name", "").lower())
                if target_artist and (target_artist in result_artist or result_artist in target_artist):
                    song_id = song.get("id")
                    break
            if song_id:
                break
        if not song_id:
            song_id = songs[0].get("id")
        if not song_id:
            return None

        lyric_resp = requests.get(
            "http://music.163.com/api/song/lyric",
            params={"id": song_id, "lv": -1, "kv": -1, "tv": -1},
            headers=headers,
            timeout=10,
        )
        lyric_resp.raise_for_status()
        lrc_text = lyric_resp.json().get("lrc", {}).get("lyric")
        if not lrc_text:
            print("NetEase: track found but has no synced lyrics")
            return None

        lines = _parse_lrc_lines(lrc_text)
        return lines if lines else None
    except Exception as e:
        print(f"NetEase lyrics failed: {e}")
        return None


def get_qq_music_lyrics(song_name, artist_name):
    """
    QQ Music (y.qq.com) — public, unofficial, no-auth search + lyrics
    endpoints, similar in spirit to NetEase above. Lyrics come back
    base64-encoded LRC text. Returns a list of {startTimeMs, words}
    dicts, or None.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://y.qq.com/n/ryqq/player",
            "Origin": "https://y.qq.com",
        }
        search_resp = requests.get(
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            params={"w": f"{song_name} {artist_name}", "format": "json", "p": 1, "n": 5, "t": 0},
            headers=headers,
            timeout=10,
        )
        search_resp.raise_for_status()
        song_list = search_resp.json().get("data", {}).get("song", {}).get("list", [])
        print(f"QQ Music: {len(song_list)} search result(s) for '{song_name}' by '{artist_name}'")
        if not song_list:
            return None

        target_artist = re.sub(r"[^a-z0-9]", "", artist_name.lower())
        songmid = None
        for song in song_list:
            for s in song.get("singer", []):
                result_artist = re.sub(r"[^a-z0-9]", "", s.get("name", "").lower())
                if target_artist and (target_artist in result_artist or result_artist in target_artist):
                    songmid = song.get("songmid")
                    break
            if songmid:
                break
        if not songmid:
            songmid = song_list[0].get("songmid")
        if not songmid:
            return None

        lyric_resp = requests.get(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            params={
                "songmid": songmid,
                "g_tk": 5381,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "nobase64": 0,
            },
            headers=headers,
            timeout=10,
        )
        lyric_resp.raise_for_status()

        text = lyric_resp.text.strip()
        # Occasionally still comes back JSONP-wrapped despite format=json.
        if text.startswith("MusicJsonCallback(") and text.endswith(")"):
            text = text[len("MusicJsonCallback("):-1]

        lyric_data = json.loads(text)
        encoded_lyric = lyric_data.get("lyric")
        if not encoded_lyric:
            print("QQ Music: track found but has no lyrics")
            return None

        try:
            lrc_text = base64.b64decode(encoded_lyric).decode("utf-8", errors="ignore")
        except Exception:
            lrc_text = encoded_lyric  # wasn't actually base64-encoded this time

        lines = _parse_lrc_lines(lrc_text)
        return lines if lines else None
    except Exception as e:
        print(f"QQ Music lyrics failed: {e}")
        return None


def get_musixmatch_token():
    """
    Musixmatch's official API requires a paid key and doesn't return full
    lyrics on the free tier. This uses the same unauthenticated "desktop
    app" token endpoint that most open-source lyric tools rely on. It's
    unofficial and can stop working if Musixmatch changes it.
    """
    global musixmatch_token
    if musixmatch_token:
        return musixmatch_token
    try:
        url = "https://apic-desktop.musixmatch.com/ws/1.1/token.get?app_id=web-desktop-app-v1.0"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        status = data.get("message", {}).get("header", {}).get("status_code")
        if status == 200:
            token = data["message"]["body"].get("user_token")
            musixmatch_token = token
            print("Musixmatch: token acquired")
            return token
        print(f"Musixmatch: token endpoint returned status {status}")
    except Exception as e:
        print(f"Musixmatch token fetch failed: {e}")
    return None


def search_musixmatch_track(song_name, artist_name):
    """
    Searches Musixmatch for the track and picks the best match, checking
    that the returned artist name actually matches — avoids the fuzzy
    title-only matcher locking onto the wrong track (cover, translated
    release, or unrelated song with a similar title).
    Returns a Musixmatch track_id, or None.
    """
    token = get_musixmatch_token()
    if not token:
        print("Musixmatch: no token, skipping search")
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = (
            "https://apic-desktop.musixmatch.com/ws/1.1/track.search"
            "?format=json&f_has_lyrics=1&s_track_rating=desc&page_size=5"
            f"&q_track={requests.utils.quote(song_name)}"
            f"&q_artist={requests.utils.quote(artist_name)}"
            f"&usertoken={token}"
        )
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()

        header = data.get("message", {}).get("header", {})
        if header.get("status_code") == 401:
            global musixmatch_token
            musixmatch_token = None
            print("Musixmatch: search token rejected (401), will refresh next time")
            return None
        if header.get("status_code") != 200:
            print(f"Musixmatch: search returned status {header.get('status_code')}")
            return None

        track_list = data.get("message", {}).get("body", {}).get("track_list", [])
        print(f"Musixmatch: {len(track_list)} search result(s) for '{song_name}' by '{artist_name}'")
        if not track_list:
            return None

        target_artist = re.sub(r"[^a-z0-9]", "", artist_name.lower())

        for entry in track_list:
            track = entry.get("track", {})
            result_artist = re.sub(r"[^a-z0-9]", "", track.get("artist_name", "").lower())
            if target_artist and (target_artist in result_artist or result_artist in target_artist):
                if track.get("has_subtitles") or track.get("has_lyrics"):
                    print(f"Musixmatch: matched track_id {track.get('track_id')} by '{track.get('artist_name')}'")
                    return track.get("track_id")

        print("Musixmatch: no result's artist closely matched — skipping to avoid wrong-song lyrics")
        return None
    except Exception as e:
        print(f"Musixmatch search failed: {e}")
        return None


def get_musixmatch_lyrics(song_name, artist_name):
    """
    Fetches line-synced lyrics (LRC format) from Musixmatch for a
    confirmed track match. Returns a list of {startTimeMs, words} dicts,
    or None.
    """
    token = get_musixmatch_token()
    if not token:
        return None

    track_id = search_musixmatch_track(song_name, artist_name)
    if not track_id:
        return None

    global musixmatch_token
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = (
            "https://apic-desktop.musixmatch.com/ws/1.1/track.subtitles.get"
            f"?format=json&subtitle_format=lrc&track_id={track_id}"
            f"&usertoken={token}"
        )
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()

        header = data.get("message", {}).get("header", {})
        status = header.get("status_code")

        if status == 401:
            musixmatch_token = None
            print("Musixmatch: subtitles token rejected (401), will refresh next time")
            return None
        if status != 200:
            print(f"Musixmatch: subtitles.get returned status {status} (no synced lyrics for this track)")
            return None

        subtitle_list = data.get("message", {}).get("body", {}).get("subtitle_list", [])
        if not subtitle_list:
            print("Musixmatch: track matched but has no subtitle entries")
            return None

        subtitle_body = subtitle_list[0].get("subtitle", {}).get("subtitle_body")
        if not subtitle_body:
            print("Musixmatch: subtitle entry was empty")
            return None

        lines = []
        for line in subtitle_body.splitlines():
            if not (line.startswith("[") and "]" in line):
                continue
            try:
                time_part, words = line.split("]", 1)
                time_part = time_part[1:].strip()
                if ":" not in time_part:
                    continue
                mins, secs = time_part.split(":")
                ms = int((int(mins) * 60 + float(secs)) * 1000)
                words = words.strip()
                if words:
                    lines.append({"startTimeMs": str(ms), "words": words})
            except (ValueError, IndexError):
                continue

        return lines if lines else None
    except Exception as e:
        print(f"Musixmatch lyrics failed: {e}")
        return None


def scrape_azlyrics(song_name, artist_name):
    """
    AZLyrics has no public API, so this guesses the page URL from the
    artist/song name using AZLyrics's own URL convention (lowercase,
    strip a leading "the", strip everything but letters/numbers), then
    scrapes the lyrics block off the page.

    This is brittle by nature: featured artists, alternate titles, or
    typos in either name will cause a miss. Returns a list of lyric line
    strings, or None if the page can't be found/parsed.

    Note: scraping AZLyrics is against their Terms of Service. Provided
    for personal/educational use — go easy on their servers and consider
    caching results so you're not re-scraping the same song every replay.
    """
    try:
        def slugify(text):
            text = text.lower()
            text = re.sub(r"^the\s+", "", text)
            text = re.sub(r"[^a-z0-9]", "", text)
            return text

        artist_slug = slugify(artist_name)
        title_slug = slugify(song_name)
        if not artist_slug or not title_slug:
            return None

        url = f"https://www.azlyrics.com/lyrics/{artist_slug}/{title_slug}.html"
        print(f"AZLyrics: trying {url}")
        browser_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = _scraper_get_with_proxies(url, browser_headers, timeout=15)
        if not resp:
            print("AZLyrics: every attempt (all proxies + direct) failed")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        comment = soup.find(
            string=lambda t: isinstance(t, Comment) and "Usage of azlyrics.com content" in t
        )
        if not comment:
            print("AZLyrics: page loaded but couldn't find the lyrics marker comment")
            return None

        lyrics_div = comment.find_next("div")
        if not lyrics_div:
            return None

        for br in lyrics_div.find_all("br"):
            br.replace_with("\n")
        text = lyrics_div.get_text()

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        print(f"AZLyrics: extracted {len(lines)} line(s)")
        return lines if lines else None
    except Exception as e:
        print(f"AZLyrics scrape failed: {e}")
        return None


def clean_title_for_search(title):
    """
    Strips things like "(feat. X)", "(Remastered 2011)", "- Remix", or
    "[Live]" off a track title before searching Genius's website.
    is picky about exact title matches, and Spotify titles often carry
    extra info that the Genius page title doesn't.
    """
    cleaned = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title)
    cleaned = re.sub(
        r"\s*-\s*(remix|remaster(ed)?\s*\d*|live|acoustic|mono|stereo|edit|version).*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip() or title


def scrape_genius_lyrics(song_name, artist_name):
    """
    Looks up a song on Genius's regular website search page, then scrapes the
    lyrics off the song's page. This avoids requiring a Genius API token.
    Returns a list of non-empty lyric line strings, or None on failure.

    Note: scraping Genius's site is against their Terms of Service. This is
    provided for personal/educational use — don't hammer their servers, and
    consider caching results locally so you're not re-scraping the same
    song on every replay.
    """
    try:
        search_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://genius.com/",
        }

        def do_search(query_title):
            search_url = (
                f"https://genius.com/search?q="
                f"{requests.utils.quote(f'{query_title} {artist_name}')}"
            )
            response = _scraper_get_with_proxies(
                search_url, search_headers, timeout=8, use_proxies=False
            )
            html = response.text if response else None
            if not html:
                return []

            soup = BeautifulSoup(html, "html.parser")
            links = []
            for anchor in soup.find_all("a", href=True):
                href = anchor["href"]
                normalized = href.split("?", 1)[0].rstrip("/")
                if not normalized.startswith("https://genius.com/"):
                    normalized = f"https://genius.com{normalized}" if normalized.startswith("/") else ""
                if not normalized or normalized in links:
                    continue
                path = normalized.removeprefix("https://genius.com/")
                if "lyrics" in path and not path.startswith((
                    "search", "artists/", "albums/", "tags/", "users/", "videos/"
                )):
                    links.append(normalized)
            return links

        cleaned_title = clean_title_for_search(song_name)
        hits = do_search(cleaned_title)
        if not hits and cleaned_title != song_name:
            hits = do_search(song_name)

        print(f"Genius: {len(hits)} website result(s) for '{song_name}' by '{artist_name}'")
        if not hits:
            return None

        # Just take the top search result, no artist-matching filter —
        # if Genius's search found something, use it.
        song_url = hits[0]
        if not song_url:
            return None

        # Genius sits behind Cloudflare, which fingerprints the TLS
        # handshake itself — plain "requests" gets flagged as non-browser
        # traffic no matter what headers you attach, and datacenter/VPS
        # IPs get treated more suspiciously than home connections.
        # cloudscraper mimics a real browser's handshake to get past this.
        browser_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://genius.com/",
        }

        page_resp = _scraper_get_with_proxies(
            song_url, browser_headers, timeout=8, use_proxies=False
        )
        if not page_resp:
            print("Genius: cloudscraper request failed")
            return None
        html = page_resp.text
        print(f"Genius: fetched '{song_url}' via cloudscraper → status {page_resp.status_code}")

        soup = BeautifulSoup(html, "html.parser")
        containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
        print(f"Genius: found {len(containers)} lyrics container(s) on the page")
        if not containers:
            return None

        lines = []
        for container in containers:
            for br in container.find_all("br"):
                br.replace_with("\n")
            text = container.get_text()
            for raw_line in text.split("\n"):
                line = raw_line.strip()
                if not line or re.match(r"^\[.*\]$", line):
                    continue
                lines.append(line)

        return lines if lines else None
    except Exception as e:
        print(f"Genius scrape failed: {e}")
        return None


def build_pseudo_synced_lines(lines, duration_ms):
    """
    Scraped lyrics (Genius/AZLyrics) don't come with timestamps, so this
    spreads lines evenly across the track's duration as a rough
    approximation. Won't line up perfectly with the music, but it beats
    showing nothing.
    """
    if not lines:
        return []

    usable_duration = max(duration_ms - 5000, 1000)
    interval = usable_duration / len(lines)

    synced = []
    for i, words in enumerate(lines):
        start_ms = int(2000 + i * interval)
        synced.append({"startTimeMs": str(start_ms), "words": words})
    return synced


def get_spotify_client(quiet=False):
    global token_info, auth_manager

    if not quiet:
        print("SPOTIFY: Getting / refreshing token...")

    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_ID,
        client_secret=SPOTIFY_SECRET,
        redirect_uri=SPOTIFY_REDIRECT,
        scope=SCOPE,
        open_browser=False,
        cache_path=".cache"
    )

    cached = None
    try:
        cached = auth_manager.get_cached_token()
    except Exception as e:
        if not quiet:
            print(f"Couldn't read .cache ({e}) — will need to log in")

    if cached:
        if auth_manager.is_token_expired(cached):
            if not quiet:
                print("Cached token expired → refreshing...")
            token_info = auth_manager.refresh_access_token(cached["refresh_token"])
        else:
            if not quiet:
                print("Using valid cached token from .cache")
            token_info = cached
    else:
        print("\n=== AUTHORIZATION NEEDED ===")
        print("Open this URL in your browser and log in with your Premium account:\n")
        print(auth_manager.get_authorize_url())
        print()

        redirected_url = input("Paste the FULL redirected URL here → ").strip()
        code = auth_manager.parse_response_code(redirected_url)

        # get_access_token() saves the full token dict to the cache file
        # internally before it even looks at as_dict, so as_dict=False
        # here just silences spotipy's deprecation warning — we don't
        # actually need the return value, since we read the saved dict
        # straight back from the cache below.
        auth_manager.get_access_token(code, as_dict=False)

        token_info = auth_manager.get_cached_token()
        if token_info:
            if not quiet:
                print("Token obtained and saved to .cache\n")
        else:
            print("Token obtained, but couldn't be read back from .cache.")
            print("You'll need to log in again next run.\n")
            raise RuntimeError("Failed to persist Spotify token to .cache")

    sp = spotipy.Spotify(auth=token_info["access_token"])
    return sp