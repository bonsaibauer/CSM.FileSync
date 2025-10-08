import threading
import time
import tkinter as tk
from tkinter import messagebox, filedialog
from pathlib import Path
from typing import List, Dict

import ttkbootstrap as ttk
from ttkbootstrap.constants import *

from .constants import APP_NAME, APP_VERSION, COLOR_GREEN, COLOR_RED, COLOR_YELLOW, LOCALAPPDATA
from .utils import ping_host, open_firewall_port, ensure_firewall_port
from .client import SyncClient, PlanItem
from .settings_store import save_settings

# -------- Log- und UI-Widgets --------

class LogWidget(ttk.ScrolledText):
    def __init__(self, master):
        super().__init__(master, height=12)  # sichtbar, nicht eingeklappt
        self.configure(state=tk.DISABLED, font=("Consolas", 10))

    def write(self, msg: str):
        self.configure(state=tk.NORMAL)
        self.insert(tk.END, msg + "\n")
        self.see(tk.END)
        self.configure(state=tk.DISABLED)

class Ampel(ttk.Label):
    def __init__(self, master):
        super().__init__(master, text="●", font=("Segoe UI", 18))
        self.set_color(COLOR_RED)
    def set_color(self, style):
        colors = {COLOR_GREEN: "#28a745", COLOR_YELLOW: "#ffc107", COLOR_RED: "#dc3545"}
        self.configure(foreground=colors.get(style, "#dc3545"))

class PlanTree(ttk.Treeview):
    COLS = ("include", "action", "target")
    def __init__(self, master):
        super().__init__(master, columns=self.COLS, show="tree headings", height=18, bootstyle="secondary")
        self.heading("#0", text="Workshop-ID / Datei (relativ)")
        self.heading("include", text="✔")
        self.heading("action", text="Aktion")
        self.heading("target", text="Ziel")

        # Spaltenbreiten: Aktion schmaler, Ziel breiter
        self.column("#0", width=420, stretch=True)
        self.column("include", width=50, anchor=tk.CENTER, stretch=False)
        self.column("action", width=90, stretch=False)
        self.column("target", width=900, stretch=True)

        self._include: Dict[str, bool] = {}
        self.bind("<Double-1>", self._toggle_on_dclick)

    def clear(self):
        for i in self.get_children(""):
            self.delete(i)
        self._include.clear()

    def add_folder(self, gid: str, action: str):
        iid = self.insert("", tk.END, text=gid, values=("✔", action, ""), open=True)
        self._include[iid] = True
        return iid

    def add_file(self, parent_iid: str, key: str, rel_path: str, action: str, target: Path):
        iid = self.insert(parent_iid, tk.END, text=rel_path, values=("✔", action, str(target)))
        self._include[iid] = True
        self.item(iid, tags=(key,))  # tag als key-träger
        return iid

    def set_all(self, include: bool):
        for iid in list(self._include.keys()):
            self._include[iid] = include
            vals = list(self.item(iid, "values"))
            vals[0] = "✔" if include else "✖"
            self.item(iid, values=vals)

    def _toggle_on_dclick(self, event):
        iid = self.identify_row(event.y)
        if not iid:
            return
        new = not self._include.get(iid, True)
        self._set_include_recursive(iid, new)
        # Elternzustand anpassen: wenn alle Kinder off -> off, wenn mind. eins on -> on
        parent = self.parent(iid)
        while parent:
            on_children = any(self._include.get(c, False) for c in self.get_children(parent))
            self._include[parent] = on_children
            vals = list(self.item(parent, "values"))
            vals[0] = "✔" if on_children else "✖"
            self.item(parent, values=vals)
            parent = self.parent(parent)

    def _set_include_recursive(self, iid: str, include: bool):
        self._include[iid] = include
        vals = list(self.item(iid, "values"))
        vals[0] = "✔" if include else "✖"
        self.item(iid, values=vals)
        for child in self.get_children(iid):
            self._set_include_recursive(child, include)

    def selected_keys(self) -> List[str]:
        """
        Gemischte Auswahl:
        - Folder-Keys = "ID"
        - File-Keys   = "ID/rel/path"
        """
        keys: List[str] = []
        for iid, inc in self._include.items():
            if not inc:
                continue
            parent = self.parent(iid)
            if not parent:
                # folder
                keys.append(self.item(iid, "text"))
            else:
                # file
                gid = self.item(parent, "text")
                rel = self.item(iid, "text")
                keys.append(f"{gid}/{rel}")
        return keys

