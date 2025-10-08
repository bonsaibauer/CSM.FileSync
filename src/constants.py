from pathlib import Path
import os

APP_NAME = "Hamachi Workshop Sync"
APP_VERSION = "1.2.0"

DEFAULT_PORT = 54821
BUFFER_SIZE = 1024 * 256  # 256 KB
PING_TIMEOUT_SEC = 2

CRP_EXT = ".crp"
DLL_EXT = ".dll"

# Ampel-"Farbnamen" (nur Bezeichner; echte Farben in GUI)
COLOR_RED = "danger"
COLOR_YELLOW = "warning"
COLOR_GREEN = "success"

# Settings/Logs in LOCALAPPDATA (user-unabhängig)
APPDATA_DIR_NAME = "HamachiWorkshopSync"
SETTINGS_FILE_NAME = "settings.json"
LOGS_DIR_NAME = "logs"

# --- Pfad-Basis (userunabhängig) ---
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))

# Default-Pfade
DEFAULT_WORKSHOP = Path(r"C:/Program Files (x86)/Steam/steamapps/workshop/content/255710")
DEFAULT_ASSETS = LOCALAPPDATA / r"Colossal Order/Cities_Skylines/Addons/Assets"
DEFAULT_MODS = LOCALAPPDATA / r"Colossal Order/Cities_Skylines/Addons/Mods"

def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p
