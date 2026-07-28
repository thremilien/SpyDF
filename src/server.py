"""Entry point: starts the local server and opens the browser."""

import os
import threading
import webbrowser

import uvicorn

from src.app import app
from src.logs import log_event, setup_logging

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8765))


def main():
    setup_logging()
    log_event("startup", host=HOST, port=PORT,
              log_file=os.environ.get("SPYDF_LOG_FILE") or "-")
    url = f"http://127.0.0.1:{PORT}"
    print(f"SpyDF -> {url}")
    if HOST == "127.0.0.1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # le journal d'acces d'uvicorn noierait les trois evenements qui comptent
    # (une ligne par requete, y compris /api/page/{sid}/{n} appele en boucle
    # au redimensionnement): on le coupe et on journalise nous-memes.
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
