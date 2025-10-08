import socket
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Callable, Optional, Set

from .utils import recv_json, send_json, recv_file, build_client_target, FileEntry

log = logging.getLogger("hws.client")

@dataclass
class PlanItem:
    key: str             # "ID/rel/path" oder "(delete)/ID" oder "(delete)/ID/rel/path"
    entry: FileEntry     # für DELETE-Gruppen: rel_folder=ID, name="(folder)"
    target: Path
    action: str          # NEW|UPDATE|SAME|DELETE
    level: str           # "folder" | "file"

class SyncClient:
    def __init__(self, server_ip: str, server_port: int, assets_path: Path, mods_path: Path):
        self.server_ip = server_ip
        self.server_port = server_port
        self.assets_path = assets_path
        self.mods_path = mods_path
        self._sock: Optional[socket.socket] = None
        self._index: Dict[str, dict] = {}   # key: "ID/rel/path" -> meta
        self.on_disconnect: Callable[[str], None] = lambda reason: None

        # Klassifikation pro ID: 'assets' | 'mods' | 'mixed'
        self._folder_class: Dict[str, str] = {}
        # Manuelle Overrides pro ID (nur 'assets' oder 'mods')
        self._folder_override: Dict[str, str] = {}

    # -------- Verbindung --------
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        self.close()
        try:
            self._sock = socket.create_connection((self.server_ip, self.server_port), timeout=5)
            log.info("Verbunden mit %s:%s", self.server_ip, self.server_port)
            idx = recv_json(self._sock)
            self._index = idx.get("entries", {})
            log.info("Index empfangen: %d Einträge", len(self._index))
            # Nach Verbindung sofort Ordner klassifizieren
            self._classify_folders()
            return True
        except Exception as e:
            log.exception("Verbindung fehlgeschlagen: %s", e)
            self._sock = None
            return False

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            finally:
                self._sock = None

    # -------- Klassifikation & Ziele --------
    def _group_index(self) -> Dict[str, List[FileEntry]]:
        groups: Dict[str, List[FileEntry]] = {}
        for _, meta in self._index.items():
            fe = FileEntry(**meta)  # type: ignore[arg-type]
            groups.setdefault(fe.rel_folder, []).append(fe)
        return groups

    def _classify_folders(self):
        """Füllt self._folder_class basierend auf Inhalt je ID."""
        self._folder_class.clear()
        groups = self._group_index()
        for gid, files in groups.items():
            has_crp = any(f.kind == "crp" for f in files)
            has_dll = any(f.kind == "dll" for f in files)
            if has_crp and has_dll:
                self._folder_class[gid] = "mixed"
            elif has_crp:
                self._folder_class[gid] = "assets"
            elif has_dll:
                self._folder_class[gid] = "mods"
            else:
                self._folder_class[gid] = "assets"  # Default, wenn weder crp noch dll
                log.info("Ordner %s enthält keine .crp/.dll – Defaultziel = Assets", gid)

    def get_folder_target(self, gid: str) -> str:
        """Liefert 'assets' oder 'mods' unter Berücksichtigung von Overrides."""
        if gid in self._folder_override:
            return self._folder_override[gid]
        cls = self._folder_class.get(gid, "assets")
        if cls == "mixed":
            # Bis Override festgelegt wird, nimm assets als neutralen Default (wird in GUI überschrieben)
            return "assets"
        return cls

    def set_folder_override(self, gid: str, dest: str) -> None:
        """dest: 'assets' oder 'mods'"""
        if dest in ("assets", "mods"):
            self._folder_override[gid] = dest

    # -------- Planung --------
    def build_plan(self) -> List[PlanItem]:
        plan: List[PlanItem] = []
        groups = self._group_index()

        for gid, files in groups.items():
            dest_base = self.get_folder_target(gid)

            # Aggregation für Folder-Aktion
            statuses: Set[str] = set()
            for e in files:
                t = build_client_target(e, self.assets_path, self.mods_path, dest_base)
                if t.exists():
                    from .utils import sha256_of_file
                    action = "SAME" if sha256_of_file(t) == e.sha256 else "UPDATE"
                else:
                    action = "NEW"
                statuses.add(action)

            if statuses == {"SAME"}:
                f_action = "SAME"
            elif "UPDATE" in statuses:
                f_action = "UPDATE"
            else:
                f_action = "NEW"

            plan.append(PlanItem(
                key=f"{gid}",
                entry=FileEntry(rel_folder=gid, name="(folder)", size=0, mtime=0.0, sha256="", kind="other"),
                target=Path(f"<{dest_base.capitalize()}>/{gid}"),
                action=f_action,
                level="folder",
            ))

            for e in files:
                t = build_client_target(e, self.assets_path, self.mods_path, dest_base)
                if t.exists():
                    from .utils import sha256_of_file
                    action = "SAME" if sha256_of_file(t) == e.sha256 else "UPDATE"
                else:
                    action = "NEW"
                plan.append(PlanItem(
                    key=f"{gid}/{e.name}",
                    entry=e,
                    target=t,
                    action=action,
                    level="file",
                ))

        # DELETE-Kandidaten: lokale IDs, die im Index nicht mehr existieren
        local_ids: Set[str] = set()
        for base in [self.assets_path, self.mods_path]:
            if not base.exists():
                continue
            for id_dir in base.iterdir():
                if id_dir.is_dir() and id_dir.name.isdigit():
                    local_ids.add(id_dir.name)
        groups = self._group_index()
        missing_ids = [gid for gid in local_ids if gid not in groups]
        for gid in missing_ids:
            plan.append(PlanItem(
                key=f"(delete)/{gid}",
                entry=FileEntry(rel_folder=gid, name="(folder)", size=0, mtime=0.0, sha256="", kind="other"),
                target=(self.assets_path / gid),
                action="DELETE",
                level="folder",
            ))
            # Einzeldateien für Anzeige (rekursiv)
            for base in [self.assets_path, self.mods_path]:
                d = base / gid
                if d.exists():
                    for f in d.rglob("*"):
                        if f.is_file():
                            rel = f.relative_to(base / gid).as_posix()
                            plan.append(PlanItem(
                                key=f"(delete)/{gid}/{rel}",
                                entry=FileEntry(rel_folder=gid, name=rel, size=f.stat().st_size, mtime=f.stat().st_mtime,
                                                sha256="", kind=("crp" if f.suffix.lower()==".crp" else ("dll" if f.suffix.lower()==".dll" else "other"))),
                                target=f,
                                action="DELETE",
                                level="file",
                            ))
        log.info("Plan erstellt: %d Elemente (%d Gruppen)", len(plan), len(self._group_index()))
        return plan

    def mixed_folders(self) -> List[str]:
        """Liste aller IDs, deren Inhalt .crp und .dll gemischt enthält."""
        return [gid for gid, cls in self._folder_class.items() if cls == "mixed"]

    # -------- Hilfen --------
    def _classify_file_action(self, fe: FileEntry, dest_base: str) -> str:
        """Ermittelt NEW/UPDATE/SAME für eine einzelne Datei."""
        t = build_client_target(fe, self.assets_path, self.mods_path, dest_base)
        if t.exists():
            from .utils import sha256_of_file
            return "SAME" if sha256_of_file(t) == fe.sha256 else "UPDATE"
        return "NEW"

    def _expand_selection_to_file_entries(self, selected_keys: List[str]) -> List[Tuple[str, FileEntry]]:
        """
        Liefert (dest_base, FileEntry) für alle ausgewählten Dateien.
        - Ordner (ID) werden zu allen enthaltenen Dateien expandiert.
        - Einzeldateien bleiben wie sie sind.
        """
        groups = self._group_index()
        out: List[Tuple[str, FileEntry]] = []
        selected_set = set(selected_keys)

        # Ordner-IDs
        folder_ids = {k for k in selected_set if "/" not in k and not k.startswith("(delete)/")}
        for gid in folder_ids:
            dest_base = self.get_folder_target(gid)
            for e in groups.get(gid, []):
                out.append((dest_base, e))

        # Einzeldateien
        for k in selected_set:
            if "/" in k and not k.startswith("(delete)/"):
                gid, rel = k.split("/", 1)
                meta = self._index.get(k)
                if meta:
                    e = FileEntry(**meta)  # type: ignore[arg-type]
                    dest_base = self.get_folder_target(gid)
                    out.append((dest_base, e))

        # Deduplizieren (gleicher Key kann über Ordner+Datei doppelt kommen)
        seen = set()
        result: List[Tuple[str, FileEntry]] = []
        for dest_base, e in out:
            key = f"{e.rel_folder}/{e.name}"
            if key not in seen:
                seen.add(key)
                result.append((dest_base, e))
        return result

    # -------- Sync (mit ausführlicher Statistik) --------
    def synchronize(self, selected_keys: List[str], progress_cb=lambda c,t: None) -> Dict[str, float]:
        """
        Überträgt nur NEW/UPDATE, zählt SAME als übersprungen.
        Gibt detaillierte Statistik zurück.
        """
        sock = self._sock
        if not sock:
            log.error("Nicht verbunden.")
            return {"files": 0.0, "bytes": 0.0, "seconds": 0.0}

        entries = self._expand_selection_to_file_entries(selected_keys)

        stats = {
            "selected_total": float(len(entries)),
            "to_transfer": 0.0,
            "transferred_files": 0.0,
            "bytes": 0.0,
            "seconds": 0.0,
            "new_count": 0.0,
            "update_count": 0.0,
            "same_count": 0.0,
            "assets_files": 0.0,
            "mods_files": 0.0,
        }

        # Entscheide pro Datei Aktion und baue Request nur für NEW/UPDATE
        request_files: Dict[str, dict] = {}
        action_by_key: Dict[str, str] = {}
        dest_by_key: Dict[str, str] = {}

        for dest_base, e in entries:
            key = f"{e.rel_folder}/{e.name}"
            action = self._classify_file_action(e, dest_base)
            action_by_key[key] = action
            dest_by_key[key] = dest_base
            if action == "SAME":
                stats["same_count"] += 1
                continue
            if action == "NEW":
                stats["new_count"] += 1
            elif action == "UPDATE":
                stats["update_count"] += 1
            request_files[key] = self._index[key]

        stats["to_transfer"] = float(len(request_files))
        started = time.time()

        try:
            # Nichts zu tun?
            if not request_files:
                return stats

            send_json(sock, {"action": "REQUEST_FILES", "files": request_files})
            while True:
                msg = recv_json(sock)
                action = msg.get("action")
                if action == "FILE":
                    rel_folder = msg["rel_folder"]
                    name = msg["name"]  # relativer Pfad innerhalb der ID
                    size = msg["size"]
                    key = f"{rel_folder}/{name}"
                    meta = self._index[key]
                    entry = FileEntry(**meta)  # type: ignore[arg-type]
                    dest_base = dest_by_key.get(key, self.get_folder_target(rel_folder))
                    target = build_client_target(entry, self.assets_path, self.mods_path, dest_base)
                    recv_file(sock, target)
                    # Ziel-Zähler
                    if dest_base == "assets":
                        stats["assets_files"] += 1
                    else:
                        stats["mods_files"] += 1
                    # Gesamt-Zähler
                    stats["bytes"] += size
                    stats["transferred_files"] += 1
                    progress_cb(int(stats["transferred_files"]), int(stats["to_transfer"]))
                    log.info("Empfangen: %s → %s", name, target)
                elif action == "DONE":
                    break
        except Exception as e:
            log.exception("Verbindung verloren während Synchronisierung: %s", e)
            self.close()
            self.on_disconnect("Sync unterbrochen – Verbindung verloren")

        stats["seconds"] = time.time() - started
        return stats

    # -------- Löschen (robust) --------
    def delete_local(self, delete_keys: List[str]) -> tuple[int, int]:
        """
        Löscht ganze IDs (Ordner) oder einzelne Dateien.
        Akzeptiert Keys in den Formen:
          - "(delete)/ID"
          - "(delete)/ID/rel/path"
        (robust auch ohne Präfix: "ID", "ID/rel/path")

        Rückgabe: (files_deleted, folders_deleted)
        """
        files_deleted = 0
        folders_deleted = 0

        # --- Normalisieren ---
        norm_ids: set[str] = set()
        file_targets: list[tuple[str, str]] = []

        for k in delete_keys:
            if k.startswith("(delete)/"):
                tail = k.split("/", 1)[1]
                if "/" in tail:
                    gid, rel = tail.split("/", 1)
                    if gid.isdigit() and rel:
                        file_targets.append((gid, rel))
                else:
                    if tail.isdigit():
                        norm_ids.add(tail)
            else:
                if "/" in k:
                    gid, rel = k.split("/", 1)
                    if gid.isdigit() and rel:
                        file_targets.append((gid, rel))
                else:
                    if k.isdigit():
                        norm_ids.add(k)

        # --- 1) Ganze ID-Ordner löschen (Assets und Mods) ---
        for gid in sorted(norm_ids):
            for base in [self.assets_path, self.mods_path]:
                d = base / gid
                if d.exists() and d.is_dir():
                    for f in d.rglob("*"):
                        if f.is_file():
                            try:
                                f.unlink()
                                files_deleted += 1
                                log.info("Gelöscht: %s", f)
                            except Exception:
                                log.exception("Datei konnte nicht gelöscht werden: %s", f)
                    try:
                        d.rmdir()
                        folders_deleted += 1
                        log.info("Ordner gelöscht: %s", d)
                    except Exception:
                        # Unterordner noch vorhanden? dann bleibt er stehen
                        pass

        # --- 2) Einzeldateien löschen (Ordner bleibt bestehen) ---
        for gid, rel in file_targets:
            deleted_here = False
            for base in [self.assets_path, self.mods_path]:
                p = base / gid / Path(rel)
                if p.exists() and p.is_file():
                    try:
                        p.unlink()
                        files_deleted += 1
                        deleted_here = True
                        log.info("Gelöscht: %s", p)
                        break
                    except Exception:
                        log.exception("Löschen fehlgeschlagen: %s", p)
            if not deleted_here:
                log.warning("Datei nicht gefunden zum Löschen: %s/%s (Assets/Mods)", gid, rel)

        return files_deleted, folders_deleted
