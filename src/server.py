import socket
import threading
import logging
from pathlib import Path

from .constants import DEFAULT_PORT
from .utils import send_json, recv_json, send_file, ensure_firewall_port
from .protocol import build_index

log = logging.getLogger("hws.server")

class SyncServer:
    def __init__(self, workshop_root: Path, port: int = DEFAULT_PORT):
        self.workshop_root = workshop_root
        self.port = port
        self._srv: socket.socket | None = None
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass

    def _run(self):
        # Best-effort Firewall-Freigabe
        ok, msg = ensure_firewall_port(self.port)
        log.info("Firewall: %s", msg)

        log.info("Server lauscht auf Port %s…", self.port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(5)
            self._srv = srv
            while not self._stop.is_set():
                try:
                    cli, addr = srv.accept()
                except OSError:
                    break
                log.info("Client verbunden: %s", addr)
                threading.Thread(target=self._handle_client, args=(cli,), daemon=True).start()

    def _handle_client(self, cli: socket.socket):
        with cli:
            idx = build_index(self.workshop_root)
            send_json(cli, idx)
            log.info("Index gesendet: %d Einträge", len(idx['entries']))
            while True:
                try:
                    msg = recv_json(cli)
                except Exception:
                    log.info("Client getrennt.")
                    break
                action = msg.get("action")
                if action == "REQUEST_FILES":
                    files = msg.get("files", {})
                    log.info("Angeforderte Dateien: %d", len(files))
                    for _, meta in files.items():
                        rel_folder = meta["rel_folder"]
                        name = meta["name"]
                        path = Path(self.workshop_root) / rel_folder / name
                        log.debug("Sende %s/%s (%d B)", rel_folder, name, path.stat().st_size)
                        send_json(cli, {"action": "FILE", "rel_folder": rel_folder, "name": name, "size": path.stat().st_size})
                        send_file(cli, path)
                    send_json(cli, {"action": "DONE"})
                else:
                    log.warning("Unbekannte Aktion: %s", action)
