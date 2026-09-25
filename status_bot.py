"""
Shows the current Spotify lyric line as your Discord custom status, by
directly PATCHing your own account's settings. This uses your personal
Discord token to automate your own account — note that automating a
normal user account like this is against Discord's Terms of Service and
carries some risk of the account being flagged, separate from the rate
limit issue this file specifically works around.

If you can run this on a machine with the real Discord desktop client
open instead, rpc_bot.py (Discord Rich Presence) is the officially
supported alternative and doesn't carry that same risk.
"""

import requests
import time
import fpstimer
import os
import io
import sys
from contextlib import redirect_stdout
from dotenv import load_dotenv

from lyrics_core import get_spotify_client, get_current_playback, fetch_lyrics_for_song, get_next_line

load_dotenv()

API_TOKEN = os.environ.get("DISCORD_AUTH")

LYRIC_UPDATE_RATE_PER_SECOND = 20
SECONDS_TO_SPOTIFY_RESYNC = int(os.environ.get("SPOTIFY_RESYNC_SECONDS", "3"))
FALLBACK_TEXT = "{song_name} — {artist_name}"
IDLE_STATUS_TEXT = os.environ.get("IDLE_STATUS_TEXT", "clutch's lyric syncer")

# Discord's custom_status endpoint has an undocumented, fairly strict rate
# limit. Hitting it once per lyric line (every couple seconds) reliably
# triggers 429s after the first update.
#
# This starting value is just a floor. The actual interval is learned and
# raised automatically from Discord's own `retry_after` value whenever a
# 429 comes back, so it converges on whatever the real limit is instead of
# relying on a guessed constant.
DISCORD_MIN_UPDATE_INTERVAL = float(os.environ.get("DISCORD_MIN_UPDATE_INTERVAL", "2.0"))

TIMER = fpstimer.FPSTimer(LYRIC_UPDATE_RATE_PER_SECOND)

_last_discord_update = 0.0
_current_min_interval = DISCORD_MIN_UPDATE_INTERVAL
_last_discord_text = object()
_discord_auth_invalid = False


def print_startup_banner():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    pink = "\033[95m"
    light_pink = "\033[35m"
    white = "\033[97m"
    gray = "\033[90m"
    reset = "\033[0m"

    logo = [
        "▄▄▄▀▀▀█               ▄▄▄▀▀▀█                    ▄▄▄▀▀▀█",
        "                 █   █                 █   █                      █   █",
        "  ▀▄▄▄▀▀▀▄▄▄▄▄   █ ░ █    ▀▄▄▄ ▄▄▄▀    █ ░ █▄▄▄    ▀▄▄▄▀▀▀▄▄▄▄▄   █ ░ █",
        " ██▓██    █▀▓▀▌  █░▒░█   ██▓██ ██▓██   █░▒░▀▓█▀▀  ██▓██    █▀▓▀▌  █░▒░█",
        "█▓▒▓       ▀▀▀   █▒▓▒█  █▓▒▓     ▓▒▓█  █▒▓▒██░█  █▓▒▓       ▀▀▀   █▒▓▒▄▀▀▓▄▄▄■▄",
        "█▒░▒▓            █▓█▓░  █▒░▒▓   ▓▒░▒█  █▓█▓░▒▀   █▒░▒▓            █▓█▓░  ▐▐░▓█▌▌",
        "█░ ░▓    █░██▄   █ ░ ▒  █░ ░▓   ▓░ ░█  █ ░ ▒░    █░ ░▓    █░██▄   █ ░ ▒   █▓▒▓█░",
        " █ ░▒ ▄▄░░▒░▓■   █░▒░▓   █ ░▒▄ ▄▒░ █   █░▒░▓▓▄■▄  █ ░▒ ▄▄░░▒░▓■   █░▒░▓   █▒░░▒▒",
        "   ▀▀▀■▀▀▀▀▀     █▀▀▀     ▀▄▄▄▄▄▄▄▀    ■▀▀▀▄▀▀▀▀    ▀▀▀■▀▀▀▀▀     █▀▀▀    █░▄▄▄▓",
        "                ▀▀                    ▀▀                         ▀▀     ■▀▀▀",
    ]

    for line in logo:
        print(f"{pink}{line}{reset}")
    print(f"{white}clutch was here{reset}")


def refresh_console(lyric=""):
    os.system("cls" if os.name == "nt" else "clear")
    print_startup_banner()
    if lyric:
        print(lyric)


