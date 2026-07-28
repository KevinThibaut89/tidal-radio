"""Minimal telnet client for liquidsoap's command server."""
import logging
import socket

log = logging.getLogger(__name__)


class Liquidsoap:
    def __init__(self, host: str = "127.0.0.1", port: int = 1234):
        self.host = host
        self.port = port

    def _command(self, cmd: str) -> str:
        with socket.create_connection((self.host, self.port), timeout=5) as s:
            s.sendall((cmd + "\n").encode())
            data = b""
            while b"END" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.sendall(b"quit\n")
        return data.decode(errors="replace").replace("END", "").strip()

    def push(self, path: str) -> bool:
        try:
            resp = self._command(f"main.push {path}")
            return resp.strip().isdigit()  # returns the request id
        except Exception as e:
            log.error("liquidsoap push failed: %s", e)
            return False

    def queue_length(self) -> int:
        try:
            resp = self._command("main.queue")
            return len(resp.split()) if resp else 0
        except Exception as e:
            log.warning("liquidsoap queue query failed: %s", e)
            return -1

    def skip(self) -> bool:
        try:
            self._command("main.skip")
            return True
        except Exception as e:
            log.error("liquidsoap skip failed: %s", e)
            return False

    def alive(self) -> bool:
        try:
            self._command("uptime")
            return True
        except Exception:
            return False
