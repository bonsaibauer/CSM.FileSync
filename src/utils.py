import hashlib
import json
import os
import socket
import struct
import subprocess
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from .constants import CRP_EXT, DLL_EXT

log = logging.getLogger("hws.utils")

@dataclass
class FileEntry:
    rel_folder: str   # Workshop-ID (z. B. "05435243")
    name: str         # relativer Dateipfad innerhalb der ID (z. B. "models/myfile.crp" oder "readme.txt")
    size: int
    mtime: float
    sha256: str
    kind: str         # "crp" | "dll" | "other"

def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def is_numbered_dir(p: Path) -> bool:
    # Workshop-IDs sind numerisch (Steam), führende Nullen sind möglich -> isdigit() passt
    return p.is_dir() and p.name.isdigit()

def scan_workshop(workshop_root: Path) -> List[FileEntry]:
    """
    Scannt alle Dateien in allen numerischen Unterordnern (Workshop-IDs).
    Nimmt ALLE Dateien mit – nicht nur .crp/.dll.
    Speichert den relativen Pfad innerhalb der ID in 'name'.
    """
    entries: List[FileEntry] = []
    if not workshop_root.exists():
        log.warning("Workshop-Root existiert nicht: %s", workshop_root)
        return entries
    for sub in workshop_root.iterdir():
        if not is_numbered_dir(sub):
            continue
        for fp in sub.rglob("*"):
            if not fp.is_file():
                continue
            suf = fp.suffix.lower()
            if suf == CRP_EXT:
                kind = "crp"
            elif suf == DLL_EXT:
                kind = "dll"
            else:
                kind = "other"
            try:
                st = fp.stat()
                rel = fp.relative_to(sub).as_posix()  # relativer Pfad mit /-Separator
                fe = FileEntry(
                    rel_folder=sub.name,
                    name=rel,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    sha256=sha256_of_file(fp),
                    kind=kind,
                )
                entries.append(fe)
                log.debug("Gefunden: %s/%s (%s)", fe.rel_folder, fe.name, fe.kind)
            except PermissionError:
                log.exception("Keine Berechtigung für Datei: %s", fp)
    log.info("Scan abgeschlossen: %d Einträge", len(entries))
    return entries

def build_client_target(entry: FileEntry, assets_path: Path, mods_path: Path, dest_base: str) -> Path:
    """
    dest_base: 'assets' oder 'mods'
    Zielpfad: <Basis>/<ID>/<relativer Pfad>
    """
    base = assets_path if dest_base == "assets" else mods_path
    return base / entry.rel_folder / Path(entry.name)
# ---------- Ping ----------

def ping_host(ip: str, timeout_sec: int = 2) -> Tuple[bool, float]:
    count_flag = "-n" if os.name == "nt" else "-c"
    try:
        proc = subprocess.run(["ping", count_flag, "1", ip], capture_output=True, text=True, timeout=timeout_sec)
    except Exception:
        return False, -1.0
    if proc.returncode != 0:
        return False, -1.0
    text = proc.stdout.replace(",", ".")
    rtt_ms = -1.0
    for part in text.split():
        if "ms" in part:
            try:
                val = float(part.replace("ms", ""))
                if val > 0:
                    rtt_ms = val
                    break
            except ValueError:
                pass
    return True, rtt_ms

# ---------- Socket-Utils (Längen-präfixiertes JSON + Binärdateien) ----------

def send_json(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack("!I", len(data)))
    sock.sendall(data)

def recv_json(sock: socket.socket) -> dict:
    raw_len = recvall(sock, 4)
    if not raw_len:
        raise ConnectionError("connection closed")
    (length,) = struct.unpack("!I", raw_len)
    data = recvall(sock, length)
    return json.loads(data.decode("utf-8"))

def recvall(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf

def send_file(sock: socket.socket, path: Path) -> None:
    size = path.stat().st_size
    sock.sendall(struct.pack("!Q", size))
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            sock.sendall(chunk)
    log.debug("Datei gesendet: %s (%d B)", path, size)

def recv_file(sock: socket.socket, dest: Path) -> None:
    raw = recvall(sock, 8)
    (size,) = struct.unpack("!Q", raw)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with dest.open("wb") as f:
        while written < size:
            chunk = sock.recv(min(1024 * 256, size - written))
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    log.debug("Datei empfangen: %s (%d B)", dest, written)

# ---------- Firewall-Helpers ----------

def open_firewall_port(port: int) -> Tuple[bool, str]:
    try:
        name = f"Hamachi Workshop Sync {port}"
        cmd = [
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={name}", "dir=in", "action=allow", "protocol=TCP", f"localport={port}"
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        ok = proc.returncode == 0
        out = (proc.stdout + "\n" + proc.stderr).strip()
        log.info("Firewall-Regel %s: %s", "erstellt" if ok else "FEHLER", out)
        return ok, out
    except Exception as e:
        log.exception("Firewall-Ausnahme fehlgeschlagen")
        return False, str(e)

def ensure_firewall_port(port: int, app_name: str = "HamachiWorkshopSync") -> Tuple[bool, str]:
    rule_name = f"{app_name}_{port}"
    try:
        check_cmd = ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}"]
        res = subprocess.run(check_cmd, capture_output=True, text=True, timeout=8)
        stdout = (res.stdout or "").lower()
        if res.returncode == 0 and rule_name.lower() in stdout:
            msg = f"Firewall-Regel vorhanden: {rule_name}"
            log.info(msg)
            return True, msg

        add_cmd = [
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={rule_name}", "dir=in", "action=allow", "protocol=TCP", f"localport={port}"
        ]
        add = subprocess.run(add_cmd, capture_output=True, text=True, timeout=8)
        if add.returncode == 0:
            msg = f"Firewall-Regel erstellt: {rule_name}"
            log.info(msg)
            return True, msg

        msg = (
            f"Firewall-Regel konnte nicht automatisch gesetzt werden (Code {add.returncode}). "
            "Falls Verbindungen scheitern, bitte einmalig als Administrator starten oder den Button "
            "\"Firewall-Port öffnen\" verwenden."
        )
        log.warning("%s\n%s", msg, (add.stderr or "").strip())
        return False, msg
    except Exception as e:
        msg = f"Firewall-Prüfung fehlgeschlagen: {e}"
        log.warning(msg)
        return False, msg
