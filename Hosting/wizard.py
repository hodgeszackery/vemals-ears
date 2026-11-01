"""Desktop setup and service controls for a Vemal's Ears host."""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
BG = "#0b0b0d"
PANEL = "#171719"
FIELD = "#222124"
BORDER = "#494039"
TEXT = "#f5eee2"
MUTED = "#c0b4a1"
GOLD = "#e4bd6b"
RED = "#f18c94"
GREEN = "#92d4a5"
DISCORD_DASHBOARD = "https://discord.com/developers/applications"
CLOUDFLARE_DASHBOARD = "https://one.dash.cloudflare.com/"


def public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == "https" and parsed.hostname
                    and not parsed.username and not parsed.password
                    and not any(ord(char) < 33 for char in url))
    except ValueError:
        return False


def validate_server(settings: dict, *, require_token=True) -> str | None:
    hostname = str(settings.get("hostname", "")).strip()
    if not hostname or "." not in hostname or "/" in hostname or ":" in hostname:
        return "Enter your public hostname, such as ears.example.org, without https://."
    for key, label in (("guild_id", "Discord server ID"), ("application_id", "Application ID")):
        value = str(settings.get(key, "")).strip()
        if not value.isascii() or not value.isdigit() or not 17 <= len(value) <= 20:
            return label + " must contain 17 to 20 digits."
    if not str(settings.get("guild_name", "")).strip():
        return "Enter a name for your host."
    name = str(settings["guild_name"]).strip()
    if len(name) > 80 or any(ord(char) < 32 or ord(char) > 126 for char in name):
        return "Use a host name with 1 to 80 printable ASCII characters."
    try:
        port = int(settings.get("port", 8770))
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        return "The local relay port must be a number between 1 and 65535."
    if require_token and not str(settings.get("bot_token", "")).strip():
        return "Enter the bot token from your own Discord application."
    return None


def validate_connection(settings: dict, *, require_tunnel_token=True) -> str | None:
    if (require_tunnel_token and settings.get("tunnel_enabled")
            and not str(settings.get("tunnel_token", "")).strip()):
        return "Enter your Cloudflare tunnel token, or choose existing HTTPS forwarding."
    if settings.get("client_installer"):
        path = Path(settings["client_installer"])
        if path.suffix.lower() != ".exe" or not path.is_file():
            return "Choose the Vemal's Ears member installer (.exe) supplied by the project."
    elif not public_url(str(settings.get("client_download_url", ""))):
        return "Choose the member installer file or enter its public HTTPS download link."
    return None


def prepare_host(backend, settings: dict, progress, configured, *, configure_existing=False) -> dict:
    """The retry path reuses durable configuration before downloading anything."""
    current = backend.read_settings()
    if configure_existing or not current.get("configured"):
        progress("Saving your host settings and protecting its private keys...")
        backend.configure(settings)
        current = backend.read_settings()
    configured(current)
    progress("Preparing NVIDIA speech recognition. The first download is about 1.2 GB...")
    backend.prepare(progress)
    progress("Installing and starting your host services...")
    backend.install_services()
    progress("Checking the relay and caption worker...")
    return {"settings": backend.read_settings(), "status": backend.status()}