def update_discord_status(text):
    """
    Sends (or clears, if text is None) the Discord custom status, enforcing
    the current learned minimum interval between requests and actually
    checking the response instead of firing-and-forgetting. Returns True if
    Discord accepted the update or the requested text is already active;
    returns False if it was skipped because the rate limit window has not
    elapsed or the request was rejected.
    """
    global _last_discord_update, _current_min_interval, _last_discord_text, _discord_auth_invalid

    if _discord_auth_invalid:
        return False

    if text == _last_discord_text:
        return True

    now = time.time()
    if now - _last_discord_update < _current_min_interval:
        return False

    try:
        resp = requests.patch(
            "https://discord.com/api/v6/users/@me/settings",
            headers={"authorization": API_TOKEN},
            json={"custom_status": {"text": text} if text else None},
            timeout=10
        )

        if resp.status_code == 429:
            retry_after = None
            try:
                retry_after = resp.json().get("retry_after")
            except Exception:
                pass

            if retry_after:
                learned_interval = float(retry_after) + 0.25
                if learned_interval > _current_min_interval:
                    print(
                        f"DISCORD: Rate limited (429) — raising update interval "
                        f"from {_current_min_interval:.1f}s to {learned_interval:.1f}s"
                    )
                    _current_min_interval = learned_interval
            else:
                print("DISCORD: Rate limited (429), no retry_after given")

            _last_discord_update = now
            return False

        if resp.status_code == 401:
            _discord_auth_invalid = True
            print(
                "DISCORD: Authentication failed (401). Revoke the old Discord "
                "token and replace DISCORD_AUTH in .env, or use rpc_bot.py "
                "with a Discord Application ID instead."
            )
            return False

        if not resp.ok:
            print(f"DISCORD: Update rejected ({resp.status_code}): {resp.text[:200]}")
            return False

        _last_discord_update = now
        _last_discord_text = text
        return True

    except Exception as e:
        print(f"DISCORD: Request error: {e}")
        return False


def main(last_played_song, last_played_line, song, lyrics):
    start = time.time()

    if not song:
        if last_played_line == "NO SONG":
            TIMER.sleep()
            return "", "NO SONG"

        if update_discord_status(IDLE_STATUS_TEXT):
            TIMER.sleep()
            return "", "NO SONG"
        TIMER.sleep()
        return last_played_song, last_played_line

    current_time = song["progress_ms"]
    song_name = song['item']['name']
    artist_name = song['item']['artists'][0]['name']
    fallback_status = FALLBACK_TEXT.format(song_name=song_name, artist_name=artist_name)

    if lyrics.get("error", False) or lyrics.get("syncType") != "LINE_SYNCED":
        if last_played_line == "NO LYRICS" and song_name == last_played_song:
            TIMER.sleep()
            return song_name, last_played_line

        if update_discord_status(fallback_status):
            TIMER.sleep()
            return song_name, "NO LYRICS"
        TIMER.sleep()
        return song_name, last_played_line

    next_line = get_next_line(lyrics, current_time)
    if next_line and next_line != last_played_line:
        if update_discord_status(next_line):
            last_played_line = next_line
            refresh_console(next_line)

    TIMER.sleep()
    end = time.time()
    song["progress_ms"] += (end - start) * 1000
    return song_name, last_played_line


if __name__ == "__main__":
    refresh_console()

    last_played_song = ""
    last_played_line = ""
    main_loops = 0

    sp = get_spotify_client(quiet=True)
    song, lyrics = None, {"error": True}
    last_track_id = None

    # Real-time based, not loop-count based: if a resync (which can
    # involve slow network calls — Genius/AZLyrics proxy attempts, etc.)
    # takes much longer than the nominal loop rate assumes, a loop-count
    # schedule silently drifts and the token can expire long before a
    # count-based refresh ever fires. Wall-clock time doesn't have that
    # problem.
    SPOTIFY_REAUTH_INTERVAL_SECONDS = 300
    last_spotify_reauth = time.time()

    while True:
        try:
            if time.time() - last_spotify_reauth >= SPOTIFY_REAUTH_INTERVAL_SECONDS:
                sp = get_spotify_client(quiet=True)
                last_spotify_reauth = time.time()

            # Predict locally whether the current track has ended, using
            # the progress estimate main() already keeps updated each
            # loop — this clears the status the instant a song ends
            # instead of waiting up to SECONDS_TO_SPOTIFY_RESYNC for the
            # next scheduled poll to notice.
            force_resync = False
            if song and song['item'].get('duration_ms') and song['progress_ms'] >= song['item']['duration_ms']:
                song = None
                lyrics = {"error": True}
                last_track_id = None
                force_resync = True

            if force_resync or main_loops % (LYRIC_UPDATE_RATE_PER_SECOND * SECONDS_TO_SPOTIFY_RESYNC) == 0:
                new_song, info = get_current_playback(sp)

                if info.get("reauth_needed"):
                    # Don't wait for the periodic check above — a 401
                    # means every call will keep failing identically
                    # until we get a fresh token, so react immediately.
                    try:
                        sp = get_spotify_client(quiet=True)
                        last_spotify_reauth = time.time()
                        new_song, info = get_current_playback(sp)
                    except Exception as e:
                        print(f"SPOTIFY: Re-auth failed: {e}")
                        new_song = None

                if not new_song or not new_song.get("is_playing", False):
                    song, lyrics, last_track_id = None, {"error": True}, None
                else:
                    new_track_id = new_song["item"]["id"]
                    if new_track_id != last_track_id:
                        # Actual song change (or first song after startup)
                        # — this is the only time the full lyric-source
                        # cascade needs to run.
                        with redirect_stdout(io.StringIO()):
                            lyrics = fetch_lyrics_for_song(new_song)
                        last_track_id = new_track_id
                    song = new_song

            last_played_song, last_played_line = main(last_played_song, last_played_line, song, lyrics)
            main_loops += 1

        except Exception as e:
            print(f"ERROR: {e}")
            try:
                sp = get_spotify_client(quiet=True)
            except Exception as e2:
                print(f"Re-auth failed: {e2}")
            time.sleep(5)