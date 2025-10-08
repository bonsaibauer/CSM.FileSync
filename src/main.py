from __future__ import annotations
import logging
import sys

from .settings_store import load_settings, save_settings
from .gui import App

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    )

    settings = load_settings()

    # Backward-Compatibility: falls alte settings.json kein 'mode' hatte
    if not hasattr(settings, "mode"):
        settings.mode = "gui"
        try:
            save_settings(settings)
        except Exception:
            pass

    # Aktuell immer GUI-Start
    app = App(settings)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        # Sauber beenden, falls per Konsole abgebrochen
        try:
            app._on_close()
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())