class HostWizard:
    def __init__(self, root: tk.Tk, backend, *, open_url=webbrowser.open, takeover_backend=None):
        self.root = root
        self.backend = backend
        self.open_url = open_url
        self.events = queue.Queue()
        self.busy = False
        self.closed = False
        self.configured = False
        self.editing = False
        self.settings = {}
        self.page = "loading"
        self.secrets = set()
        self.retry_action = None
        self.completion = None
        self.current_status = {}
        self.values = {
            key: tk.StringVar(root, value=default)
            for key, default in (
                ("guild_name", "Vemal's Ears"), ("hostname", ""),
                ("guild_id", ""), ("application_id", ""), ("bot_token", ""),
                ("port", "8770"),
                ("client_installer", ""), ("client_download_url", ""),
                ("tunnel_token", ""), ("installer_mode", "file"),
                ("tunnel_mode", "cloudflare"),
            )
        }
        from Hosting.takeover_ui import TakeoverUi
        self.takeover_ui = TakeoverUi(self, takeover_backend)
        self.error_back = self.show_connection
        self.notice = tk.StringVar(root)
        self.progress_text = tk.StringVar(root, value="Reading your host settings...")
        self.root.title("Vemal's Ears - Host")
        self.root.configure(bg=BG)
        self.root.geometry(str(min(840, self.root.winfo_screenwidth() - 80)) + "x"
                           + str(min(760, max(560, self.root.winfo_screenheight() - 120))))
        self.root.minsize(680, 560)
        self.root.option_add("*Font", "{Segoe UI} 10")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        icon = ROOT / "Windows" / "VemalsEars.ico"
        if icon.is_file():
            try:
                self.root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        self._style()
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=30, pady=(24, 14))
        mark = tk.Label(header, text="VE", font=("Segoe UI", 19, "bold"),
                        bg=GOLD, fg=BG, width=3, pady=6)
        mark.pack(side="left", padx=(0, 14))
        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left")
        self._label(titles, "Vemal's Ears", size=22, bold=True).pack(anchor="w")
        self._label(titles, "HOST  /  BY DOWNTIME", size=9, color=MUTED).pack(anchor="w", pady=(2, 0))
        self.step_label = self._label(root, "", size=10, color=GOLD)
        self.step_label.pack(anchor="w", padx=30, pady=(0, 12))
        self.content = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.content.pack(fill="both", expand=True, padx=30)
        self.canvas = tk.Canvas(self.content, bg=PANEL, bd=0, highlightthickness=0,
                                yscrollincrement=24)
        self.scrollbar = ttk.Scrollbar(self.content, orient="vertical", command=self.canvas.yview,
                                        style="Host.Vertical.TScrollbar")
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=PANEL)
        self.body_window = self.canvas.create_window(24, 20, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._body_resized)
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.notice_label = tk.Label(root, textvariable=self.notice, bg=BG, fg=RED,
                                     anchor="w", justify="left", wraplength=760)
        self.notice_label.pack(fill="x", padx=30, pady=(12, 0))
        self.navigation = tk.Frame(root, bg=BG)
        self.navigation.pack(fill="x", padx=30, pady=(12, 16))
        self._label(root, "Your computer stays online. You do not need to stay in voice.",
                    size=9, color=MUTED).pack(pady=(0, 16))
        self.root.bind("<Configure>", self._resize_notice, add="+")
        self.root.bind("<MouseWheel>", self._mousewheel, add="+")
        self.root.bind("<FocusIn>", self._reveal_focus, add="+")
        self._show_progress("Opening your host", "Reading your saved settings...")
        self._start_job(lambda progress: self.backend.read_settings(), self._loaded,
                        retry=lambda: self._load_again())
        self.poll_id = self.root.after(60, self._drain_events)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Host.Horizontal.TProgressbar", background=GOLD,
                        troughcolor=FIELD, bordercolor=FIELD, lightcolor=GOLD, darkcolor=GOLD)
        style.configure("Host.Vertical.TScrollbar", background=BORDER, troughcolor=PANEL,
                        bordercolor=PANEL, lightcolor=BORDER, darkcolor=BORDER,
                        arrowcolor=MUTED, arrowsize=12)

    def _label(self, parent, text, *, size=10, color=TEXT, bold=False, wrap=None):
        label = tk.Label(parent, text=text, fg=color, bg=parent.cget("bg"), anchor="w",
                         justify="left", font=("Segoe UI", size, "bold" if bold else "normal"),
                         wraplength=wrap or 700)
        parent.bind("<Configure>", lambda event: label.configure(wraplength=min(wrap or 700, max(100, event.width)))
                    if event.widget is parent and label.winfo_exists() else None, add="+")
        return label

    def _button(self, parent, text, command, *, primary=False, disabled=False):
        button = tk.Button(parent, text=text, command=command, relief="flat", bd=0,
                           padx=18, pady=10, cursor="hand2", takefocus=True,
                           bg=GOLD if primary else FIELD, fg=BG if primary else TEXT,
                           activebackground="#f2d59b" if primary else "#363035",
                           activeforeground=BG if primary else TEXT,
                           disabledforeground=MUTED, highlightthickness=1,
                           highlightbackground=GOLD if primary else BORDER,
                           highlightcolor=GOLD, font=("Segoe UI", 10, "bold"))
        if disabled:
            button.configure(state="disabled")
        return button

    def _link(self, parent, label, url):
        button = tk.Button(parent, text=label, command=lambda: self._open(url), relief="flat",
                           bd=0, padx=0, pady=3, cursor="hand2", fg=GOLD,
                           bg=parent.cget("bg"), activebackground=parent.cget("bg"),
                           activeforeground=TEXT, font=("Segoe UI", 10, "underline"),
                           highlightthickness=1, highlightbackground=parent.cget("bg"),
                           highlightcolor=GOLD, takefocus=True)
        return button

    def _field(self, parent, key, label, *, secret=False, hint=None):
        frame = tk.Frame(parent, bg=PANEL)
        self._label(frame, label, size=10, bold=True).pack(anchor="w", pady=(0, 6))
        entry = tk.Entry(frame, textvariable=self.values[key], bg=FIELD, fg=TEXT,
                         insertbackground=GOLD, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=GOLD, font=("Segoe UI", 11),
                         show="*" if secret else "")
        entry.pack(fill="x", ipady=8)
        if hint:
            self._label(frame, hint, size=9, color=MUTED).pack(anchor="w", pady=(5, 0))
        return frame, entry

    def _clear(self, page, step):
        if hasattr(self, "progress_bar"):
            try:
                self.progress_bar.stop()
            except tk.TclError:
                pass
        self.page = page
        self.notice.set("")
        for child in self.body.winfo_children():
            child.destroy()
        for child in self.navigation.winfo_children():
            child.destroy()
        self.step_label.configure(text=step)
        self.canvas.yview_moveto(0)

    def _heading(self, title, description):
        self._label(self.body, title, size=19, bold=True).pack(anchor="w")
        self._label(self.body, description, color=MUTED).pack(anchor="w", pady=(8, 20))

    def _loaded(self, settings):
        self._accept_settings(settings)
        if self.configured:
            self.show_dashboard({"detail": "Checking your host..."})
            self.refresh_status()
        else:
            self.takeover_ui.show_choices()

    def _load_again(self):
        self._show_progress("Opening your host", "Reading your saved settings...")
        self._start_job(lambda progress: self.backend.read_settings(), self._loaded,
                        retry=self._load_again)

    def _accept_settings(self, settings):
        self.settings = {key: value for key, value in settings.items()
                         if key not in {"bot_token", "tunnel_token"}}
        self.configured = bool(settings.get("configured"))
        for key in ("guild_name", "hostname", "guild_id", "application_id",
                    "client_installer", "client_download_url", "port"):
            if key in settings:
                self.values[key].set(str(settings[key] or ""))
        if "tunnel_enabled" in settings:
            self.values["tunnel_mode"].set("cloudflare" if settings["tunnel_enabled"] else "existing")
        if self.configured:
            self.editing = False
            self.values["installer_mode"].set("file" if settings.get("client_installer") else "url")
            self.values["bot_token"].set("")
            self.values["tunnel_token"].set("")

    def show_server(self):
        self.error_back = self.show_connection
        self._clear("server", "1  YOUR SERVER     /     2  CONNECTIONS     /     3  HOST")
        self._heading("Bring captions to your server", "Use your own Discord bot and a public address for your host.")
        fields = tk.Frame(self.body, bg=PANEL)
        fields.pack(fill="x")
        fields.columnconfigure(0, weight=1, uniform="fields")
        fields.columnconfigure(1, weight=1, uniform="fields")
        for row, pair in enumerate((
            (("guild_name", "Host name"), ("hostname", "Public hostname")),
            (("guild_id", "Discord server ID"), ("application_id", "Application ID")),
        )):
            for column, (key, label) in enumerate(pair):
                frame, entry = self._field(fields, key, label)
                if (self.configured and self.settings.get("identity_locked", True)
                        and key in {"guild_id", "application_id"}):
                    entry.configure(state="readonly", readonlybackground=FIELD)
                frame.grid(row=row, column=column, sticky="ew", padx=(0, 12) if column == 0 else (12, 0), pady=(0, 18))
        frame, self.bot_entry = self._field(self.body, "bot_token", "Discord bot token", secret=True,
                                          hint=("Leave blank to keep the saved token. Changing IDs needs a new token; IDs stay fixed after members pair or consent."
                                                if self.configured else "Kept private on this computer. It is never shown in the host dashboard."))
        frame.pack(fill="x", pady=(0, 12))
        self._link(self.body, "Open Discord Developer Portal", DISCORD_DASHBOARD).pack(anchor="w")
        self._label(self.body, "Create an application and bot there. In Discord, turn on Developer Mode to copy your server ID.",
                    color=MUTED, size=9).pack(anchor="w", pady=(10, 0))
        advanced = tk.Frame(self.body, bg=PANEL)
        advanced.pack(fill="x", pady=(18, 0))
        self.advanced_visible = tk.BooleanVar(self.root, value=self.values["port"].get() != "8770")
        fields = tk.Frame(advanced, bg=PANEL)
        frame, _port_entry = self._field(fields, "port", "Local relay port",
                                        hint="Keep 8770 unless another application already uses it. Update your HTTPS route if you change this.")
        frame.pack(fill="x", pady=(8, 0))

        def toggle_advanced():
            if self.advanced_visible.get():
                fields.pack(fill="x")
            else:
                fields.pack_forget()

        tk.Checkbutton(advanced, text="Advanced host settings", variable=self.advanced_visible,
                       command=toggle_advanced, bg=PANEL, fg=MUTED, selectcolor=FIELD,
                       activebackground=PANEL, activeforeground=TEXT, highlightthickness=0,
                       cursor="hand2").pack(anchor="w", before=fields if fields.winfo_manager() else None)
        toggle_advanced()
        self._button(self.navigation, "Continue", self.next_page, primary=True).pack(side="right")
        if self.editing:
            self._button(self.navigation, "Cancel", self.cancel_edit).pack(side="left")
        elif not self.configured:
            self._button(self.navigation, "Back", self.takeover_ui.show_choices).pack(side="left")

    def collect_settings(self):
        values = {key: variable.get().strip() for key, variable in self.values.items()
                  if key not in {"installer_mode", "tunnel_mode"}
                  and not key.startswith("handoff_")}
        values["tunnel_enabled"] = self.values["tunnel_mode"].get() == "cloudflare"
        if self.values["installer_mode"].get() == "file":
            values["client_download_url"] = ""
        else:
            values["client_installer"] = ""
        if not values["tunnel_enabled"]:
            values["tunnel_token"] = ""
        return values

    def next_page(self):
        settings = self.collect_settings()
        error = validate_server(settings, require_token=self._requires_bot_token(settings))
        if error:
            self.notice.set(error)
            return
        self.show_connection()

    def _requires_bot_token(self, settings):
        return not self.configured or any(
            settings.get(key) != self.settings.get(key) for key in ("guild_id", "application_id")
        )

    def _radio(self, parent, text, variable, value, command):
        button = tk.Radiobutton(parent, text=text, variable=variable, value=value,
                                command=command, bg=PANEL, fg=TEXT, selectcolor=FIELD,
                                activebackground=PANEL, activeforeground=GOLD,
                                highlightthickness=0, anchor="w", cursor="hand2")
        return button

    def show_connection(self):
        self._clear("connection", "1  YOUR SERVER     /     2  CONNECTIONS     /     3  HOST")
        self._heading("Connect your members", "Choose how members reach this computer and get the member app.")
        self._label(self.body, "Public connection", bold=True).pack(anchor="w")
        radios = tk.Frame(self.body, bg=PANEL)
        radios.pack(fill="x", pady=(6, 10))
        self._radio(radios, "Use Cloudflare Tunnel", self.values["tunnel_mode"], "cloudflare", self._routing_changed).pack(side="left")
        self._radio(radios, "I already have HTTPS forwarding", self.values["tunnel_mode"], "existing", self._routing_changed).pack(side="left", padx=(20, 0))
        self.routing_fields = tk.Frame(self.body, bg=PANEL)
        self.routing_fields.pack(fill="x")
        self._routing_changed()
        tk.Frame(self.body, height=1, bg=BORDER).pack(fill="x", pady=18)
        self._label(self.body, "Member app", bold=True).pack(anchor="w")
        radios = tk.Frame(self.body, bg=PANEL)
        radios.pack(fill="x", pady=(6, 10))
        self._radio(radios, "I have the installer file", self.values["installer_mode"], "file", self._installer_changed).pack(side="left")
        self._radio(radios, "Use a download link", self.values["installer_mode"], "url", self._installer_changed).pack(side="left", padx=(20, 0))
        self.installer_fields = tk.Frame(self.body, bg=PANEL)
        self.installer_fields.pack(fill="x")
        self._installer_changed()
        self._button(self.navigation, "Back", self.show_server).pack(side="left")
        self._button(self.navigation, "Set up host", self.begin_setup, primary=True).pack(side="right")

    def _routing_changed(self):
        for child in self.routing_fields.winfo_children():
            child.destroy()
        if self.values["tunnel_mode"].get() == "cloudflare":
            frame, _entry = self._field(self.routing_fields, "tunnel_token", "Cloudflare tunnel token", secret=True,
                                       hint="Leave blank to keep the saved token." if self.configured and self.settings.get("tunnel_enabled") else None)
            frame.pack(fill="x")
            self._link(self.routing_fields, "Open Cloudflare dashboard", CLOUDFLARE_DASHBOARD).pack(anchor="w", pady=(8, 0))
            self._label(self.routing_fields, "Create a tunnel and route your hostname to http://127.0.0.1:"
                        + self.values["port"].get() + ". Paste only its token above; setup installs the connector.",
                        size=9, color=MUTED).pack(anchor="w", pady=(6, 0))
        else:
            self._label(self.routing_fields, "Your hostname must already forward HTTPS and WebSocket traffic to http://127.0.0.1:"
                        + self.values["port"].get() + " on this computer.",
                        color=MUTED).pack(anchor="w", pady=(0, 8))

    def _installer_changed(self):
        for child in self.installer_fields.winfo_children():
            child.destroy()
        if self.values["installer_mode"].get() == "file":
            row = tk.Frame(self.installer_fields, bg=PANEL)
            row.pack(fill="x")
            entry = tk.Entry(row, textvariable=self.values["client_installer"], state="readonly",
                             readonlybackground=FIELD, fg=TEXT, relief="flat", bd=0)
            entry.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 12))
            self._button(row, "Choose file", self.choose_installer).pack(side="right")
            self._label(self.installer_fields, "Choose Vemals-Ears-Setup.exe supplied by the project. Members will download it from your host.",
                        size=9, color=MUTED).pack(anchor="w", pady=(8, 0))
        else:
            frame, _entry = self._field(self.installer_fields, "client_download_url", "Public HTTPS download link")
            frame.pack(fill="x")

    def choose_installer(self):
        filename = filedialog.askopenfilename(parent=self.root, title="Choose the Vemal's Ears member installer",
                                              filetypes=(("Windows installers", "*.exe"),))
        if filename:
            self.values["client_installer"].set(filename)

    def begin_setup(self):
        if self.settings.get("mode") == "remote-worker" and not self.settings.get("takeover_confirmed"):
            self.takeover_ui.show_activation()
            return
        if self.busy:
            return
        settings = self.collect_settings()
        updating = self.editing
        if not self.configured or updating:
            error = (validate_server(settings, require_token=self._requires_bot_token(settings))
                     or validate_connection(settings, require_tunnel_token=not self.settings.get("tunnel_enabled", False)))
            if error:
                self.notice.set(error)
                return
        for key in ("bot_token", "tunnel_token"):
            if settings.get(key):
                self.secrets.add(settings[key])
        self._show_progress("Preparing your host", "Your settings stay private on this computer.")

        def work(progress):
            return prepare_host(self.backend, settings, progress,
                                lambda saved: self.events.put(("configured", saved)),
                                configure_existing=updating)

        self._start_job(work, self._setup_complete, retry=self.begin_setup)

    def _show_progress(self, title, detail):
        self._clear("progress", "HOST SETUP")
        self._heading(title, detail)
        self._label(self.body, "Progress", size=14, bold=True).pack(anchor="w", pady=(36, 12))
        self.progress_bar = ttk.Progressbar(self.body, mode="indeterminate", style="Host.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", pady=(0, 18))
        self.progress_bar.start(18)
        self.progress_text.set(detail)
        tk.Label(self.body, textvariable=self.progress_text, fg=TEXT, bg=PANEL,
                 anchor="w", justify="left", wraplength=690).pack(fill="x")
        self._label(self.body, "Keep this window open. Setup may take several minutes on the first run.\nIf it fails, Retry continues with the same host settings and keys.",
                    color=MUTED).pack(anchor="w", pady=(30, 0))
        self._button(self.navigation, "Working...", lambda: None, primary=True, disabled=True).pack(side="right")

    def _setup_complete(self, result):
        self._accept_settings(result["settings"])
        self.show_dashboard(result["status"])
        if not result["status"].get("health"):
            self.notice.set("Services are starting. Check again in a moment, then test your public join page.")

    def show_dashboard(self, status):
        self._clear("dashboard", "YOUR HOST")
        self.current_status = status
        healthy = bool(status.get("health"))
        self._heading(self.settings.get("guild_name") or "Your host", self.settings.get("hostname") or "Vemal's Ears Host")
        self._label(self.body, "Relay responding" if healthy else "Host needs a check", size=18,
                    bold=True, color=GREEN if healthy else GOLD).pack(anchor="w", pady=(0, 8))
        self._label(self.body, self._safe(status.get("detail") or "Check the services, then verify captions in Discord."),
                    color=MUTED).pack(anchor="w", pady=(0, 18))
        for key, label in (("relay", "Member relay"), ("worker", "Caption worker"), ("tunnel", "Public connection")):
            row = tk.Frame(self.body, bg=FIELD)
            row.pack(fill="x", pady=4)
            self._label(row, label, bold=True).pack(side="left", padx=14, pady=11)
            value = str(status.get(key, "Not checked"))
            self._label(row, self._safe(value), color=GOLD).pack(side="right", padx=14)
        actions = tk.Frame(self.body, bg=PANEL)
        actions.pack(fill="x", pady=(20, 12))
        self._button(actions, "Add bot to Discord", lambda: self._open(self.settings.get("bot_install_url", "")),
                     disabled=not self.settings.get("bot_install_url")).pack(side="left", padx=(0, 12))
        self._button(actions, "Open join page", lambda: self._open(self.settings.get("join_url", "")),
                     primary=True, disabled=not self.settings.get("join_url")).pack(side="left")
        remote = self.settings.get("mode") == "remote-worker"
        instruction = ("Existing members keep their connections. Start a new /vem start session and test captions and typed chat.\nKeep the original PC online for the shared relay, pairing and Downtime." if remote else
                       "In Discord: use /vem setup to pair, then /vem start in a voice channel.\nTest with a consenting member. A healthy relay alone does not verify captions.")
        self._label(self.body, instruction,
                    color=MUTED, size=9).pack(anchor="w", pady=(4, 0))
        self._button(self.navigation, "Retry setup", self.begin_setup).pack(side="left", padx=(0, 10))
        if not remote:
            self._button(self.navigation, "Edit settings", self.edit_settings).pack(side="left", padx=(0, 10))
        self._button(self.navigation, "Stop", lambda: self.control_services("stop")).pack(side="left")
        self._button(self.navigation, "Check", self.refresh_status).pack(side="right", padx=(10, 0))
        self._button(self.navigation, "Start", lambda: self.control_services("start"), primary=True).pack(side="right")

    def edit_settings(self):
        if not self.busy:
            if self.settings.get("mode") == "remote-worker":
                self.notice.set("This replacement preserves the existing host identity and connection.")
                return
            self.editing = True
            self.show_server()

    def cancel_edit(self):
        self._accept_settings(self.settings)
        self.show_dashboard(self.current_status)

    def refresh_status(self):
        if self.busy:
            return
        self.notice.set("Checking services...")
        self._start_job(lambda progress: self.backend.status(), self.show_dashboard, retry=self.refresh_status)

    def control_services(self, action):
        if self.busy:
            return
        if action == "start" and self.settings.get("mode") == "remote-worker" and not self.settings.get("takeover_confirmed"):
            self.takeover_ui.show_activation()
            return
        self.notice.set("Starting host services..." if action == "start" else "Stopping host services...")

        def work(progress):
            if action == "start":
                self.backend.start_services()
            else:
                self.backend.stop_services()
            return self.backend.status()

        self._start_job(work, self.show_dashboard, retry=lambda: self.control_services(action))

    def _start_job(self, work, completion, *, retry):
        if self.busy:
            return
        self.busy = True
        self.retry_action = retry
        self.completion = completion

        def run():
            try:
                result = work(lambda text: self.events.put(("progress", str(text))))
            except Exception as error:
                self.events.put(("error", str(error) or "Setup could not finish. Please retry."))
            else:
                self.events.put(("complete", result))

        threading.Thread(target=run, name="VemalsEarsHostSetup", daemon=True).start()

    def _safe(self, message):
        result = str(message)
        for value in sorted(self.secrets, key=len, reverse=True):
            if value:
                result = result.replace(value, "[private value]")
        return result

    def _drain_events(self):
        if self.closed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.progress_text.set(self._safe(value))
                elif kind == "configured":
                    self._accept_settings(value)
                elif kind == "error":
                    self.busy = False
                    self.notice.set(self._safe(value))
                    if self.page == "progress":
                        self.progress_bar.stop()
                        self.progress_text.set("Setup stopped. Your saved settings and keys are preserved.")
                        for child in self.navigation.winfo_children():
                            child.destroy()
                        if not self.configured:
                            self._button(self.navigation, "Back", self.error_back).pack(side="left")
                        self._button(self.navigation, "Retry", self.retry_action, primary=True).pack(side="right")
                elif kind == "complete":
                    self.busy = False
                    callback = self.completion
                    self.completion = None
                    callback(value)
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_id = self.root.after(60, self._drain_events)

    def _resize_notice(self, event):
        if event.widget is self.root:
            self.notice_label.configure(wraplength=max(300, event.width - 60))

    def _body_resized(self, _event):
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), self.body.winfo_reqheight() + 40))

    def _canvas_resized(self, event):
        self.canvas.itemconfigure(self.body_window, width=max(100, event.width - 48))
        self._body_resized(event)

    def _mousewheel(self, event):
        if self.body.winfo_reqheight() + 40 > self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _reveal_focus(self, event):
        widget = event.widget
        current = widget
        while current is not self.body:
            if current is self.root or current.master is None:
                return
            current = current.master
        top = widget.winfo_rooty() - self.body.winfo_rooty() + 20
        bottom = top + widget.winfo_height()
        visible_top = self.canvas.canvasy(0)
        visible_bottom = visible_top + self.canvas.winfo_height()
        total = self.body.winfo_reqheight() + 40
        if top < visible_top:
            self.canvas.yview_moveto(max(0, top - 12) / total)
        elif bottom > visible_bottom:
            self.canvas.yview_moveto(max(0, bottom - self.canvas.winfo_height() + 12) / total)

    def _open(self, url):
        if not public_url(url):
            self.notice.set("This host does not yet have a valid public link.")
            return
        self.open_url(url)

    def close(self):
        if self.busy:
            if not messagebox.askyesno("Close host setup?", "Setup is still working. Closing now interrupts it.\nYou can reopen the app and retry with the same saved settings.", parent=self.root):
                return
        self.closed = True
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


