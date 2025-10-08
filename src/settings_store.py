from __future__ import annotations
import json
import os
import tempfile
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict

from .constants import LOCALAPPDATA, APP_NAME

log = logging.getLogger("hws.settings")

# Speicherort: %LOCALAPPDATA%\HamachiWorkshopSync\settings.json
APP_DIR = Path(LOCALAPPDATA) / APP_NAME
SETTINGS_FILE = APP_DIR / "settings.json"


@dataclass
class UISettings:
    width: int = 1000
    height: int = 700


@dataclass
class HostSettings:
    listen_port: int = 47017
    # Default – kann der Nutzer in der App ändern
    workshop_root: str = r"C:\Program Files (x86)\Steam\steamapps\workshop\content\255710"


@dataclass
class ClientSettings:
    server_ip: str = "25.0.0.1"  # Beispiel Hamachi-IP
    server_port: int = 47017
    # Pfade relativ zu %LOCALAPPDATA% (benutzerunabhängig)
    assets_path: str = str(Path(LOCALAPPDATA) / "Colossal Order" / "Cities_Skylines" / "Addons" / "Assets")
    mods_path: str = str(Path(LOCALAPPDATA) / "Colossal Order" / "Cities_Skylines" / "Addons" / "Mods")


@dataclass
class Settings:
    ui: UISettings = field(default_factory=UISettings)
    host: HostSettings = field(default_factory=HostSettings)
    client: ClientSettings = field(default_factory=ClientSettings)
    # Startmodus (für evtl. spätere CLI/Headless-Nutzung). Aktuell: "gui".
    mode: str = "gui"


def _ensure_appdir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Schreibt JSON atomisch (Temp-Datei -> flush/fsync -> replace)."""
    _ensure_appdir()
    fd, tmp_path = tempfile.mkstemp(prefix="settings_", suffix=".json", dir=str(APP_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def save_settings(settings: Settings) -> None:
    try:
        payload = asdict(settings)
        _atomic_write_json(SETTINGS_FILE, payload)
        log.info("Einstellungen gespeichert: %s", SETTINGS_FILE)
    except Exception as e:
        log.exception("Einstellungen speichern fehlgeschlagen: %s", e)


def _merge_dict(defaults: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Rekursives Merge: fehlende Felder aus defaults ergänzen (Migration)."""
    out = dict(defaults)
    for k, v in current.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> Settings:
    """Lädt Settings; bei Fehlern Defaults + Migration + Auto-Reparatur."""
    defaults = asdict(Settings())

    if not SETTINGS_FILE.exists():
        try:
            save_settings(Settings())
        except Exception:
            pass
        return Settings()

    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.exception("Settings lesen fehlgeschlagen, verwende Defaults: %s", e)
        return Settings()

    # Migration: fehlende Felder auffüllen
    try:
        merged = _merge_dict(defaults, data)
        ui = UISettings(**merged.get("ui", {}))
        host = HostSettings(**merged.get("host", {}))
        client = ClientSettings(**merged.get("client", {}))
        mode = merged.get("mode", "gui")
        settings = Settings(ui=ui, host=host, client=client, mode=mode)

        # Reparierte Datei zurückschreiben, wenn nötig
        if merged != data:
            save_settings(settings)

        return settings
    except Exception as e:
        log.exception("Settings migrieren fehlgeschlagen, verwende Defaults: %s", e)
        return Settings()
