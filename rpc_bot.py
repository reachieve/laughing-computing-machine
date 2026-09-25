"""
Shows the current Spotify lyric line via Discord Rich Presence (the same
mechanism Spotify, VS Code, and games use to show "Playing: X" under your
name), instead of PATCHing your account's custom_status.

Why this avoids the rate-limit problem status_bot.py has:
Rich Presence talks to Discord over a local IPC connection to your running
Discord desktop client, not the REST endpoint that PATCHes account
settings — so it isn't subject to that endpoint's rate limit. It also
doesn't require your personal Discord account token at all (no
DISCORD_AUTH needed here), just a Client ID for a Discord "Application"
you register.

REQUIREMENTS — read before running:
1. The Discord desktop client must be running and logged in on the SAME
   machine as this script. IPC is local-only; this will NOT work on a
   headless remote server/container unless Discord is also running there
   with a display (not a typical setup).
2. Create a Discord Application (not a bot) at
   https://discord.com/developers/applications → "New Application" →
   copy its "Application ID" from the General Information page.
3. Set that ID as DISCORD_CLIENT_ID in your .env.
4. pip install pypresence

Discord's own client still throttles how often it forwards Rich Presence
updates to its backend (roughly ~1 update per second is safe; Discord's
own guidance suggests not exceeding this). RPC_MIN_UPDATE_INTERVAL below
handles that pacing — it's far more generous than the custom_status
endpoint's limit, so fast-changing lyric lines come through much more
reliably than with status_bot.py.
"""

import os
import time
from dotenv import load_dotenv
from pypresence import Presence, PyPresenceException

from lyrics_core import get_spotify_client, get_current_playback, fetch_lyrics_for_song, get_next_line

load_dotenv()

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")

SECONDS_TO_SPOTIFY_RESYNC = int(os.environ.get("SPOTIFY_RESYNC_SECONDS", "3"))
POLL_INTERVAL_SECONDS = 1.0
RPC_MIN_UPDATE_INTERVAL = float(os.environ.get("RPC_MIN_UPDATE_INTERVAL", "1.0"))
IDLE_STATUS_TEXT = os.environ.get("IDLE_STATUS_TEXT", "clutch's lyric syncer")


def connect_rpc():
    if not DISCORD_CLIENT_ID:
        raise SystemExit(
            "DISCORD_CLIENT_ID is not set. Create a Discord Application at "
            "https://discord.com/developers/applications, copy its "
            "Application ID from the General Information page, and set "
            "DISCORD_CLIENT_ID in your .env."
        )

    rpc = Presence(DISCORD_CLIENT_ID)
    print("RPC: Connecting to local Discord client...")
    try:
        rpc.connect()
    except Exception as e:
        raise SystemExit(
            f"RPC: Couldn't connect to Discord ({e}). Make sure the Discord "
            "desktop app is open and logged in on this same machine — Rich "
            "Presence connects over local IPC and won't work on a headless "
            "remote server without a Discord client running on it."
        )
    print("RPC: Connected.")
    return rpc


def main():
    rpc = connect_rpc()
    sp = get_spotify_client()

    song = None
    lyrics = {"error": True}
    last_track_id = None
    last_state_text = None
    last_song_id = None
    last_rpc_update = 0.0
    loop_count = 0

    # Real-time based periodic refresh, since access tokens expire after
    # roughly an hour and there was previously no refresh at all here
    # beyond the initial login.
    SPOTIFY_REAUTH_INTERVAL_SECONDS = 300
    last_spotify_reauth = time.time()

    try:
        while True:
            try:
                if time.time() - last_spotify_reauth >= SPOTIFY_REAUTH_INTERVAL_SECONDS:
                    sp = get_spotify_client()
                    last_spotify_reauth = time.time()

                # Predict locally whether the current track has ended,
                # using the progress estimate advanced each loop below —
                # this clears the presence the instant a song ends
                # instead of waiting up to SECONDS_TO_SPOTIFY_RESYNC for
                # the next scheduled poll to notice.
                force_resync = False
                if song and song['item'].get('duration_ms') and song['progress_ms'] >= song['item']['duration_ms']:
                    print("SPOTIFY: Track ended locally — clearing and resyncing now")
                    song = None
                    lyrics = {"error": True}
                    last_track_id = None
                    force_resync = True

                if force_resync or loop_count % SECONDS_TO_SPOTIFY_RESYNC == 0:
                    new_song, info = get_current_playback(sp)

                    if info.get("reauth_needed"):
                        # Don't wait for the periodic check above — a 401
                        # means every call keeps failing identically
                        # until we get a fresh token, so react immediately.
                        print("SPOTIFY: Re-authenticating now due to expired token...")
                        try:
                            sp = get_spotify_client()
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
                            # Actual song change (or first song after
                            # startup) — this is the only time the full
                            # lyric-source cascade needs to run.
                            print(f"SPOTIFY: Song changed → fetching lyrics for '{new_song['item']['name']}'")
                            lyrics = fetch_lyrics_for_song(new_song)
                            last_track_id = new_track_id
                        song = new_song

                if not song:
                    if last_state_text != IDLE_STATUS_TEXT:
                        print(f"RPC: No song playing → {IDLE_STATUS_TEXT}")
                        try:
                            rpc.update(details=IDLE_STATUS_TEXT)
                        except PyPresenceException as e:
                            print(f"RPC: update failed ({e})")
                        last_state_text = IDLE_STATUS_TEXT
                        last_song_id = None
                    time.sleep(POLL_INTERVAL_SECONDS)
                    loop_count += 1
                    continue

                song_name = song['item']['name']
                artist_name = song['item']['artists'][0]['name']
                song_id = song['item']['id']
                current_time = song['progress_ms']
                duration_ms = song['item'].get('duration_ms')

                if lyrics.get("error", False) or lyrics.get("syncType") != "LINE_SYNCED":
                    state_text = artist_name
                else:
                    next_line = get_next_line(lyrics, current_time)
                    state_text = next_line or artist_name

                now = time.time()
                song_changed = song_id != last_song_id
                line_changed = state_text != last_state_text
                interval_ok = (now - last_rpc_update) >= RPC_MIN_UPDATE_INTERVAL

                if (song_changed or line_changed) and interval_ok:
                    start_ts = None
                    end_ts = None
                    if duration_ms:
                        start_ts = int(now - (current_time / 1000))
                        end_ts = int(start_ts + (duration_ms / 1000))

                    try:
                        rpc.update(
                            details=song_name[:128],
                            state=(state_text or "")[:128],
                            start=start_ts,
                            end=end_ts,
                        )
                        print(f"RPC: {song_name} — {state_text}")
                        last_state_text = state_text
                        last_song_id = song_id
                        last_rpc_update = now
                    except PyPresenceException as e:
                        print(f"RPC: update failed ({e}) — is Discord still running?")

                # Advance the local progress-ms estimate between resyncs,
                # same approach as status_bot.py.
                song["progress_ms"] += POLL_INTERVAL_SECONDS * 1000

                time.sleep(POLL_INTERVAL_SECONDS)
                loop_count += 1

            except Exception as e:
                print(f"RPC loop error: {e}")
                time.sleep(5)

    finally:
        try:
            rpc.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()