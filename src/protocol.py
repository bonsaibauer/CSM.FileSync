from pathlib import Path
from typing import Dict
from .utils import FileEntry, scan_workshop

# INDEX: Host → Client, enthält alle FileEntry
def build_index(workshop_root: Path) -> dict:
    entries = scan_workshop(workshop_root)
    grouped: Dict[str, dict] = {
        e.rel_folder + "/" + e.name: {
            "rel_folder": e.rel_folder,
            "name": e.name,
            "size": e.size,
            "mtime": e.mtime,
            "sha256": e.sha256,
            "kind": e.kind,
        } for e in entries
    }
    return {"action": "INDEX", "entries": grouped}
