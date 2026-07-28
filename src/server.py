"""Entry point: starts the local server and opens the browser."""

import os
import threading
import webbrowser

import uvicorn

from src.app import app

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8765))


def main():
    url = f"http://127.0.0.1:{PORT}"
    print(f"SpyDF -> {url}")
    if HOST == "127.0.0.1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