class PreviewController:
    """In-memory GUI inspection without disk, service, or network operations."""
    def __init__(self):
        self.settings = {"configured": False}

    def read_settings(self):
        return dict(self.settings)

    def configure(self, settings):
        self.settings = {key: value for key, value in settings.items()
                         if key not in {"bot_token", "tunnel_token"}}
        self.settings.update(configured=True,
                             bot_install_url=DISCORD_DASHBOARD,
                             join_url="https://example.org/join")

    def prepare(self, progress):
        progress("Preview only: no downloads or services were started.")

    def install_services(self):
        pass

    start_services = install_services
    stop_services = install_services

    def status(self):
        return {"configured": True, "health": False, "relay": "Preview", "worker": "Preview",
                "tunnel": "Preview", "detail": "Preview only. No services were changed."}


def request_elevation(state_directory: str | None) -> bool:
    if os.name != "nt" or ctypes.windll.shell32.IsUserAnAdmin():
        return False
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if windowless.is_file():
        executable = windowless
    arguments = ["-E", "-s", "-B", "-m", "Hosting.wizard"]
    if state_directory:
        arguments.extend(("--state-directory", state_directory))
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", str(executable), subprocess.list2cmdline(arguments), str(ROOT), 1,
    )
    if result <= 32:
        raise OSError("Administrator access was not granted. Reopen Host Setup and allow Windows to continue.")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.smoke_test and request_elevation(args.state_directory):
            return 0
        if args.smoke_test:
            backend = PreviewController()
        else:
            from Hosting.control import HostController

            backend = HostController(state_directory=Path(args.state_directory) if args.state_directory else None)
    except Exception as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Vemal's Ears Host", str(error), parent=root)
        root.destroy()
        return 2
    root = tk.Tk()
    HostWizard(root, backend, open_url=(lambda url: None) if args.smoke_test else webbrowser.open)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
