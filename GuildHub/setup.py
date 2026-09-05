"""Nontechnical pairing fallback for Vemal's Ears members."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from GuildHub.client import GuildHubClientError, pair_device
from GuildHub.credentials import (
    DeviceCredentialStoreError,
    DeviceCredentialValidationError,
    WindowsDeviceCredentialStore,
)

BG = "#080b10"
PANEL = "#101823"
PANEL_ALT = "#182433"
PANEL_HOVER = "#203044"
BORDER = "#2a3c52"
TEXT = "#eaf3fa"
MUTED = "#91a4b7"
ACCENT = "#35c6f4"
DANGER = "#ff6b72"
SUCCESS = "#55d58a"

STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path(__file__).resolve().parent)) / "GearHelper"
PAIR_SIGNAL = STATE_DIR / "vemals-ears-paired.signal"


def _signal_companion() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PAIR_SIGNAL.write_text(str(time.time()), encoding="ascii")
    except OSError:
        pass


class PairingWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Pair Vemal's Ears")
        root.configure(bg=BG)
        root.geometry("640x330")
        root.minsize(560, 315)
        root.resizable(True, True)

        card = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=22, pady=22)
        hero = tk.Frame(card, bg=PANEL)
        hero.pack(fill="x", padx=20, pady=(19, 13))
        mark = tk.Canvas(
            hero,
            width=48,
            height=48,
            bg=PANEL,
            highlightthickness=0,
        )
        mark.pack(side="left", padx=(0, 13))
        self.draw_listening_mark(mark)
        copy = tk.Frame(hero, bg=PANEL)
        copy.pack(side="left", fill="x", expand=True)
        tk.Label(
            copy,
            text="Connect to your guild",
            bg=PANEL,
            fg=TEXT,
            font=("Bahnschrift", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            copy,
            text="One private invitation connects captions, consent, and raid replies.",
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=440,
            font=("Segoe UI Variable", 9),
        ).pack(fill="x", pady=(3, 0))

        field_label = tk.Frame(card, bg=PANEL)
        field_label.pack(fill="x", padx=20, pady=(0, 6))
        tk.Label(
            field_label,
            text="Guild invitation",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Variable", 9, "bold"),
        ).pack(side="left")
        tk.Label(
            field_label,
            text="ONE USE",
            bg=PANEL_ALT,
            fg=ACCENT,
            padx=7,
            pady=2,
            font=("Segoe UI Variable", 8),
        ).pack(side="right")

        self.link = tk.Entry(
            card,
            bg=PANEL_ALT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            font=("Segoe UI Variable", 10),
        )
        self.link.pack(fill="x", padx=20, ipady=8)
        self.link.focus_set()

        self.status = tk.Label(
            card,
            text="Private by design - the invitation is never written to a log.",
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=540,
            font=("Segoe UI Variable", 9),
        )
        self.status.pack(fill="x", padx=20, pady=(9, 13))

        buttons = tk.Frame(card, bg=PANEL)
        buttons.pack(side="bottom", fill="x", padx=20, pady=(0, 19))
        self.make_button(buttons, "Cancel", root.destroy).pack(side="right")
        self.pair_button = self.make_button(
            buttons,
            "Connect guild",
            self.pair,
            primary=True,
        )
        self.pair_button.pack(side="right", padx=(0, 8))
        root.bind("<Return>", lambda _event: self.pair())
        root.bind("<Escape>", lambda _event: root.destroy())

    @staticmethod
    def draw_listening_mark(canvas: tk.Canvas) -> None:
        canvas.create_oval(2, 2, 46, 46, fill=PANEL_ALT, outline=BORDER, width=2)
        canvas.create_arc(
            6, 6, 42, 42,
            start=214,
            extent=112,
            style="arc",
            outline=ACCENT,
            width=2,
        )
        canvas.create_line(13, 18, 35, 18, fill=TEXT, width=2, capstyle="round")
        canvas.create_line(13, 24, 31, 24, fill=ACCENT, width=2, capstyle="round")
        canvas.create_line(13, 30, 26, 30, fill=MUTED, width=2, capstyle="round")
        canvas.create_line(26, 30, 22, 35, fill=MUTED, width=2, capstyle="round")

    @staticmethod
    def make_button(
        parent: tk.Frame,
        label: str,
        command,
        *,
        primary: bool = False,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=label,
            command=command,
            bg=ACCENT if primary else PANEL_ALT,
            fg=BG if primary else TEXT,
            activebackground="#70d8f7" if primary else PANEL_HOVER,
            activeforeground=BG if primary else TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=ACCENT if primary else BORDER,
            highlightcolor=TEXT if primary else ACCENT,
            padx=16,
            pady=7,
            takefocus=True,
            cursor="hand2",
            font=("Segoe UI Variable", 9, "bold"),
        )

    def confirm(self, credentials, existing_credentials) -> bool:
        server_host = urlsplit(credentials.server_url).netloc
        replacement = (
            "\n\nThis replaces the guild currently saved on this computer."
            if existing_credentials is not None
            else ""
        )
        return bool(
            messagebox.askyesno(
                "Confirm guild",
                "Pair this computer with:\n\n"
                + credentials.guild_name
                + "\n\nGuild server:\n"
                + server_host
                + replacement
                + "\n\nThe one-use invitation has already been accepted by the "
                "guild server and cannot be reused if you cancel.",
                parent=self.root,
            )
        )

    def pair(self) -> None:
        private_link = self.link.get().strip()
        self.link.delete(0, "end")
        if not private_link:
            self.status.config(text="Paste a one-use invitation first.", fg=DANGER)
            return
        self.pair_button.config(state="disabled", text="Checking...")
        self.root.update_idletasks()
        try:
            credentials = asyncio.run(
                pair_device(
                    private_link,
                    WindowsDeviceCredentialStore(),
                    confirm=self.confirm,
                )
            )
        except (
            GuildHubClientError,
            DeviceCredentialStoreError,
            DeviceCredentialValidationError,
        ) as error:
            self.status.config(text=str(error), fg=DANGER)
        except Exception:
            self.status.config(
                text="Pairing could not be completed. Ask your guild admin for a new link.",
                fg=DANGER,
            )
        else:
            _signal_companion()
            self.status.config(
                text="Paired with " + credentials.guild_name + ".",
                fg=SUCCESS,
            )
            messagebox.showinfo(
                "Vemal's Ears",
                "This computer is now paired with " + credentials.guild_name + ".",
                parent=self.root,
            )
            self.root.destroy()
            return
        finally:
            private_link = ""
        self.pair_button.config(state="normal", text="Connect guild")


def main() -> int:
    root = tk.Tk()
    PairingWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
