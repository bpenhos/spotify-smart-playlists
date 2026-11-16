import os
import sys
import time
import base64
import requests
import requests.exceptions
from spotipy import Spotify
from spotipy.exceptions import SpotifyException
from utils.logger import log_event
from utils.db_utils import get_db_connection

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["SPOTIFY_REFRESH_TOKEN"]

# Si Spotify pide esperar más de este número de segundos, la ejecución se detiene limpiamente.
MAX_WAIT_SECONDS = 120


def fetch_access_token():
    """Pide un access token nuevo usando el refresh token."""
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def new_client():
    # Sin reintentos internos de spotipy: si Spotify pide esperar, el error sale de inmediato.
    return Spotify(auth=fetch_access_token(), retries=0, status_retries=0, requests_timeout=15)


sp = new_client()


def _retry_after_seconds(e):
    headers = getattr(e, "headers", None) or {}
    try:
        return int(headers.get("Retry-After", 5))
    except (TypeError, ValueError):
        return 5


def fetch_artist(artist_id):
    global sp
    retries = 0
    refreshed = False
    while True:
        try:
            return sp.artist(artist_id)
        except SpotifyException as e:
            if e.http_status == 401 and not refreshed:
                refreshed = True
                log_event("sync_artists", "Token expirado. Renovando access token.")
                sp = new_client()
                continue
            if e.http_status == 429:
                retry_after = _retry_after_seconds(e)
                if retry_after > MAX_WAIT_SECONDS:
                    log_event(
                        "sync_artists",
                        f"Spotify rate limit: esperar {retry_after}s es demasiado. "
                        "Se detiene esta ejecución; la siguiente continuará.",
                        level="warning",
                    )
                    sys.exit(0)
                retries += 1
                if retries > 5:
                    raise
                log_event("sync_artists", f"Rate limit hit. Retry #{retries} in {retry_after}s")
                time.sleep(retry_after)
                continue
            # 403 u otros: el llamador omite este artista
            raise
        except requests.exceptions.ConnectionError as e:
            retries += 1
            if retries > 5:
                raise
            log_event("sync_artists", f"Connection error: {e}. Retry #{retries} in 5s", level="warning")
            time.sleep(5)


conn = get_db_connection()
cur = conn.cursor()

# Artistas referenciados por álbumes o likes que aún no tienen datos completos
cur.execute("""
    SELECT DISTINCT artist_id FROM (
        SELECT artist_id FROM albums WHERE artist_id IS NOT NULL
        UNION
        SELECT artist_id FROM liked_tracks WHERE artist_id IS NOT NULL
    ) s
    WHERE artist_id NOT IN (
        SELECT id FROM artists WHERE last_checked_at IS NOT NULL
    )
""")
artist_ids = [row[0] for row in cur.fetchall()]
log_event("sync_artists", f"Artistas pendientes: {len(artist_ids)}")

synced = 0
skipped = 0

for artist_id in artist_ids:
    try:
        data = fetch_artist(artist_id)
    except SpotifyException as e:
        skipped += 1
        log_event("sync_artists", f"Omitido {artist_id} (HTTP {e.http_status})", level="warning")
        continue

    if not data:
        skipped += 1
        continue

    name = data.get("name")
    genres = data.get("genres") or []
    images = data.get("images") or []
    image_url = images[0]["url"] if images else None

    cur.execute("""
        INSERT INTO artists (id, name, genres, image_url, last_checked_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            genres = EXCLUDED.genres,
            image_url = EXCLUDED.image_url,
            last_checked_at = NOW()
    """, (artist_id, name, genres, image_url))
    conn.commit()
    synced += 1

    if synced % 50 == 0:
        log_event("sync_artists", f"Progreso: {synced} artistas sincronizados")

log_event("sync_artists", f"Artistas sincronizados: {synced}. Omitidos: {skipped}")

cur.close()
conn.close()
