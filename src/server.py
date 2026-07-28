"""Uvicorn bootstrap: reads the HOST/PORT env vars and starts the app."""

import os
import threading
import webbrowser

import uvicorn

from src.app import app
from src.logs import log_event, setup_logging

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8765))


def main():
    """Serve the app, opening a browser tab when it is bound to localhost.

    Uvicorn's own access log stays off: it prints a line per request, including
    the one-per-page-per-resize `/api/page/{sid}/{n}`, which would bury the
    three events that matter. Those are logged by hand in `src.app` instead.
    """
    setup_logging()
    log_event("startup", host=HOST, port=PORT, log_file=os.environ.get("SPYDF_LOG_FILE") or "-")
    url = f"http://127.0.0.1:{PORT}"
    print(f"SpyDF -> {url}")
    if HOST == "127.0.0.1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
