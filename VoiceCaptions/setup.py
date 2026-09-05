"""Local, nontechnical Discord setup window for Voice Captions.

This window only saves credentials and prepares the bot install link. It does
not open a browser, contact Discord, or attempt a login.
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from VoiceCaptions.credentials import (  # type: ignore
        BOT_INSTALL_PERMISSION_NAMES,
        CredentialStore,
        CredentialStoreError,
        CredentialValidationError,
        WindowsCredentialStore,
        build_bot_install_url,
        validate_credentials,
    )
    from VoiceCaptions.runtime_installer import (  # type: ignore
        InstallOutcome,
        InstallProgress,
        RuntimeInstallError,
        detect_installed_runtime,
        install_voice_dependencies,
    )
else:
    from .credentials import (
        BOT_INSTALL_PERMISSION_NAMES,
        CredentialStore,
        CredentialStoreError,
        CredentialValidationError,
        WindowsCredentialStore,
        build_bot_install_url,
        validate_credentials,
    )
    from .runtime_installer import (
        InstallOutcome,
        InstallProgress,
        RuntimeInstallError,
        detect_installed_runtime,
        install_voice_dependencies,
    )

# Shared suite colors. The narrow frost rail is the one visual signature: it
# turns the Discord setup sequence into a path, not a generic settings form.
VOID = "#090b11"
SLATE = "#11161d"
RAISED = "#161b24"
BORDER = "#334a63"
FROST = "#42b8f2"
TEXT = "#dbe6f2"
MUTED = "#879cb3"
SUCCESS = "#4fd182"
DANGER = "#e85c61"

DISPLAY_FONT = ("Segoe UI Semibold", 19)
HEADING_FONT = ("Segoe UI Semibold", 11)
BODY_FONT = ("Segoe UI", 10)
UTILITY_FONT = ("Cascadia Mono", 9)


@dataclass(frozen=True)
class SavedSetup:
    install_url: str


def save_setup(
    store: CredentialStore, application_id: str, bot_token: str
) -> SavedSetup:
    """Validate, store, and return only the non-secret install link."""
    credentials = validate_credentials(application_id, bot_token)
    store.save(credentials)
    return SavedSetup(install_url=build_bot_install_url(credentials.application_id))


def run_runtime_install(installer, event_sink) -> None:
    """Background boundary used only after the install button is clicked."""
    try:
        outcome = installer(
            progress=lambda update: event_sink(("progress", update))
        )
    except RuntimeInstallError as error:
        event_sink(("error", str(error)))
    except Exception:
        # Keep implementation paths, network details, and process output out
        # of the nontechnical setup window.
        event_sink(("error", "Local voice tools could not be installed."))
    else:
        event_sink(("done", outcome))


class DiscordSetupWindow(tk.Tk):
    WIDTH = 640
    HEIGHT = 980

    def __init__(
        self,
        store: CredentialStore | None,
        *,
        startup_error: str = "",
        runtime_installer=install_voice_dependencies,
        runtime_detector=detect_installed_runtime,
    ) -> None:
        super().__init__()
        self.store = store
        self.runtime_installer = runtime_installer
        self.runtime_detector = runtime_detector
        self.runtime_events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.runtime_installing = False
        self.title("Vemal's Ears - Connect Discord Host")
        self.configure(bg=VOID)
        # Setup is normally opened while WoW owns the screen. Keep this
        # deliberate utility window above the game so it cannot appear to do
        # nothing, then closing it returns the player to the game unchanged.
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.minsize(self.WIDTH, self.HEIGHT)

        self.application_id = tk.StringVar()
        self.bot_token = tk.StringVar()
        self.reveal_token = tk.BooleanVar(value=False)
        self.install_url = tk.StringVar()
        self.status = tk.StringVar(value=startup_error)
        self.runtime_status = tk.StringVar(
            value="Nothing downloads until you click install."
        )
        self.status_color = DANGER if startup_error else MUTED

        self._build()
        self._load_saved_state()
        self._load_runtime_state()
        self._center()
        self.after_idle(self.application_entry.focus_set)

    def _center(self) -> None:
        self.update_idletasks()
        x = max((self.winfo_screenwidth() - self.WIDTH) // 2, 0)
        y = max((self.winfo_screenheight() - self.HEIGHT) // 2, 0)
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _build(self) -> None:
        shell = tk.Frame(self, bg=VOID, padx=28, pady=24)
        shell.pack(fill="both", expand=True)

        tk.Label(
            shell,
            text="VOICE CAPTIONS / DISCORD",
            bg=VOID,
            fg=FROST,
            font=UTILITY_FONT,
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            shell,
            text="Connect Discord voice",
            bg=VOID,
            fg=TEXT,
            font=DISPLAY_FONT,
            anchor="w",
        ).pack(fill="x", pady=(4, 3))
        tk.Label(
            shell,
            text=(
                "Paste the two values from your Discord application. They stay "
                "in Windows Credential Manager on this PC."
            ),
            bg=VOID,
            fg=MUTED,
            font=BODY_FONT,
            justify="left",
            wraplength=565,
            anchor="w",
        ).pack(fill="x", pady=(0, 18))

        outer = tk.Frame(shell, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="x")
        card = tk.Frame(outer, bg=SLATE, padx=20, pady=18)
        card.pack(fill="both")

        content = tk.Frame(card, bg=SLATE)
        content.pack(fill="x")
        rail = tk.Canvas(
            content,
            width=30,
            height=295,
            bg=SLATE,
            highlightthickness=0,
            bd=0,
        )
        rail.pack(side="left", fill="y", padx=(0, 10))
        rail.create_line(15, 18, 15, 270, fill=BORDER, width=2)
        for y, number in ((18, "1"), (151, "2"), (270, "3")):
            rail.create_oval(7, y - 8, 23, y + 8, fill=FROST, outline=FROST)
            rail.create_text(15, y, text=number, fill=VOID, font=("Segoe UI Semibold", 8))

        fields = tk.Frame(content, bg=SLATE)
        fields.pack(side="left", fill="both", expand=True)

        self._field_label(fields, "Discord Application ID")
        self.application_entry = self._entry(fields, self.application_id)
        self.application_entry.pack(fill="x", pady=(5, 4), ipady=8)
        self.application_entry.bind("<KeyRelease>", self._refresh_install_link)
        tk.Label(
            fields,
            text="A 17 to 20 digit number on the General Information page.",
            bg=SLATE,
            fg=MUTED,
            font=BODY_FONT,
            anchor="w",
        ).pack(fill="x", pady=(0, 16))

        self._field_label(fields, "Bot Token")
        token_row = tk.Frame(fields, bg=SLATE)
        token_row.pack(fill="x", pady=(5, 4))
        self.token_entry = self._entry(token_row, self.bot_token, show="*")
        self.token_entry.pack(side="left", fill="x", expand=True, ipady=8)
        show = tk.Checkbutton(
            token_row,
            text="Show",
            variable=self.reveal_token,
            command=self._toggle_token,
            bg=SLATE,
            fg=MUTED,
            activebackground=SLATE,
            activeforeground=TEXT,
            selectcolor=RAISED,
            font=BODY_FONT,
            padx=8,
            highlightthickness=0,
            bd=0,
        )
        show.pack(side="left", padx=(8, 0))
        tk.Label(
            fields,
            text="The token is hidden and is never written to an addon file or log.",
            bg=SLATE,
            fg=MUTED,
            font=BODY_FONT,
            anchor="w",
        ).pack(fill="x", pady=(0, 16))

        self.save_button = self._button(
            fields,
            "Save details on this PC",
            self._save,
            accent=True,
        )
        self.save_button.pack(fill="x", ipady=5)
        if self.store is None:
            self.save_button.configure(state="disabled")

        install = tk.Frame(shell, bg=VOID)
        install.pack(fill="x", pady=(18, 0))
        tk.Label(
            install,
            text="Install the bot",
            bg=VOID,
            fg=TEXT,
            font=HEADING_FONT,
            anchor="w",
        ).pack(fill="x")
        permission_text = ", ".join(BOT_INSTALL_PERMISSION_NAMES)
        tk.Label(
            install,
            text=f"The link requests only: {permission_text}.",
            bg=VOID,
            fg=MUTED,
            font=BODY_FONT,
            anchor="w",
        ).pack(fill="x", pady=(3, 8))

        link_row = tk.Frame(install, bg=VOID)
        link_row.pack(fill="x")
        self.link_entry = tk.Entry(
            link_row,
            textvariable=self.install_url,
            state="readonly",
            readonlybackground=RAISED,
            fg=TEXT,
            font=UTILITY_FONT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=FROST,
        )
        self.link_entry.pack(side="left", fill="x", expand=True, ipady=8)
        self.copy_button = self._button(link_row, "Copy install link", self._copy_link)
        self.copy_button.pack(side="left", padx=(9, 0), ipady=4)
        self.copy_button.configure(state="disabled")

        tk.Label(
            install,
            text="Paste the copied link into your browser, choose your server, then approve.",
            bg=VOID,
            fg=MUTED,
            font=BODY_FONT,
            anchor="w",
        ).pack(fill="x", pady=(7, 0))

        runtime_outer = tk.Frame(shell, bg=BORDER, padx=1, pady=1)
        runtime_outer.pack(fill="x", pady=(18, 0))
        runtime = tk.Frame(runtime_outer, bg=SLATE, padx=18, pady=14)
        runtime.pack(fill="both")
        tk.Label(
            runtime,
            text="Local captions engine",
            bg=SLATE,
            fg=TEXT,
            font=HEADING_FONT,
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            runtime,
            text=(
                "Installs a verified high-accuracy speech model on this PC. "
                "Supported NVIDIA cards are used automatically, with a "
                "compatibility engine kept as a fallback. Voice stays local."
            ),
            bg=SLATE,
            fg=MUTED,
            font=BODY_FONT,
            justify="left",
            wraplength=550,
            anchor="w",
        ).pack(fill="x", pady=(3, 9))

        runtime_action = tk.Frame(runtime, bg=SLATE)
        runtime_action.pack(fill="x")
        self.runtime_button = self._button(
            runtime_action,
            "Install local voice tools",
            self._install_runtime,
            accent=True,
        )
        self.runtime_button.pack(side="left", ipady=3)
        tk.Label(
            runtime_action,
            text="About 560 MB; 1.2 GB with NVIDIA",
            bg=SLATE,
            fg=MUTED,
            font=UTILITY_FONT,
        ).pack(side="right")

        self.runtime_progress = tk.Canvas(
            runtime,
            height=8,
            bg=VOID,
            highlightthickness=1,
            highlightbackground=BORDER,
            bd=0,
        )
        self.runtime_progress.pack(fill="x", pady=(10, 7))
        self.runtime_progress_fill = self.runtime_progress.create_rectangle(
            0,
            0,
            0,
            8,
            fill=FROST,
            outline=FROST,
        )
        # The bar can be filled before Tk has laid the canvas out, when an
        # already installed runtime is detected during startup. The canvas is
        # one pixel wide until then, so keep the requested fraction and draw it
        # again on every layout change instead of leaving an empty bar.
        self.runtime_fraction = 0.0
        self.runtime_progress.bind("<Configure>", self._redraw_runtime_progress)
        self.runtime_status_label = tk.Label(
            runtime,
            textvariable=self.runtime_status,
            bg=SLATE,
            fg=MUTED,
            font=BODY_FONT,
            anchor="w",
        )
        self.runtime_status_label.pack(fill="x")

        self.status_label = tk.Label(
            shell,
            textvariable=self.status,
            bg=VOID,
            fg=self.status_color,
            font=BODY_FONT,
            anchor="w",
        )
        self.status_label.pack(fill="x", pady=(15, 0))

        bottom = tk.Frame(shell, bg=VOID)
        bottom.pack(fill="x", pady=(8, 0))
        self.forget_button = self._button(
            bottom, "Forget saved details", self._forget, quiet=True
        )
        self.forget_button.pack(side="left")
        if self.store is None:
            self.forget_button.configure(state="disabled")
        self._button(bottom, "Close", self.destroy, quiet=True).pack(side="right")

    @staticmethod
    def _field_label(parent: tk.Widget, text: str) -> None:
        tk.Label(
            parent,
            text=text,
            bg=SLATE,
            fg=TEXT,
            font=HEADING_FONT,
            anchor="w",
        ).pack(fill="x")

    @staticmethod
    def _entry(
        parent: tk.Widget, variable: tk.StringVar, *, show: str = ""
    ) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            show=show,
            bg=RAISED,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=FROST,
            selectforeground=VOID,
            font=BODY_FONT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=FROST,
        )

    @staticmethod
    def _button(
        parent: tk.Widget,
        text: str,
        command,
        *,
        accent: bool = False,
        quiet: bool = False,
    ) -> tk.Button:
        background = FROST if accent else (VOID if quiet else RAISED)
        foreground = VOID if accent else (MUTED if quiet else TEXT)
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=foreground,
            activebackground=TEXT if accent else BORDER,
            activeforeground=VOID if accent else TEXT,
            disabledforeground=BORDER,
            font=HEADING_FONT,
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=13,
            pady=3,
            highlightthickness=1,
            highlightbackground=background if accent else BORDER,
            highlightcolor=TEXT,
        )

    def _set_status(self, text: str, color: str) -> None:
        self.status.set(text)
        self.status_label.configure(fg=color)

    def _set_runtime_progress(self, fraction: float) -> None:
        self.runtime_fraction = max(0.0, min(float(fraction), 1.0))
        self._redraw_runtime_progress()

    def _redraw_runtime_progress(self, _event=None) -> None:
        width = max(self.runtime_progress.winfo_width() - 2, 1)
        self.runtime_progress.coords(
            self.runtime_progress_fill,
            0,
            0,
            int(width * self.runtime_fraction),
            8,
        )

    def _load_runtime_state(self) -> None:
        try:
            installed = self.runtime_detector(verify_hashes=False)
        except (OSError, RuntimeInstallError):
            installed = None
        if installed is None:
            return
        if installed.acceleration == "cuda":
            message = "Local speech engine is ready with NVIDIA acceleration."
        else:
            message = "Local speech engine is ready in compatibility mode."
        self.runtime_status.set(message)
        self.runtime_status_label.configure(fg=SUCCESS)
        self._set_runtime_progress(1.0)
        self.runtime_button.configure(text="Repair local voice tools")

    def _install_runtime(self) -> None:
        if self.runtime_installing:
            return
        self.runtime_installing = True
        self.runtime_button.configure(state="disabled")
        self.runtime_status.set("Preparing local voice tools...")
        self.runtime_status_label.configure(fg=FROST)
        self._set_runtime_progress(0.0)
        worker = threading.Thread(
            target=run_runtime_install,
            args=(self.runtime_installer, self.runtime_events.put),
            name="VoiceCaptionsRuntimeInstall",
            daemon=True,
        )
        worker.start()
        self.after(75, self._drain_runtime_events)

    def _drain_runtime_events(self) -> None:
        finished = False
        while True:
            try:
                kind, payload = self.runtime_events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress" and isinstance(payload, InstallProgress):
                self.runtime_status.set(payload.message)
                self._set_runtime_progress(payload.fraction)
            elif kind == "error":
                self.runtime_status.set(str(payload))
                self.runtime_status_label.configure(fg=DANGER)
                finished = True
            elif kind == "done" and isinstance(payload, InstallOutcome):
                self._set_runtime_progress(1.0)
                if payload.adapter.ready:
                    acceleration = (
                        " with NVIDIA acceleration"
                        if payload.runtime.acceleration == "cuda"
                        else " in compatibility mode"
                    )
                    self.runtime_status.set(
                        "Local captions are ready"
                        + acceleration
                        + "; the Discord voice helper is ready."
                    )
                    runtime_color = SUCCESS
                else:
                    self.runtime_status.set(
                        "Local captions are ready. " + payload.adapter.detail
                    )
                    runtime_color = DANGER
                self.runtime_status_label.configure(fg=runtime_color)
                self.runtime_button.configure(text="Repair local voice tools")
                finished = True

        if finished:
            self.runtime_installing = False
            self.runtime_button.configure(state="normal")
        elif self.runtime_installing:
            self.after(75, self._drain_runtime_events)

    def _toggle_token(self) -> None:
        self.token_entry.configure(show="" if self.reveal_token.get() else "*")

    def _refresh_install_link(self, _event=None) -> None:
        try:
            url = build_bot_install_url(self.application_id.get())
        except CredentialValidationError:
            self.install_url.set("")
            self.copy_button.configure(state="disabled")
            return
        self.install_url.set(url)
        self.copy_button.configure(state="normal")

    def _load_saved_state(self) -> None:
        if self.store is None:
            return
        try:
            credentials = self.store.load()
        except CredentialStoreError:
            self._set_status(
                "Windows could not read the saved details. You can replace them here.",
                DANGER,
            )
            return
        if credentials is None:
            return
        self.application_id.set(credentials.application_id)
        self._refresh_install_link()
        self._set_status(
            "Discord details are already saved. Paste a token only to replace them.",
            SUCCESS,
        )

    def _save(self) -> None:
        if self.store is None:
            return
        try:
            result = save_setup(
                self.store, self.application_id.get(), self.bot_token.get()
            )
        except CredentialValidationError as error:
            self._set_status(str(error), DANGER)
            return
        except CredentialStoreError:
            self._set_status(
                "Windows could not save the details in Credential Manager.", DANGER
            )
            return
        self.install_url.set(result.install_url)
        self.copy_button.configure(state="normal")
        self.bot_token.set("")
        self.reveal_token.set(False)
        self._toggle_token()
        self._set_status(
            "Saved in Windows Credential Manager. Copy the install link next.",
            SUCCESS,
        )

    def _copy_link(self) -> None:
        url = self.install_url.get()
        if not url:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.update_idletasks()
        except tk.TclError:
            self._set_status("Windows could not copy the install link.", DANGER)
            return
        self._set_status("Install link copied. Paste it into your browser.", SUCCESS)

    def _forget(self) -> None:
        if self.store is None:
            return
        try:
            self.store.delete()
        except CredentialStoreError:
            self._set_status("Windows could not remove the saved details.", DANGER)
            return
        self.application_id.set("")
        self.bot_token.set("")
        self.install_url.set("")
        self.copy_button.configure(state="disabled")
        self._set_status("Saved Discord details removed from this PC.", MUTED)


def main() -> int:
    startup_error = ""
    try:
        store: CredentialStore | None = WindowsCredentialStore()
    except CredentialStoreError:
        store = None
        startup_error = "Windows Credential Manager is unavailable on this PC."
    window = DiscordSetupWindow(store, startup_error=startup_error)
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