# -------- Auto-Reconnect nur bei Verbindungsverlust --------

class AutoReconnectManager:
    def __init__(self, app:"App", interval_sec:int=10):
        self.app = app
        self.interval = interval_sec
        self._stop = threading.Event()
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._running = False

    def _run(self):
        self.app._log_write(f"Verbindung verloren – Auto-Reconnect alle {self.interval}s …")
        while not self._stop.is_set():
            if self.app.client.is_connected():
                self.app._log_write("Neu verbunden – Auto-Reconnect beendet.")
                self._running = False
                return
            ok = self.app.client.connect()
            if ok:
                plan = self.app.client.build_plan()
                self.app._set_plan(plan)
                self.app.btn_sync.configure(state=NORMAL)
                self.app.btn_delete.configure(state=NORMAL)
                self._log_write("Neu verbunden. Vorschau aktualisiert.")
                self._running = False
                return
            self.app.status.configure(text="Auto-Reconnect läuft …")
            time.sleep(self.interval)
        self._running = False
        self._log_write("Auto-Reconnect gestoppt.")

# -------- Haupt-App --------

class App(ttk.Window):
    def __init__(self, settings):
        super().__init__(themename="flatly")
        self.settings = settings
        self.title(f"{APP_NAME} {APP_VERSION}")

        # *** Fenster doppelt so breit ***
        target_width = max(self.settings.ui.width * 2, 1600)
        target_height = max(self.settings.ui.height, 900)
        self.geometry(f"{int(target_width)}x{int(target_height)}")
        self.minsize(1400, 800)
        self.resizable(True, True)

        # -------- Splitter: oben Inhalt (Tabs), unten Log --------
        self.pw = ttk.PanedWindow(self, orient="vertical")
        self.pw.pack(fill=BOTH, expand=True)

        self.top_frame = ttk.Frame(self.pw)
        self.pw.add(self.top_frame, weight=3)  # oben größer

        self.bottom_frame = ttk.Frame(self.pw)
        self.pw.add(self.bottom_frame, weight=1)  # unten sichtbar

        # Tabs oben
        self.nb = ttk.Notebook(self.top_frame)
        self.nb.pack(fill=BOTH, expand=True)

        # Client-UI
        self.client_frame = ttk.Frame(self.nb); self.nb.add(self.client_frame, text="Client")
        header = ttk.Frame(self.client_frame); header.pack(fill=X, padx=12, pady=8)
        ttk.Label(header, text="Host IP:").pack(side=LEFT)
        self.ip_var = tk.StringVar(value=self.settings.client.server_ip)
        ttk.Entry(header, textvariable=self.ip_var, width=18).pack(side=LEFT, padx=(6,12))
        ttk.Label(header, text="Port:").pack(side=LEFT)
        self.port_var = tk.IntVar(value=self.settings.client.server_port)
        ttk.Entry(header, textvariable=self.port_var, width=8).pack(side=LEFT, padx=(6,12))
        ttk.Button(header, text="Ping", command=self._do_ping, bootstyle=SECONDARY).pack(side=LEFT)
        self.ampel = Ampel(header); self.ampel.pack(side=LEFT, padx=8)
        ttk.Button(header, text="Verbinden", command=self._connect, bootstyle=PRIMARY).pack(side=LEFT, padx=8)
        self.btn_sync = ttk.Button(header, text="Synchronisieren", command=self._sync, bootstyle=SUCCESS, state=DISABLED)
        self.btn_sync.pack(side=LEFT)
        self.btn_delete = ttk.Button(header, text="Löschen", command=self._delete, bootstyle=DANGER, state=DISABLED)
        self.btn_delete.pack(side=LEFT, padx=(8,0))
        ttk.Button(header, text="Alle ✔", command=lambda: self.plan.set_all(True), bootstyle=INFO).pack(side=LEFT, padx=(12,0))
        ttk.Button(header, text="Alle ✖", command=lambda: self.plan.set_all(False), bootstyle=WARNING).pack(side=LEFT)

        # Pfade (LOCALAPPDATA als sichere Basis)
        paths = ttk.Labelframe(self.client_frame, text="Zielpfade (Client)")
        paths.pack(fill=X, padx=12, pady=4)
        self.assets_var = tk.StringVar(value=self.settings.client.assets_path)
        self.mods_var = tk.StringVar(value=self.settings.client.mods_path)
        self._row_path(paths, "Assets", self.assets_var)
        self._row_path(paths, "Mods", self.mods_var)

        # Vorschau (Tree: ID -> Dateien)
        ttk.Label(self.client_frame, text="Vorschau (ID → relative Dateien)").pack(anchor=W, padx=12)
        self.plan = PlanTree(self.client_frame)
        self.plan.pack(fill=BOTH, expand=True, padx=12, pady=(0,8))

        # Host-UI
        self.host_frame = ttk.Frame(self.nb); self.nb.add(self.host_frame, text="Host")
        hrow = ttk.Frame(self.host_frame); hrow.pack(fill=X, padx=12, pady=8)
        ttk.Label(hrow, text="Listen-Port:").pack(side=LEFT)
        self.host_port = tk.IntVar(value=self.settings.host.listen_port)
        ttk.Entry(hrow, textvariable=self.host_port, width=8).pack(side=LEFT, padx=(6,12))
        ttk.Button(hrow, text="Server starten", command=self._host_start, bootstyle=PRIMARY).pack(side=LEFT)
        ttk.Button(hrow, text="Server stoppen", command=self._host_stop, bootstyle=SECONDARY).pack(side=LEFT, padx=8)
        ttk.Button(hrow, text="Firewall-Port öffnen", command=self._host_firewall_button, bootstyle=INFO).pack(side=LEFT)

        wpaths = ttk.Labelframe(self.host_frame, text="Workshop-Quelle (Host)")
        wpaths.pack(fill=X, padx=12, pady=4)
        self.host_workshop = tk.StringVar(value=self.settings.host.workshop_root)
        self._row_path(wpaths, "Workshop", self.host_workshop)

        # -------- Unten: Protokoll-Bereich dauerhaft sichtbar --------
        bottom_header = ttk.Frame(self.bottom_frame)
        bottom_header.pack(fill=X, padx=12, pady=(6,0))
        ttk.Label(bottom_header, text="Protokoll").pack(anchor=W)

        self.log = LogWidget(self.bottom_frame)
        self.log.pack(fill=BOTH, expand=True, padx=12, pady=(0,12))

        # Statusleiste (ganz unten)
        self.status = ttk.Label(self, text="Bereit", anchor=W)
        self.status.pack(fill=X, side=BOTTOM)

        # Client-Objekt
        self.client = SyncClient(
            self.ip_var.get().strip(),
            int(self.port_var.get()),
            Path(self.assets_var.get()), Path(self.mods_var.get())
        )
        self.after(200, lambda: setattr(self.client, "on_disconnect", self._on_disconnect))
        self._reconnector = AutoReconnectManager(self, interval_sec=10)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- Helpers -----
    def _row_path(self, parent, label, var: tk.StringVar):
        row = ttk.Frame(parent); row.pack(fill=X, pady=6)
        ttk.Label(row, text=f"{label}:", width=10).pack(side=LEFT)
        ttk.Entry(row, textvariable=var).pack(side=LEFT, fill=X, expand=True, padx=(0,8))
        def browse():
            init_dir = var.get() or str(LOCALAPPDATA)
            d = filedialog.askdirectory(title=f"{label}-Ordner wählen", mustexist=True, initialdir=init_dir)
            if d:
                var.set(d)
                self._save_settings()
        ttk.Button(row, text="Durchsuchen…", command=browse, bootstyle=SECONDARY).pack(side=LEFT)
        var.trace_add("write", lambda *args: self._save_settings())

    def _log_write(self, msg: str):
        self.log.write(msg)
        self.status.configure(text=msg)

    def _set_plan(self, plan_items: List[PlanItem]):
        self.plan.clear()
        folders: Dict[str, List[PlanItem]] = {}
        folder_actions: Dict[str, str] = {}
        for it in plan_items:
            if it.level == "folder":
                folder_actions[it.entry.rel_folder] = it.action
            else:
                folders.setdefault(it.entry.rel_folder, []).append(it)
        for gid, files in sorted(folders.items()):
            f_action = folder_actions.get(gid, "NEW")
            parent_iid = self.plan.add_folder(gid, f_action)
            for it in files:
                # it.entry.name ist relativer Pfad innerhalb der ID
                self.plan.add_file(parent_iid, it.key, it.entry.name, it.action, it.target)
        self._log_write("Vorschau aktualisiert.")

    # ----- Aktionen -----
    def _do_ping(self):
        ip = self.ip_var.get().strip()
        def run():
            ok, rtt = ping_host(ip)
            if ok:
                style = COLOR_GREEN if rtt < 60 else (COLOR_YELLOW if rtt < 150 else COLOR_RED)
                self.ampel.set_color(style)
                self._log_write(f"Ping {ip}: {rtt:.0f} ms")
            else:
                self.ampel.set_color(COLOR_RED)
                self._log_write(f"Ping {ip}: keine Antwort")
        threading.Thread(target=run, daemon=True).start()

    def _prompt_mixed_folders(self, gids: List[str]) -> None:
        """Fragt für jede ID mit .crp UND .dll das Ziel ab (Assets/Mods) und setzt Override im Client."""
        for gid in gids:
            ans = messagebox.askyesno(
                "Gemischter Ordner",
                f"Ordner {gid} enthält sowohl .crp als auch .dll.\n"
                f"Soll dieser Ordner nach **Assets** kopiert werden?\n\n"
                f"Ja = Assets   |   Nein = Mods"
            )
            self.client.set_folder_override(gid, "assets" if ans else "mods")
            self._log_write(f"Ziel für {gid}: {'Assets' if ans else 'Mods'}")

    def _connect(self):
        self._reconnector.stop()
        self.client.server_ip = self.ip_var.get().strip()
        self.client.server_port = int(self.port_var.get())
        self.client.assets_path = Path(self.assets_var.get())
        self.client.mods_path = Path(self.mods_var.get())
        def run():
            if self.client.connect():
                # Mixed-Folder abfragen und Overrides setzen
                mixed = self.client.mixed_folders()
                if mixed:
                    self._prompt_mixed_folders(mixed)
                plan = self.client.build_plan()
                self._set_plan(plan)
                self.btn_sync.configure(state=NORMAL)
                self.btn_delete.configure(state=NORMAL)
        threading.Thread(target=run, daemon=True).start()

    def _sync(self):
        keys = self.plan.selected_keys()
        if not keys:
            messagebox.showwarning("Hinweis", "Keine Einträge ausgewählt.")
            return
        def run():
            stats = self.client.synchronize(
                keys,
                progress_cb=lambda c,t: self.status.configure(text=f"Übertragung: {c}/{t}")
            )
            if self.client.is_connected():
                # Dialog nur kurz & bündig
                messagebox.showinfo("Synchronisierung", "Synchronisierung abgeschlossen.")

                # Ausführliche Statistik ins Protokoll
                kb = stats["bytes"] / 1024
                lines = [
                    "=== Synchronisierung abgeschlossen ===",
                    f"Ausgewählt gesamt:       {int(stats['selected_total'])}",
                    f"Übertragen (NEW+UPDATE): {int(stats['to_transfer'])}",
                    f"  • Neu kopiert (NEW):   {int(stats['new_count'])}",
                    f"  • Aktualisiert (UPD):  {int(stats['update_count'])}",
                    f"Übersprungen (SAME):     {int(stats['same_count'])}",
                    "Ziel-Zusammenfassung:",
                    f"  • nach Assets:         {int(stats['assets_files'])}",
                    f"  • nach Mods:           {int(stats['mods_files'])}",
                    f"Übertragene Daten:       {kb:.1f} KiB",
                    f"Dauer:                   {stats['seconds']:.1f} s",
                    "=====================================",
                ]
                for line in lines:
                    self._log_write(line)
        threading.Thread(target=run, daemon=True).start()

    def _delete(self):
        keys = self.plan.selected_keys()
        if not keys:
            messagebox.showinfo("Löschen", "Nichts ausgewählt.")
            return

        # Normalisieren:
        #  - ID           -> (delete)/ID
        #  - ID/rel/path  -> (delete)/ID/rel/path
        del_keys: List[str] = []
        for k in keys:
            if k.startswith("(delete)/"):
                del_keys.append(k)
            else:
                if "/" in k:
                    del_keys.append(f"(delete)/{k}")
                else:
                    if k.isdigit():
                        del_keys.append(f"(delete)/{k}")

        if not del_keys:
            messagebox.showinfo("Löschen", "Keine gültigen Einträge ausgewählt.")
            return

        if not messagebox.askyesno("Löschen bestätigen",
                                   f"Sollen {len(del_keys)} Einträge wirklich gelöscht werden?"):
            return

        def run():
            files_deleted, folders_deleted = self.client.delete_local(del_keys)
            self._log_write(f"Gelöscht: {files_deleted} Dateien, {folders_deleted} Ordner")
        threading.Thread(target=run, daemon=True).start()

    # ----- Host-Steuerung -----
    def _host_firewall_button(self):
        ok, out = open_firewall_port(int(self.host_port.get()))
        if ok:
            messagebox.showinfo("Firewall", "Portfreigabe erfolgreich.\n\n" + out)
        else:
            messagebox.showerror("Firewall", "Portfreigabe fehlgeschlagen.\n\n" + out)

    def _host_start(self):
        port = int(self.host_port.get())
        ok, msg = ensure_firewall_port(port)
        self._log_write(msg)
        from .server import SyncServer
        self.settings.host.listen_port = port
        self.settings.host.workshop_root = self.host_workshop.get().strip()
        save_settings(self.settings)
        self.server = SyncServer(Path(self.settings.host.workshop_root), port=port)
        self.server.start()
        self._log_write(f"Host-Server gestartet (Port {port}, Workshop {self.settings.host.workshop_root})")

    def _host_stop(self):
        if hasattr(self, 'server') and self.server:
            self.server.stop()
            self._log_write("Host-Server gestoppt.")

    # ----- Settings -----
    def _save_settings(self):
        """Live-Updates (on change). Nicht kritisch – der Schluss-Save in _on_close ist maßgeblich."""
        try:
            self.settings.client.server_ip = self.ip_var.get().strip()
            try:
                self.settings.client.server_port = int(self.port_var.get())
            except Exception:
                pass
            self.settings.client.assets_path = self.assets_var.get().strip()
            self.settings.client.mods_path = self.mods_var.get().strip()
            try:
                self.settings.host.listen_port = int(self.host_port.get())
            except Exception:
                pass
            self.settings.host.workshop_root = self.host_workshop.get().strip()
            save_settings(self.settings)
        except Exception:
            # kein Popup hier – stilles Logging genügt
            pass

    def _on_close(self):
        """Beim Schließen ALLES final auslesen und atomisch speichern."""
        try:
            # Letzte Geometry von Tk aktualisieren und abfragen
            self.update_idletasks()
            w, h = self.winfo_width(), self.winfo_height()
            self.settings.ui.width = int(w)
            self.settings.ui.height = int(h)

            # Aktuelle Eingabefelder final übernehmen
            self.settings.client.server_ip = self.ip_var.get().strip()
            try:
                self.settings.client.server_port = int(self.port_var.get())
            except Exception:
                pass
            self.settings.client.assets_path = self.assets_var.get().strip()
            self.settings.client.mods_path = self.mods_var.get().strip()
            try:
                self.settings.host.listen_port = int(self.host_port.get())
            except Exception:
                pass
            self.settings.host.workshop_root = self.host_workshop.get().strip()

            # Einmal zentral & atomisch speichern
            save_settings(self.settings)
        finally:
            # Threads stoppen & Fenster schließen
            if hasattr(self, "_reconnector"):
                self._reconnector.stop()
            self.destroy()
