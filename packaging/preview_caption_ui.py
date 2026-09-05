"""Render caption setup surfaces without reading or writing live state."""

from __future__ import annotations

import argparse
import queue
import sys
import time
import tkinter as tk
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("surface", choices=("overlay", "placement", "pairing"))
    result.add_argument("--screenshot", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = tk.Tk()
    root.withdraw()
    if args.surface == "overlay":
        from VoiceCaptions.events import CaptionEvent
        from VoiceCaptions.overlay import CaptionOverlay

        view = CaptionOverlay(
            root,
            queue.Queue(),
            desktop_fallback=True,
            preview=True,
        )
        view.target_rect = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        now = time.monotonic()
        view.board.ingest(
            CaptionEvent(
                "mira",
                "Mira",
                "Mira",
                "HEALER",
                "Spread for circles, then stack on me.",
                True,
                1,
                0.0,
                wow_class="priest",
            ),
            now=now,
        )
        view.board.ingest(
            CaptionEvent(
                "drex",
                "Drex",
                "Drex",
                "TANK",
                "Boss turning left.",
                False,
                1,
                0.0,
                wow_class="warrior",
            ),
            now=now,
        )
        view.session_state = "active"
        view.render(now)
    elif args.surface == "placement":
        from VoiceCaptions.placement import PlacementWindow

        PlacementWindow(root, preview=True)
    else:
        from GuildHub.setup import PairingWindow

        PairingWindow(root)
    if args.surface != "overlay":
        root.deiconify()

    def finish() -> None:
        root.update()
        if args.screenshot is not None:
            from PIL import ImageGrab

            left = root.winfo_rootx()
            top = root.winfo_rooty()
            right = left + root.winfo_width()
            bottom = top + root.winfo_height()
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(bbox=(left, top, right, bottom)).save(
                args.screenshot,
                format="PNG",
            )
        root.after(20, root.destroy)

    root.after(180, finish)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
