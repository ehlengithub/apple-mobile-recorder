"""Run the AirPlay receiver on its own: `python -m hidagent.receiver`.

It runs UxPlay (starting it again whenever it exits) and serves the phone's screen as JPEG frames on a local TCP
port; the server follows that stream (MIRROR_STREAM=127.0.0.1:8891), so restarting the server leaves the phone's
Screen Mirroring session alone. UxPlay's pid is recorded so the server can still restart a frozen receiver.
"""

import logging
import os
import socket
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from .screen import Screen

log = logging.getLogger(__name__)


def serve(screen, port: int) -> None:
    """Send each server that connects the current frame at once, then every new one.

    A still phone sends no frames, so a server restarted in the meantime would otherwise wait for the screen to
    change (GStreamer's tcpserversink only passes on frames made after a client connects).
    """
    with socket.create_server(("127.0.0.1", port)) as srv:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=_feed, args=(screen, conn), daemon=True).start()


def _feed(screen, conn: socket.socket) -> None:
    sent = 0.0
    with conn:
        try:
            while True:
                updated, frame = screen.updated, screen.frame  # in this order a new frame may go twice, never 0 times
                if frame is None and sent:
                    return  # UxPlay went away: closing tells the server the phone is gone
                if frame is not None and updated > sent:
                    conn.sendall(frame)
                    sent = updated
                time.sleep(0.02)
        except OSError:
            pass  # the server went away


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    name = os.environ.get("AIRPLAY_NAME", "Apple Recorder")
    port = int(os.environ.get("MIRROR_PORT", 8991))
    data = Path(os.environ.get("HIDAGENT_DATA", Path.home() / ".apple-mobile-recorder"))
    data.mkdir(parents=True, exist_ok=True)
    screen = Screen(name, fps=30, pidfile=data / "uxplay.pid")
    screen.start()
    log.info("AirPlay receiver '%s': frames on 127.0.0.1:%d", name, port)
    serve(screen, port)


if __name__ == "__main__":
    main()
