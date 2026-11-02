"""Command-free existing-host handoff screens for the host wizard."""

from pathlib import Path
import tkinter as tk
from tkinter import filedialog


class TakeoverUi:
    def __init__(self, wizard, backend=None):
        self.wizard = wizard
        if backend is None and hasattr(wizard.backend, "state"):
            from Hosting.takeover import TakeoverController
            backend = TakeoverController(wizard.backend)
        self.backend = backend
        self.original_stopped = tk.BooleanVar(wizard.root, value=False)
        self.stop_for_export = tk.BooleanVar(wizard.root, value=False)
        self.never_started = tk.BooleanVar(wizard.root, value=False)
        self.preview = None
        for key in ("handoff_file", "handoff_password", "handoff_password_confirm"):
            wizard.values[key] = tk.StringVar(wizard.root, value="")

    def label(self, text, **options):
        from Hosting.wizard import MUTED
        return self.wizard._label(self.wizard.body, text, color=MUTED, **options)

    @staticmethod
    def gpu_label():
        from VoiceCaptions import runtime_installer
        return runtime_installer.gpu_support_label()

    def checkbox(self, text, variable):
        from Hosting.wizard import PANEL, TEXT, FIELD, GOLD
        widget = tk.Checkbutton(self.wizard.body, text=text, variable=variable,
                                bg=PANEL, fg=TEXT, selectcolor=FIELD,
                                activebackground=PANEL, activeforeground=GOLD,
                                wraplength=550, justify="left", anchor="w")
        widget.bind("<Configure>", lambda event: widget.configure(wraplength=max(240, event.width - 32)))
        widget.pack(fill="x", pady=(10, 4))
        return widget

    def show_choices(self):
        from Hosting.wizard import FIELD, BORDER, MUTED
        w = self.wizard
        w._clear("choose-host", "CHOOSE YOUR SETUP")
        w._heading("How will this PC host captions?", "Start a new host or move an existing caption worker.")
        choices = (
            ("Set up a new host", "Use your own bot, hostname and pairings.", "Set up", w.show_server, True),
            ("Export from the current host", "Stop its worker and save an encrypted handoff.", "Export", self.show_source, False),
            ("Import on the replacement PC", "Keep the existing bot and member connections.", "Import", self.show_import, False),
        )
        for title, description, action, command, primary in choices:
            row = tk.Frame(w.body, bg=FIELD, highlightthickness=1, highlightbackground=BORDER)
            row.pack(fill="x", pady=(0, 10))
            w._button(row, action, command, primary=primary,
                      disabled=not primary and self.backend is None).pack(side="right", padx=(8, 16), pady=16)
            copy = tk.Frame(row, bg=FIELD)
            copy.pack(side="left", fill="x", expand=True, padx=(16, 0), pady=16)
            w._label(copy, title, size=11, bold=True).pack(anchor="w")
            w._label(copy, description, size=9, color=MUTED).pack(anchor="w", pady=(4, 0))
        self.label("When moving a worker, the original PC keeps pairing, public access and Downtime. Keep that PC online.",
                   size=9).pack(anchor="w", pady=(2, 0))

    def show_source(self):
        if self.backend is None or self.wizard.busy:
            return
        w = self.wizard
        w.error_back = self.show_choices
        w._show_progress("Checking the original host", "Reading the caption worker registration. No services are changing.")
        w._start_job(lambda progress: self.backend.legacy.inspect(), self.source_ready, retry=self.show_source)

    def source_ready(self, status):
        w = self.wizard
        self.never_started.set(False)
        self.stop_for_export.set(False)
        w._clear("takeover-export", "ORIGINAL PC  /  ENCRYPTED HANDOFF")
        w._heading("Move the caption worker", "Downtime, member pairing and the public connection stay on this PC.")
        w._button(w.navigation, "Back", self.show_choices).pack(side="left")
        if not status.get("available"):
            self.label(status.get("detail", "No supported original host was found.")).pack(anchor="w")
            self.label("Use Import on the replacement PC, or Set up a new host for a separate guild.").pack(anchor="w", pady=14)
            return
        self.label(status.get("guild_name", "Vemal's Ears") + "  -  "
                   + status.get("public_http_url", ""), bold=True).pack(anchor="w", pady=(0, 12))
        if status.get("handoff_pending"):
            self.label("Handoff pending. Original caption worker: " + status.get("worker", "STOPPED")
                       + ". Keep this PC online for existing member connections and Downtime.").pack(anchor="w")
            self.label("Handoff file: " + status.get("bundle", ""), size=9).pack(anchor="w", pady=10)
            self.label("Send the encrypted file privately and its passphrase separately. The recipient chooses Import on the replacement PC.").pack(anchor="w", pady=6)
            self.checkbox("The replacement has NEVER been started. I will discard this handoff file before resuming the original worker.", self.never_started)
            self.label("After the replacement has run, return its latest consent and mapping state before moving back. This button cannot perform that return.", size=9).pack(anchor="w", pady=8)
            w._button(w.navigation, "Cancel unused handoff", self.cancel_unused).pack(side="right")
            return
        self.label("Export stops and disables this caption worker so a reboot cannot start two bots. Other services keep running; keep this PC online.").pack(anchor="w", pady=(0, 10))
        self.file_field(save=True)
        self.passphrase_fields()
        self.checkbox("I am ready to stop this PC's caption worker and export the handoff.", self.stop_for_export)
        w._button(w.navigation, "Stop worker and export", self.export, primary=True).pack(side="right")

    def file_field(self, *, save):
        w = self.wizard
        frame, entry = w._field(w.body, "handoff_file", "Encrypted handoff file")
        frame.pack(fill="x", pady=(4, 10))
        entry.pack_forget()
        w._button(frame, "Choose location" if save else "Choose file",
                  lambda: self.choose_file(save=save)).pack(side="right", padx=(10, 0))
        entry.pack(side="left", fill="x", expand=True, ipady=8)

    def passphrase_fields(self):
        from Hosting.wizard import PANEL
        w = self.wizard
        row = tk.Frame(w.body, bg=PANEL)
        row.pack(fill="x", pady=(0, 4))
        fields = (
            w._field(row, "handoff_password", "Passphrase (12+ characters)", secret=True)[0],
            w._field(row, "handoff_password_confirm", "Repeat passphrase", secret=True)[0],
        )
        layout = [None]

        def arrange(event):
            columns = event.width >= 500
            if columns == layout[0]:
                return
            layout[0] = columns
            row.columnconfigure(0, weight=1, uniform="passphrases" if columns else "")
            row.columnconfigure(1, weight=1 if columns else 0, uniform="passphrases" if columns else "")
            for index, frame in enumerate(fields):
                frame.grid(row=0 if columns else index, column=index if columns else 0,
                           sticky="ew", padx=(0, 8) if columns and index == 0 else (8, 0) if columns else 0,
                           pady=(0, 8))

        row.bind("<Configure>", arrange)
        fields[0].grid(row=0, column=0, sticky="ew")
        fields[1].grid(row=0, column=1, sticky="ew")

    def choose_file(self, *, save):
        options = {"parent": self.wizard.root, "filetypes": [("Vemal's Ears handoff", "*.vehost")]}
        if save:
            path = filedialog.asksaveasfilename(**options, defaultextension=".vehost",
                                               initialfile="Vemals-Ears-Worker.vehost")
        else:
            path = filedialog.askopenfilename(**options)
        if path:
            self.wizard.values["handoff_file"].set(path)

    def export(self):
        w = self.wizard
        if w.busy or self.backend is None:
            return
        password = w.values["handoff_password"].get()
        destination = w.values["handoff_file"].get()
        if len(password) < 12 or password != w.values["handoff_password_confirm"].get():
            w.notice.set("Use a passphrase of at least 12 characters and enter it identically twice.")
            return
        from Hosting.handoff import HandoffError, validate_password
        try:
            validate_password(password)
        except HandoffError as error:
            w.notice.set(str(error))
            return
        if not self.stop_for_export.get():
            w.notice.set("Confirm that you are ready to stop the original caption worker.")
            return
        if not destination or not Path(destination).is_absolute() or Path(destination).exists():
            w.notice.set("Choose a new handoff file. Existing files are never overwritten.")
            return
        w.secrets.add(password)
        w.error_back = self.show_source
        w._show_progress("Preparing the handoff", "Stopping the caption worker and encrypting only its required state...")
        w._start_job(lambda progress: self.backend.legacy.export(destination, password, progress),
                     self.exported, retry=self.show_source)

    def exported(self, result):
        w = self.wizard
        w.values["handoff_password"].set("")
        w.values["handoff_password_confirm"].set("")
        self.source_ready({"available": True, "handoff_pending": True, **result})

    def cancel_unused(self):
        w = self.wizard
        if w.busy or self.backend is None:
            return
        if not self.never_started.get():
            w.notice.set("Confirm that the replacement has never been started before cancelling.")
            return
        w.error_back = self.show_source
        w._show_progress("Cancelling an unused handoff", "Restoring only the original caption worker...")
        w._start_job(lambda progress: self.backend.legacy.cancel_unused(replacement_never_started=True),
                     self.source_ready, retry=self.show_source)

    def show_import(self):
        w = self.wizard
        if w.busy:
            return
        self.preview = None
        if self.backend is not None:
            self.backend.pending = None
        self.original_stopped.set(False)
        w._clear("takeover-import", "REPLACEMENT PC  /  OPEN HANDOFF")
        w._heading("Take over an existing host", "Open the encrypted file from the original PC. Members keep their connections.")
        self.label("This PC runs the Discord bot and the " + self.gpu_label() + " GPU caption worker. The original PC keeps pairing, the public relay and Downtime, and must stay online.").pack(anchor="w", pady=(0, 10))
        self.file_field(save=False)
        w._field(w.body, "handoff_password", "Handoff passphrase", secret=True)[0].pack(fill="x", pady=6)
        w._button(w.navigation, "Back", self.show_choices).pack(side="left")
        w._button(w.navigation, "Open handoff", self.open_import, primary=True,
                  disabled=self.backend is None).pack(side="right")

    def open_import(self):
        w = self.wizard
        if w.busy or self.backend is None:
            return
        path, password = w.values["handoff_file"].get(), w.values["handoff_password"].get()
        if not path or not password:
            w.notice.set("Choose the encrypted handoff file and enter its passphrase.")
            return
        w.secrets.add(password)
        w.error_back = self.show_import
        w._show_progress("Opening the handoff", "Verifying the encrypted file. No worker is starting.")
        w._start_job(lambda progress: self.backend.preview(path, password), self.import_ready,
                     retry=self.show_import)

    def import_ready(self, preview):
        w = self.wizard
        self.preview = preview
        w.values["handoff_password"].set("")
        w._clear("takeover-confirm", "REPLACEMENT PC  /  CONFIRM HOST")
        w._heading(preview["guild_name"], "Existing member connection: " + preview["public_http_url"])
        self.label("The existing Discord bot, publisher identity, consent choices and mappings will be preserved. No member needs to pair again.").pack(anchor="w", pady=(0, 12))
        self.label("Discord server ID: " + preview["guild_id"] + "\nApplication ID: " + preview["application_id"], size=9).pack(anchor="w", pady=(0, 12))
        self.label("Setup prepares GPU speech recognition and starts this replacement worker. Keep the original PC online for pairing, the relay and Downtime.").pack(anchor="w")
        self.checkbox("The original caption worker is stopped and disabled. This is the only replacement worker I will start.", self.original_stopped)
        w._button(w.navigation, "Back", self.show_import).pack(side="left")
        w._button(w.navigation, "Import and start captions", self.begin_import, primary=True).pack(side="right")

    def begin_import(self):
        w = self.wizard
        if w.busy or self.backend is None:
            return
        if not self.original_stopped.get():
            w.notice.set("Confirm that the original caption worker is stopped and disabled.")
            return
        w.error_back = self.show_import
        w._show_progress("Setting up the replacement", "Importing the worker and preparing GPU speech recognition...")

        def work(progress):
            settings = self.backend.import_pending(original_stopped=True)
            w.events.put(("configured", settings))
            w.backend.prepare(progress)
            progress("Starting only the replacement caption worker...")
            w.backend.install_services()
            return {"settings": w.backend.read_settings(), "status": w.backend.status()}

        w._start_job(work, w._setup_complete, retry=w.begin_setup)

    def show_activation(self):
        w = self.wizard
        self.original_stopped.set(False)
        w._clear("takeover-activate", "REPLACEMENT PC  /  START CAPTIONS")
        w._heading("Start the replacement worker", "Members keep " + w.settings.get("public_http_url", w.settings.get("hostname", "")))
        self.label("The original PC must remain online for the shared relay, pairing and Downtime.").pack(anchor="w", pady=(0, 16))
        self.checkbox("The original caption worker is stopped and disabled. This is the only replacement worker I will start.", self.original_stopped)
        w._button(w.navigation, "Back", lambda: w.show_dashboard(w.current_status)).pack(side="left")
        w._button(w.navigation, "Prepare and start captions", self.activate, primary=True).pack(side="right")

    def activate(self):
        w = self.wizard
        if not self.original_stopped.get():
            w.notice.set("Confirm that the original caption worker is stopped and disabled.")
            return
        w.error_back = self.show_activation
        w._show_progress("Preparing captions", "Checking the replacement worker...")

        def work(progress):
            self.backend.confirm_start(original_stopped=True)
            w.backend.prepare(progress)
            w.backend.install_services()
            return {"settings": w.backend.read_settings(), "status": w.backend.status()}

        w._start_job(work, w._setup_complete, retry=self.show_activation)
