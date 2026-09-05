"""Render the real companion UI without starting services or touching state."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
import tkinter as tk
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_companion():
    name = "wow_companion_preview_module"
    loader = importlib.machinery.SourceFileLoader(
        name,
        str(ROOT / "WoW-Companion.pyw"),
    )
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--edition",
        choices=("member",),
        default="member",
    )
    result.add_argument(
        "--surface",
        choices=(
            "main",
            "activity",
            "settings",
            "quick-chat",
            "quick-chat-error",
        ),
        default="main",
    )
    result.add_argument(
        "--screenshot",
        type=Path,
        help="Optional PNG path for an ephemeral design review capture.",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    companion = load_companion()
    editions = {
        "member": companion.MEMBER_EDITION,
        "hosted": companion.HOSTED_EDITION,
        "local": companion.FULL_EDITION,
    }

    root = tk.Tk()
    root.withdraw()
    window = companion.CompanionWindow(
        root,
        edition=editions[args.edition],
        preview=True,
    )
    if args.screenshot is not None:
        # A fullscreen game can otherwise cover the review surface between
        # deiconify and ImageGrab, producing a convincing screenshot of the
        # wrong window.
        root.attributes("-topmost", True)
    root.update_idletasks()
    root.geometry(
        f"{root.winfo_width()}x{root.winfo_height()}+160+100"
    )

    def show_surface() -> None:
        if args.surface == "settings":
            window.open_settings()
        elif args.surface == "activity":
            window.toggle_activity()
        elif args.surface in ("quick-chat", "quick-chat-error"):
            window.open_speak_box()
            if args.surface == "quick-chat-error":
                window.speak_status.config(
                    text=(
                        "The guild message could not be delivered. Check your "
                        "guild connection, then try again. Your draft is safe."
                    ),
                    fg=companion.DANGER,
                )
        elif args.surface == "job-status":
            states = {
                "gear": ("Done - type /reload in WoW", companion.SUCCESS, "done"),
                "talents": ("Checking encounter logs", companion.WARNING, "checking"),
            }
            for service in window.services:
                if service.spec.key in states:
                    service.set_status(*states[service.spec.key])
        root.update()

        if args.screenshot is None:
            return
        try:
            from PIL import ImageGrab

            target = root
            if args.surface == "settings" and window.menu is not None:
                target = window.menu
            elif args.surface in ("quick-chat", "quick-chat-error"):
                target = window.speak_box
            if target is not root:
                root.attributes("-topmost", False)
                target.attributes("-topmost", True)
                target.lift()
            target.update()
            # Tk's borderless child windows can resolve to their owner's HWND
            # on Windows. Widget coordinates identify the requested review
            # surface directly and avoid capturing the main window instead.
            left = target.winfo_rootx()
            top = target.winfo_rooty()
            right = left + target.winfo_width()
            bottom = top + target.winfo_height()
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(bbox=(left, top, right, bottom)).save(
                args.screenshot,
                format="PNG",
            )
        finally:
            root.after(20, root.destroy)

    root.after(160, show_surface)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
