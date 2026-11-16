import sys
import time
import requests.exceptions
from spotipy import Spotify
from spotipy.exceptions import SpotifyException
from utils.logger import log_event
from utils.spotify_auth import get_spotify_client
from utils.db_utils import get_db_connection

# Cliente sin reintentos internos de spotipy: si Spotify pide esperar,
# el error sale de inmediato en vez de dormir el proceso horas.
_base_client = get_spotify_client()
sp = Spotify(
    auth_manager=_base_client.auth_manager,
    retries=0,
    status_retries=0,
    requests_timeout=15,
)

# Si Spotify pide esperar más de este número de segundos, la ejecución se detiene limpiamente.
MAX_WAIT_SECONDS = 120


def _retry_after_seconds(e):
    headers = getattr(e, "headers", None) or {}
    try:
        return int(headers.get("Retry-After", 5))
    except (TypeError, ValueError):
        return 5


def safe_call(func, *args, **kwargs):
    retries = 0
    while retries < 5:
        try:
            return func(*args, **kwargs)
        except SpotifyException as e:
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
                log_event("sync_artists", f"Rate limit hit. Retry #{retries} in {retry_after}s")
                time.sleep(retry_after)
            else:
                # 403 u otros errores: se lanzan para que el llamador omita este artista
                raise
        except requests.exceptions.ConnectionError as e:
            retries += 1
            log_event("sync_artists", f"Connection error: {e}. Retry #{retries} in 5s", level="warning")
            time.sleep(5)
    raise Exception("safe_call failed after 5 retries")


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
        data = safe_call(sp.artist, artist_id)
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
