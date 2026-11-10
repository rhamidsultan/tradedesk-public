"""Zerodha's WebSocket price stream with reconnects. Read-only: it subscribes to prices and nothing else."""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("tdserver.recorder")
WS_URL = "wss://ws.kite.trade"
MAX_TOKENS = 3000                  # Zerodha's limit per connection


class KiteStream:
    """Runs on its own thread; calls on_frame(bytes, received_epoch) for every binary frame."""

    def __init__(self, api_key: str, access_token: str, on_frame, on_state=None, url: str = WS_URL, connect=None):
        self._url = f"{url}?api_key={api_key}&access_token={access_token}"
        self._on_frame, self._on_state = on_frame, on_state or (lambda *_: None)
        self._connect = connect
        self._tokens: set[int] = set()
        self._lock = threading.Lock()
        self._ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.reconnects = 0
        self.last_error = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="kite-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def subscribe(self, tokens) -> None:
        with self._lock:
            new = [t for t in tokens if t not in self._tokens][: MAX_TOKENS - len(self._tokens)]
            self._tokens.update(new)
        if new:
            self._send_subscribe(new)

    def _send_subscribe(self, tokens) -> None:
        ws = self._ws
        if ws is None or not tokens:
            return
        try:
            ws.send(json.dumps({"a": "subscribe", "v": list(tokens)}))
            ws.send(json.dumps({"a": "mode", "v": ["full", list(tokens)]}))
        except Exception as e:  # noqa: BLE001
            self.last_error = type(e).__name__

    def _open(self):
        if self._connect is not None:
            return self._connect(self._url)
        from websockets.sync.client import connect
        return connect(self._url, open_timeout=15, close_timeout=3, max_size=2 ** 22)

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                self._ws = self._open()
                self.connected = True
                self._on_state("connected", "")
                delay = 1.0
                with self._lock:
                    tokens = list(self._tokens)
                self._send_subscribe(tokens)
                while not self._stop.is_set():
                    try:
                        msg = self._ws.recv(timeout=10)
                    except TimeoutError:
                        continue
                    if isinstance(msg, (bytes, bytearray)):
                        self._on_frame(bytes(msg), time.time())
                    # text frames are order updates and messages; the recorder has no use for them
            except Exception as e:  # noqa: BLE001  never log e itself: the URL holds the access token
                self.last_error = type(e).__name__
            finally:
                if self.connected:
                    self.connected = False
                    self._on_state("disconnected", self.last_error)
                self._ws = None
            if self._stop.is_set():
                break
            self.reconnects += 1
            self._stop.wait(delay)
            delay = min(30.0, delay * 2)
