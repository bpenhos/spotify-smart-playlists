import os
import threading
from app.db.init_db import run_init_db


def run_startup_tasks():
    run_init_db()

    from utils.db_utils import get_db_connection
    from utils.spotify_auth import get_spotify_client
    from utils.create_exclusions_playlist import ensure_exclusions_playlist
    from utils.logger import log_event

    try:
        refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
        if refresh_token:
            log_event("init", "🔑 Refresh token found in environment. Checking exclusions playlist.")
            sp = get_spotify_client()
            ensure_exclusions_playlist(sp)
            log_event("init", "ℹ️ Exclusions playlist already exists. No action taken.")
            log_event("init", "✅ Exclusions playlist check complete. No action needed.")
        else:
            log_event("init", "⚠️ Skipping exclusions playlist check. No SPOTIFY_REFRESH_TOKEN found.")

    except Exception:
        import traceback
        print("⚠️ Error during exclusions playlist check:", flush=True)
        traceback.print_exc()


def _run_in_background():
    try:
        run_startup_tasks()
    except Exception as e:
        print(f"❌ Startup tasks failed: {e}", flush=True)


# Se ejecuta en un hilo aparte para que el servidor web abra el puerto
# de inmediato y Render no marque el deploy como fallido.
threading.Thread(target=_run_in_background, daemon=True).start()
