"""Screens for moving a whole host from one PC to another.

The old PC shows its address and a one-time code as two separate fields; the
new, empty PC types them. Everything after that is automatic. These screens
only call the sequences in Hosting.transfer_flow: every rule about what may
start, stop or be erased lives there, never here.

Nothing private is drawn or kept by this module. The one-time code and the
release code are shown only in their own fields, are added to the wizard's
scrub set so no message can echo them, and are cleared once used. Progress
shows fixed wording chosen here by stage name, never text from the other PC.
"""

from __future__ import annotations

import re
import threading
import time
import tkinter as tk
from tkinter import ttk

# Imported here, on the UI thread, like the wizard's own imports: a first
# import inside a worker thread can collect Tk objects off their thread.
from Hosting.control import HostError
from Hosting.transfer import TransferError


STEPS = {
    "transfer-card": "Move this host / Check",
    "transfer-code": "Move this host / Transfer code",
    "transfer-moving": "Move this host / Moving",
    "transfer-receive": "Receive a host / Connect",
    "transfer-confirm": "Receive a host / Check",
    "transfer-receiving": "Receive a host / Moving",
}
GENERIC_ERROR = "The move could not continue. Check both PCs, then try again."
MOVED_HERE = "This host now runs on this PC. In Discord, run /vem start once to bring captions back."
CLOSE_JOIN_SECONDS = 8
ABANDON_JOIN_SECONDS = 15

SENDER_STAGES = {
    "waiting": "Waiting for the other PC. Type the address and the code there.",
    "connected": "The other PC is connecting...",
    "wrong_code": "A wrong code was typed on the other PC. Waiting for another try.",
    "confirmed": "Connected. Waiting for Move it here on the other PC.",
    "peer_downloads": "The other PC is copying the member downloads...",
    "sending": "Pausing new pairings on this host...",
    "worker_stopped": "Captions are stopped here. Copying this host...",
    "bundle": "Sending this host to the other PC...",
    "peer_importing": "The other PC is saving its copy...",
    "peer_services": "The other PC is getting ready to start...",
    "staged": "The other PC has a full copy. Handing over...",
    "released": "Handed over. The other PC is starting the host.",
    "stopping": "Stopping this host for good...",
    "moved": "Done.",
}
RECEIVER_STAGES = {
    "preparing": "Finishing the speech download first...",
    "downloads": "Copying the member downloads from the old PC...",
    "receiving": "The old PC is stopping captions and sending its copy...",
    "importing": "Saving the host on this PC...",
    "services": "Getting this PC ready to start...",
    "staged": "This PC has a full copy. Waiting for the old PC to let go...",
    "waiting_release": "This PC has a full copy. Waiting for the old PC to let go...",
    "activating": "Starting the host on this PC...",
    "active": "Done.",
}
# After these the new PC keeps its copy whatever happens, so Cancel is gone.
RECEIVER_COMMITTED = frozenset({"staged", "waiting_release", "activating", "active"})


def split_entry(address: str, code: str) -> tuple:
    """(address, code) from what was typed or pasted into the two boxes.

    Both may be pasted as one line into either box, in either order. A part
    with a dot is the address; everything else is the code. This only sorts
    text on this PC: the code is never put into an address or sent anywhere.
    """
    host, symbols = "", []
    for token in re.split(r"[\s,;]+", str(address) + " " + str(code)):
        if not token or token.endswith(":"):
            continue
        bare = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", token)
        if "." in bare:
            parts = bare.split("/")
            host = host or parts[0]
            symbols.extend(part for part in parts[1:] if part)
        else:
            symbols.extend(part for part in bare.split("/") if part)
    return host, "".join(symbols)


def clock_text(seconds) -> str:
    seconds = max(0, int(seconds))
    return str(seconds // 60) + ":" + str(seconds % 60).zfill(2)


def error_text(error: BaseException) -> str:
    """Only messages written for the operator are shown; anything else gets fixed wording."""
    text = str(error) if isinstance(error, (TransferError, HostError)) else ""
    return text or GENERIC_ERROR


class TransferBackend:
    """Everything the screens call, bound to this PC's HostController."""

    def __init__(self, host) -> None:
        # Created on the UI thread, so the sequences are imported there too.
        from Hosting import control, transfer, transfer_flow, transfer_net
        self.host = host
        self.control, self.transfer, self.flow, self.net = control, transfer, transfer_flow, transfer_net

    def status(self) -> dict:
        return self.flow.status(self.host)

    def sender(self):
        return self.flow.Sender(self.host)

    def receiver(self):
        return self.flow.Receiver(self.host)

    def keep_hosting(self) -> dict:
        return self.flow.keep_hosting(self.host)

    def finish_move(self) -> dict:
        return self.flow.finish_move(self.host)

    def erase_archive(self, checked_other_pc: bool = False) -> None:
        if checked_other_pc:
            self.flow.erase_archive(self.host, checked_other_pc=True)
        else:
            self.flow.erase_archive(self.host)

    def runtime_staged(self) -> bool:
        """Whether the speech runtime for a transfer is really on this PC, in staging."""
        try:
            return bool(self.host.staged_runtime_ready(self.transfer.staging_path(self.host.state)))
        except (HostError, OSError, ValueError):
            return False

    def discard_unused_staging(self) -> bool:
        return self.flow.discard_unused_staging(self.host)

    def activate(self, release_code=None) -> dict:
        return self.flow.activate(self.host, release_code=release_code)

    def activate_by_override(self, attestation: str) -> dict:
        return self.flow.activate_by_override(self.host, attestation=attestation)

    def start_tunnel(self) -> dict:
        return self.flow.start_tunnel(self.host)

    def discard_pending(self) -> None:
        self.flow.discard_pending(self.host)

    def public_answers(self, origin: str) -> bool:
        return self.net.public_answers(origin)

    def tunnel_withheld(self) -> bool:
        """A received host whose public tunnel is still off: the old address was not verified quiet.

        Read from the durable mark in the host settings, which only
        "Connect members" clears. The transfer receipt plays no part, so the
        answer stays right whatever happens to the receipt afterwards.
        """
        try:
            settings = self.host.read_settings()
            return bool(settings.get("configured") and settings.get("tunnel_enabled")
                        and settings.get("tunnel_withheld"))
        except (HostError, ValueError, KeyError):
            # Not knowing is not a reason to keep the app from opening; the
            # controller itself never starts a withheld tunnel.
            return False


class TransferUi:
    def __init__(self, wizard, backend=None) -> None:
        self.wizard = wizard
        if backend is None and hasattr(wizard.backend, "state"):
            backend = TransferBackend(wizard.backend)
        self.backend = backend
        root = wizard.root
        for key in ("transfer_address", "transfer_code", "transfer_release_code", "transfer_attestation"):
            wizard.values[key] = tk.StringVar(root, value="")
        self.progress = tk.StringVar(root, value="")
        self.prepare_text = tk.StringVar(root, value="")
        self.clock = tk.StringVar(root, value="")
        self.reach = tk.StringVar(root, value="")
        self.erase_confirmed = tk.BooleanVar(root, value=False)
        self.erase_checked = tk.BooleanVar(root, value=False)
        self.moved_acknowledged = False
        self.discard_confirmed = tk.BooleanVar(root, value=False)
        self.sender = None
        self.receiver = None
        self.card = None
        self.thread = None
        self.released = False
        self.committed = False
        self.cancelling = False
        self.tunnel_withheld = False
        self.tunnel_reason = ""         # "answers", "unchecked" or "" (not known in this session)
        self.prepare_state = ""         # "", "checking", "preparing", "ready", "failed", "blocked"
        self.prepare_detail = ""
        self.prepare_thread = None
        self.prepare_run = None         # {"abandoned": bool} of the running speech download
        self.prepared = False           # set by the preparing thread itself
        self.receive_cancelled = False
        self.cancel_button = None
        self.waiting = {}
        self.shown = []                 # variables of read-only fields on the current page
        self.connect_button = None
        self.unlock_button = None
        self.unlocked = False
        self.checking = False
        self.timers = {}                # page -> pending "after" id

    # -- plumbing ------------------------------------------------------------

    def _colors(self):
        from Hosting import wizard
        return wizard

    def _post(self, call) -> None:
        self.wizard.events.put(("call", call))

    def _job(self, work, done, failed) -> bool:
        """Run one backend call off the UI thread; done/failed run on the UI thread."""
        w = self.wizard
        if w.busy or self.backend is None:
            return False
        w.busy = True

        def finish(callback, value):
            w.busy = False
            self.thread = None
            callback(value)

        def run():
            try:
                result = work()
            except Exception as error:  # noqa: BLE001 - shown as fixed or operator wording
                text = error_text(error)
                self._post(lambda: finish(failed, text))
            else:
                self._post(lambda: finish(done, result))

        self.thread = threading.Thread(target=run, name="VemalsEarsHostTransfer", daemon=True)
        self.thread.start()
        return True

    def _notice(self, text) -> None:
        self.wizard.notice.set(self.wizard._safe(text))

    def _hide(self, *values) -> None:
        """No message may ever echo a code, in any spelling it was typed in."""
        for value in values:
            text = str(value or "").strip()
            for form in (text, text.upper(), text.replace("-", ""), text.upper().replace("-", "")):
                if len(form) >= 8:
                    self.wizard.secrets.add(form)

    def _page(self, page, title, description) -> None:
        w = self.wizard
        w._clear(page, "")
        self.shown = []
        self.connect_button = self.unlock_button = None
        w._heading(title, description)

    def _text(self, text, *, color=None, size=10, bold=False, pady=(0, 10)):
        w = self.wizard
        label = w._label(w.body, text, color=color or self._colors().MUTED, size=size, bold=bold)
        label.pack(anchor="w", pady=pady)
        return label

    def _rows(self, pairs) -> None:
        w, c = self.wizard, self._colors()
        for label, value in pairs:
            row = tk.Frame(w.body, bg=c.FIELD)
            row.pack(fill="x", pady=4)
            w._label(row, label, bold=True).pack(side="left", padx=14, pady=11)
            w._label(row, str(value), color=c.GOLD).pack(side="right", padx=14)

    def _display(self, parent, label, value, *, size=13):
        """A read-only field for something the operator reads off this screen."""
        w, c = self.wizard, self._colors()
        frame = tk.Frame(parent, bg=c.PANEL)
        w._label(frame, label, bold=True).pack(anchor="w", pady=(0, 6))
        variable = tk.StringVar(w.root, value=value)
        self.shown.append(variable)
        tk.Entry(frame, textvariable=variable, state="readonly", readonlybackground=c.FIELD,
                 fg=c.TEXT, relief="flat", bd=0, highlightthickness=1, highlightbackground=c.BORDER,
                 highlightcolor=c.GOLD, font=("Consolas", size, "bold"), justify="center").pack(fill="x", ipady=10)
        return frame

    def _checkbox(self, text, variable):
        c = self._colors()
        widget = tk.Checkbutton(self.wizard.body, text=text, variable=variable, bg=c.PANEL, fg=c.TEXT,
                                selectcolor=c.FIELD, activebackground=c.PANEL, activeforeground=c.GOLD,
                                wraplength=550, justify="left", anchor="w")
        widget.bind("<Configure>", lambda event: widget.configure(wraplength=max(240, event.width - 32)))
        widget.pack(fill="x", pady=(10, 4))
        return widget

    def _progress_page(self, page, title, description, note) -> None:
        w, c = self.wizard, self._colors()
        self._page(page, title, description)
        w.progress_bar = ttk.Progressbar(w.body, mode="indeterminate", style="Host.Horizontal.TProgressbar")
        w.progress_bar.pack(fill="x", pady=(12, 18))
        w.progress_bar.start(18)
        tk.Label(w.body, textvariable=self.progress, fg=c.TEXT, bg=c.PANEL, anchor="w",
                 justify="left", wraplength=690).pack(fill="x")
        self._text(note, pady=(30, 0))

    def _tick(self, page, update, delay=500) -> None:
        """Call update() now and again every `delay` ms while this page is shown."""
        w = self.wizard
        pending = self.timers.pop(page, None)
        if pending is not None:
            w.root.after_cancel(pending)
        if w.closed or w.page != page:
            return
        if update() is not False:
            self.timers[page] = w.root.after(delay, lambda: self._tick(page, update, delay))

    def stop_timers(self) -> None:
        for pending in self.timers.values():
            try:
                self.wizard.root.after_cancel(pending)
            except tk.TclError:
                pass
        self.timers = {}

    def _hostname(self) -> str:
        settings = self.wizard.settings
        return str(settings.get("hostname") or "your public address")

    def _origin(self) -> str:
        settings = self.wizard.settings
        return str(settings.get("public_http_url") or "https://" + str(settings.get("hostname", ""))).rstrip("/")

    # -- opening the app -----------------------------------------------------

    def read_status(self) -> dict:
        """This PC's place in a move. Runs on the loading thread, never the UI thread."""
        if self.backend is None:
            return {"view": ""}
        current = dict(self.backend.status())
        if current.get("view") == "interrupted" and current.get("state") == "offered":
            # A code that was shown and never used: nothing was changed, so the
            # unused offer is withdrawn without asking.
            self.backend.keep_hosting()
            current = dict(self.backend.status())
        current["tunnel_withheld"] = bool(not current.get("view") and self.backend.tunnel_withheld())
        return current

    def open(self, status: dict) -> bool:
        """Show the screen an unfinished move needs. False when there is none."""
        self.tunnel_withheld = bool(status.get("tunnel_withheld"))
        view = status.get("view", "")
        if view == "interrupted":
            self.show_interrupted(status)
        elif view == "released":
            self.show_released()
        elif view == "moved":
            self.show_moved(status)
        elif view == "restore":
            self.show_restore()
        elif view == "waiting":
            self.show_waiting(status)
        elif view == "activating":
            self.show_activating()
        else:
            return False
        return True

    def close_question(self):
        """What closing the window would interrupt, or None."""
        if self.sender is not None:
            if self.released:
                return ("This host is finishing its move. Closing now interrupts it.\n"
                        "Reopen the Host app to finish.")
            return ("A move is in progress. Closing now cancels it and keeps this host here.\n"
                    "If it cannot be put back in time, reopen the Host app and choose Keep hosting here.")
        if self.receiver is not None and self.wizard.busy:
            return ("A host is being received. Closing now interrupts the move.\n"
                    "Reopen the Host app on both PCs to see what to do next.")
        if self.prepare_state in ("checking", "preparing"):
            return "Speech setup is still downloading. Closing now stops it; it continues next time."
        return None

    def shutdown(self) -> None:
        """The window is closing: stop what can still be stopped cleanly."""
        self.stop_timers()
        thread = self.thread
        try:
            if self.sender is not None and not self.released:
                self.sender.cancel()
            elif self.receiver is not None and not self.committed:
                self.receiver.cancel()
            else:
                return
        except Exception:  # noqa: BLE001 - closing must never fail
            return
        if thread is not None:
            thread.join(CLOSE_JOIN_SECONDS)

    def _reopen(self, notice="") -> None:
        self.wizard._load_again(notice=notice)

    # -- dashboard -----------------------------------------------------------

    def dashboard(self, actions) -> None:
        """The dashboard's part of a move: the Move button, and a withheld public connection."""
        w, c = self.wizard, self._colors()
        if w.settings.get("mode") == "remote-worker":
            return      # a worker-only replacement has no relay state to move
        w._button(actions, "Move this host to another PC", self.show_move,
                  disabled=self.backend is None).pack(side="right")
        if self.tunnel_withheld:
            row = tk.Frame(w.body, bg=c.FIELD, highlightthickness=1, highlightbackground=c.BORDER)
            row.pack(fill="x", pady=(0, 10))
            w._button(row, "Connect members", self.show_tunnel_wait, primary=True).pack(
                side="right", padx=(8, 16), pady=12)
            w._label(row, "Members cannot connect yet. The public connection is off until the old PC stops answering.",
                     size=9, color=c.GOLD).pack(side="left", fill="x", expand=True, padx=(16, 0), pady=12)

    # -- old PC: move --------------------------------------------------------

    def show_move(self) -> None:
        w, c = self.wizard, self._colors()
        if w.busy or self.backend is None:
            return
        settings = w.settings
        self._page("transfer-card", "Move this host to another PC",
                   "Check that this is the host you want to move.")
        self._rows((("Host name", settings.get("guild_name", "")),
                    ("Discord server ID", settings.get("guild_id", "")),
                    ("Application ID", settings.get("application_id", "")),
                    ("Public address", settings.get("hostname", ""))))
        self._text("Everything moves: the bot, member pairings, consent choices and settings. "
                   "Members do not pair again. This PC stops hosting as soon as the other PC has a full "
                   "copy; the other PC starts right after.",
                   pady=(14, 10))
        self._text("On the other PC, install Vemal's Ears Host and choose Receive a host from another PC.")
        if not settings.get("tunnel_enabled", True):
            self._text("This host uses your own HTTPS forwarding. After the move you must point "
                       + self._hostname() + " at the other PC yourself. Members stay offline until you do.",
                       color=c.GOLD)
        w._button(w.navigation, "Back", lambda: w.show_dashboard(w.current_status)).pack(side="left")
        w._button(w.navigation, "Show transfer code", self.show_code, primary=True).pack(side="right")

    def show_code(self) -> None:
        if self.sender is not None:
            return

        def work():
            sender = self.backend.sender()
            return sender, sender.open_offer()

        if self._job(work, self._offer_ready, self._notice):
            self._notice("Getting a transfer code...")

    def _offer_ready(self, opened) -> None:
        w, c = self.wizard, self._colors()
        self.sender, offer = opened
        self.released = self.cancelling = False
        self._hide(offer["code"])
        self._page("transfer-code", "Type these on the other PC",
                   "On the other PC choose Receive a host from another PC, then type the address and the code.")
        fields = tk.Frame(w.body, bg=c.PANEL)
        fields.pack(fill="x")
        fields.columnconfigure(0, weight=3, uniform="transfer")
        fields.columnconfigure(1, weight=2, uniform="transfer")
        self._display(fields, "Address", offer["hostname"]).grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self._display(fields, "Code", offer["code"], size=16).grid(row=0, column=1, sticky="ew", padx=(12, 0))
        tk.Label(w.body, textvariable=self.clock, fg=c.GOLD, bg=c.PANEL, anchor="w").pack(fill="x", pady=(12, 0))
        self.progress.set(SENDER_STAGES["waiting"])
        tk.Label(w.body, textvariable=self.progress, fg=c.TEXT, bg=c.PANEL, anchor="w",
                 justify="left", wraplength=690).pack(fill="x", pady=(18, 0))
        self._text("The code works once. Nothing moves until the other PC has shown you this host and "
                   "you chose Move it here there.", pady=(18, 0))
        if offer.get("forwarding"):
            self._text("After the move, point " + offer["hostname"] + " at the other PC yourself.",
                       color=c.GOLD, pady=(10, 0))
        self._cancel_button()
        expires = time.monotonic() + int(offer.get("ttl_seconds", 600))

        def countdown():
            left = expires - time.monotonic()
            self.clock.set("This code works for another " + clock_text(left) + "." if left > 0
                           else "This code has expired.")
            return left > 0

        self._tick("transfer-code", countdown)
        sender = self.sender
        self._job(lambda: sender.run(lambda stage: self._post(lambda: self._sender_stage(stage))),
                  self._moved, self._move_failed)

    def _cancel_button(self) -> None:
        w = self.wizard
        self.cancel_button = w._button(w.navigation, "Cancel and keep hosting here", self.cancel_move)
        self.cancel_button.pack(side="left")

    def _sender_stage(self, stage) -> None:
        w = self.wizard
        if self.sender is None or stage not in SENDER_STAGES or w.page not in ("transfer-code", "transfer-moving"):
            return
        if stage == "confirmed" and w.page == "transfer-code":
            # The code is used up: take it off the screen.
            self._progress_page("transfer-moving", "Moving this host",
                                "Keep both PCs on. This page follows the move by itself.",
                                "Captions pause for a minute or two during the move. "
                                "Typed chat keeps working until this host stops.")
            self._cancel_button()
        if stage == "released":
            self.released = True
            for child in w.navigation.winfo_children():
                child.destroy()
            w._button(w.navigation, "Finishing...", lambda: None, primary=True, disabled=True).pack(side="right")
        if not self.cancelling or self.released:
            self.progress.set(SENDER_STAGES[stage])

    def cancel_move(self) -> None:
        if self.sender is None or self.released or self.cancelling:
            return
        self.cancelling = True
        self.sender.cancel()
        self.progress.set("Cancelling. Putting this host back as it was...")
        try:
            self.cancel_button.configure(state="disabled")
        except tk.TclError:
            pass

    def _moved(self, result) -> None:
        self.sender = None
        self.show_moved(result, acknowledged=bool(result.get("acknowledged")))

    def _move_failed(self, text) -> None:
        self.sender = None
        self.released = False
        # The receipt decides what this PC shows next: the dashboard again, or a
        # screen to finish or undo an interrupted move.
        self._reopen(text)

    # -- old PC: after or instead of a clean move ------------------------------

    def show_moved(self, status, acknowledged=None) -> None:
        w, c = self.wizard, self._colors()
        if acknowledged is None:
            acknowledged = status.get("acknowledged") is True
        self.moved_acknowledged = bool(acknowledged)
        self.erase_confirmed.set(False)
        self.erase_checked.set(False)
        self._hide(status.get("release_code", ""))
        self._page("transfer-moved", "This host has moved",
                   "The other PC is the host now. This PC no longer runs captions or the relay.")
        if acknowledged:
            self._text("The other PC confirmed that it has taken over.", color=c.GREEN)
            self._text("Release code, only needed if the other PC still shows Waiting for the old PC:")
        else:
            self._text("If the other PC shows Waiting for the old PC, type this release code there:", color=c.TEXT)
        self._display(w.body, "Release code", status.get("release_code", ""), size=10).pack(fill="x", pady=(0, 6))
        tk.Frame(w.body, height=1, bg=c.BORDER).pack(fill="x", pady=16)
        self._text("Locked archive", color=c.TEXT, bold=True, pady=(0, 6))
        self._text("This PC keeps a locked copy of the host in case the other PC fails. It cannot start. "
                   "Erase it once the other PC works. A new host cannot be set up here before that.")
        self._checkbox("The other PC is hosting. Erase this PC's copy for good.", self.erase_confirmed)
        if not acknowledged:
            # Erasing also destroys the release code, the only way left to
            # start a copy that is still waiting on the other PC.
            self._checkbox("The other PC never confirmed the move to this PC. I checked it myself: it shows "
                           "its dashboard, not Waiting for the old PC.", self.erase_checked)
        w._button(w.body, "Erase archive", self.erase_archive).pack(anchor="w", pady=(8, 0))
        w._button(w.navigation, "Close", w.close, primary=True).pack(side="right")

    def erase_archive(self) -> None:
        if not self.erase_confirmed.get():
            self._notice("Tick the box to confirm that the other PC is hosting.")
            return
        unconfirmed = not self.moved_acknowledged
        if unconfirmed and not self.erase_checked.get():
            self._notice("The other PC never confirmed the move. Check it, then tick the second box too.")
            return
        work = (lambda: self.backend.erase_archive(checked_other_pc=True)) if unconfirmed else self.backend.erase_archive
        if self._job(work, lambda _result: self._reopen("The archive was erased. This PC is empty."), self._notice):
            self._notice("Erasing the archive...")

    def show_interrupted(self, status) -> None:
        w, c = self.wizard, self._colors()
        can_finish = status.get("can_finish") is True
        taken_over = status.get("taken_over") is True
        self._page("transfer-interrupted", "Transfer interrupted",
                   "The move to another PC stopped half way. Captions stay stopped on this PC until you choose.")
        w._choice("Keep hosting here", "Cancel the move and start this host again. Choose this when the other "
                  "PC is not hosting. This PC first checks that no other PC answers at your public address.",
                  "Keep hosting here", self.keep_hosting)
        if taken_over:
            finish = "Another PC is already hosting. Stop this PC for good and lock its copy as an archive."
        elif can_finish:
            finish = ("Stop this host for good and let the other PC take over. You then type a release code "
                      "there. Never choose this if the copy on the other PC was discarded: no host would be left.")
        else:
            finish = "Not possible: the other PC never received a full copy of this host."
        w._choice("Finish the move", finish, "Finish the move", self.finish_move, disabled=not can_finish)
        self._text("If the other PC was already started with its emergency step, do not keep hosting here: "
                   "two bots would run at once.", color=c.GOLD, size=9, pady=(6, 0))

    def show_released(self) -> None:
        w = self.wizard
        self._page("transfer-released", "Finish the move",
                   "This host was handed to the other PC but did not finish stopping here.")
        self._text("Nothing here can start again. Finish the move to stop this PC for good and see the release code.")
        w._button(w.navigation, "Finish the move", self.finish_move, primary=True).pack(side="right")

    def show_restore(self) -> None:
        w = self.wizard
        self._page("transfer-restore", "Finish putting this host back",
                   "The move was cancelled, but this host did not fully start again.")
        self._text("Nothing was moved. Choose Keep hosting here to start this host again.")
        w._button(w.navigation, "Keep hosting here", self.keep_hosting, primary=True).pack(side="right")

    def keep_hosting(self) -> None:
        # A refusal can change what this PC may do next (another PC was found
        # hosting), so the page is drawn again from the receipt either way.
        if self._job(self.backend.keep_hosting,
                     lambda _result: self._reopen("Nothing was moved. This host keeps running here."),
                     self._reopen):
            self._notice("Checking that no other PC is hosting, then putting this host back...")

    def finish_move(self) -> None:
        if self._job(self.backend.finish_move, lambda result: self.show_moved(result, acknowledged=False),
                     self._notice):
            self._notice("Stopping this host for good...")

    # -- new PC: receive -----------------------------------------------------

    def show_receive(self) -> None:
        w, c = self.wizard, self._colors()
        if w.busy:
            return
        self.receiver = self.card = None
        self.committed = False
        self._page("transfer-receive", "Receive a host from another PC",
                   "On the old PC open Vemal's Ears Host, choose Move this host to another PC, "
                   "then Show transfer code.")
        fields = tk.Frame(w.body, bg=c.PANEL)
        fields.pack(fill="x")
        fields.columnconfigure(0, weight=3, uniform="transfer")
        fields.columnconfigure(1, weight=2, uniform="transfer")
        frame, entry = w._field(fields, "transfer_address", "Address")
        frame.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        frame, _entry = w._field(fields, "transfer_code", "Code")
        frame.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        entry.focus_set()
        self._text("Type both as shown on the old PC. You can also paste them together into either box.",
                   size=9, pady=(8, 18))
        tk.Label(w.body, textvariable=self.prepare_text, fg=c.MUTED, bg=c.PANEL, anchor="w",
                 justify="left", wraplength=690).pack(fill="x")
        w._button(w.navigation, "Back", w.takeover_ui.show_choices).pack(side="left")
        self.connect_button = w._button(w.navigation, "Connect", self.connect, primary=True,
                                        disabled=self.backend is None)
        self.connect_button.pack(side="right")
        self._prepare()

    def _prepare(self) -> None:
        """Check that this PC is empty, then fetch the speech runtime while the operator types."""
        if self.backend is None or self.prepare_state in ("checking", "preparing", "ready"):
            self._prepare_shown()
            return
        self.prepare_state = "checking"
        self._prepare_shown()
        run = self.prepare_run = {"abandoned": False}
        self.prepare_thread = threading.Thread(target=self._prepare_work, args=(run,),
                                               name="VemalsEarsHostPrepare", daemon=True)
        self.prepare_thread.start()

    def _prepare_work(self, run) -> None:
        state = "blocked"

        def progress(message):
            if run["abandoned"]:
                raise TransferError("Speech setup was stopped.")
            self._post(lambda: self.prepare_text.set(self.wizard._safe("This PC is ready. " + str(message))))

        def show(*arguments):
            if not run["abandoned"]:
                self._post(lambda: None if run["abandoned"] else self._prepare_set(*arguments))

        try:
            receiver = self.backend.receiver()
            receiver.preflight()
            state = "failed"
            show("preparing")
            receiver.prepare(progress)
        except Exception as error:  # noqa: BLE001 - shown as fixed or operator wording
            show(state, error_text(error))
        else:
            if not run["abandoned"]:
                self.prepared = True
            show("ready")
        finally:
            if run["abandoned"]:
                self._discard_staging()

    def _discard_staging(self) -> None:
        try:
            self.backend.discard_unused_staging()
        except Exception:  # noqa: BLE001 - tidying up must never stop a setup
            pass

    def abandon_receive(self) -> None:
        """A new host is being set up here instead of receiving one.

        Runs on the setup thread. Stops the background speech download that
        the Receive page started and removes its staging copy (about 1.2 GB),
        which nothing would use or remove otherwise. A staging directory left
        by an earlier session goes the same way; one that holds host state or
        belongs to a transfer in progress is never touched.
        """
        if self.backend is None:
            return
        run, thread = self.prepare_run, self.prepare_thread
        if run is not None:
            run["abandoned"] = True
        self.prepare_run = self.prepare_thread = None
        self.prepared = False
        self.prepare_state = ""
        if thread is not None:
            thread.join(ABANDON_JOIN_SECONDS)
            if thread.is_alive():
                return      # it removes the staging copy itself as soon as it stops
        self._discard_staging()

    def _prepare_set(self, state, detail="") -> None:
        self.prepare_state, self.prepare_detail = state, detail
        self._prepare_shown()

    def _prepare_shown(self) -> None:
        w, state = self.wizard, self.prepare_state
        detail = w._safe(self.prepare_detail)
        self.prepare_text.set({
            "": "",
            "checking": "Checking this PC...",
            "preparing": "This PC is ready. Preparing " + w.takeover_ui.gpu_label()
                         + " GPU speech recognition in the background (up to about 1.2 GB)...",
            "ready": "This PC is ready.",
            "failed": "Speech setup stopped: " + detail + " It is tried again when you move the host.",
            "blocked": detail,
        }[state])
        if self.connect_button is not None and self.connect_button.winfo_exists():
            usable = self.backend is not None and state in ("preparing", "ready", "failed")
            self.connect_button.configure(state="normal" if usable else "disabled")
        if w.page != "transfer-receive":
            return
        for child in w.navigation.winfo_children():
            if child.cget("text") == "Check again":
                child.destroy()
        if state == "blocked":
            self.prepare_state = ""
            w._button(w.navigation, "Check again", self._prepare).pack(side="right", padx=(0, 10))

    def connect(self) -> None:
        w = self.wizard
        if w.busy or self.backend is None:
            return
        if self.prepare_state not in ("preparing", "ready", "failed"):
            self._notice("This PC is still being checked. Try again in a moment.")
            return
        address, code = split_entry(w.values["transfer_address"].get(), w.values["transfer_code"].get())
        if not address or not code:
            self._notice("Type the address and the code shown on the old PC.")
            return
        self._hide(code)
        w.values["transfer_address"].set(address)
        w.values["transfer_code"].set(code)

        def work():
            receiver = self.backend.receiver()
            return receiver, receiver.connect(address, code)

        def failed(text):
            w.values["transfer_code"].set("")
            self._notice(text)

        if self._job(work, self._connected, failed):
            self._notice("Connecting to the old PC...")

    def _connected(self, connected) -> None:
        w, c = self.wizard, self._colors()
        self.receiver, self.card = connected
        card = self.card
        w.values["transfer_code"].set("")
        self._page("transfer-confirm", card["guild_name"], "Check that this is your host before you move it.")
        self._rows((("Discord server ID", card["guild_id"]), ("Application ID", card["application_id"]),
                    ("Public address", card["public_origin"][len("https://"):])))
        self._text("Moving stops the host on the old PC and starts it here. Members keep their pairing. "
                   "Captions pause for a minute or two.", pady=(14, 10))
        if not card["tunnel_enabled"]:
            self._text("This host uses your own HTTPS forwarding. After the move, point "
                       + card["public_origin"][len("https://"):] + " at this PC, port " + str(card["port"]) + ".",
                       color=c.GOLD)
        w._button(w.navigation, "Cancel", self.cancel_receive).pack(side="left")
        w._button(w.navigation, "Move it here", self.accept, primary=True).pack(side="right")

    def cancel_receive(self) -> None:
        w = self.wizard
        receiver = self.receiver
        if receiver is None or self.committed:
            return
        self.receive_cancelled = True
        threading.Thread(target=receiver.cancel, name="VemalsEarsHostCancel", daemon=True).start()
        if w.busy:
            # The running move ends by itself with "cancelled" and returns to the first page.
            self.progress.set("Cancelling. Nothing is kept on this PC...")
            return
        self.receiver = self.card = None
        self.show_receive()

    def accept(self) -> None:
        w = self.wizard
        receiver = self.receiver
        if w.busy or receiver is None:
            return

        def stage(name):
            self._post(lambda: self._receiver_stage(name))

        kept = {"copy": False}

        def work():
            try:
                self._wait_prepared(stage)
            except BaseException:
                # accept() never ran, so its own cleanup did not either: the
                # old PC is told the move is off instead of holding its claim
                # open for READY.
                try:
                    receiver.cancel()
                except Exception:  # noqa: BLE001 - the preparation error is the one to show
                    pass
                raise
            try:
                return self._outcome(receiver.accept(stage))
            except BaseException:
                kept["copy"] = self._holds_copy()
                raise

        def failed(text):
            self.receiver = self.card = None
            self.prepared = False
            self.prepare_state = ""
            self.prepare_thread = self.prepare_run = None
            if kept["copy"]:
                # The move failed but this PC still holds the host, for example
                # released and not started: the receipt and the saved settings
                # decide the screen, never Receive, whose check would call the
                # only usable copy an old host to uninstall.
                self._reopen(text)
                return
            # A failure after the copy was published erased the speech runtime
            # with it, so this PC is checked and prepared again from scratch.
            self.show_receive()
            self._notice(text)

        self.receive_cancelled = False
        if not self._job(work, self._received, failed):
            return
        self.committed = False
        self._progress_page("transfer-receiving", "Moving the host here",
                            "Keep both PCs on. This page follows the move by itself.",
                            "Nothing starts on this PC until the old PC has stopped and let go.")
        self.progress.set(RECEIVER_STAGES["downloads" if self.prepared else "preparing"])
        w._button(w.navigation, "Cancel", self.cancel_receive).pack(side="left")

    def _holds_copy(self) -> bool:
        """On the job thread, after a failed move: whether this PC still holds the received host."""
        try:
            return bool(self.backend.status().get("view") or self.wizard.backend.read_settings().get("configured"))
        except Exception:  # noqa: BLE001 - a receipt that cannot be read is never taken as erased
            return True

    def _wait_prepared(self, stage) -> None:
        """The speech runtime must be complete before the copy is published beside it."""
        thread = self.prepare_thread
        while thread is not None and thread.is_alive():
            thread.join(0.2)
            if self.receive_cancelled:
                raise TransferError("The move was cancelled. Nothing was kept on this PC.")
        # What is on disk decides, never a flag of this screen: preparing
        # again is cheap when the runtime is already in staging.
        if not self.backend.runtime_staged():
            self.backend.receiver().prepare(lambda message: None)
            self._post(lambda: self._prepare_set("ready"))
        self.prepared = True
        stage("downloads")

    def _receiver_stage(self, stage) -> None:
        w = self.wizard
        if stage not in RECEIVER_STAGES or w.page != "transfer-receiving":
            return
        if stage in RECEIVER_COMMITTED and not self.committed:
            self.committed = True
            for child in w.navigation.winfo_children():
                child.destroy()
            w._button(w.navigation, "Working...", lambda: None, primary=True, disabled=True).pack(side="right")
        self.progress.set(RECEIVER_STAGES[stage])

    def _outcome(self, result) -> dict:
        """On the job thread: what the screens need after a move ends on this PC."""
        w = self.wizard
        if result.get("state") == "active":
            return {"result": result, "settings": w.backend.read_settings(), "status": w.backend.status()}
        return {"result": result, "settings": w.backend.read_settings(), "transfer": self.backend.status()}

    def _received(self, outcome) -> None:
        w = self.wizard
        self.receiver = self.card = None
        self.prepare_state = ""
        self.prepare_thread = self.prepare_run = None
        self.prepared = False
        w._accept_settings(outcome["settings"])
        result = outcome["result"]
        if result.get("state") != "active":
            self.show_waiting(outcome.get("transfer", {}))
            return
        self.tunnel_withheld = bool(result.get("tunnel_enabled") and not result.get("tunnel_started"))
        self.tunnel_reason = "unchecked" if result.get("public_unchecked") else "answers"
        w.current_status = outcome["status"]
        if result.get("forwarding"):
            self.show_repoint()
        elif self.tunnel_withheld:
            self.show_tunnel_wait()
        else:
            w.show_dashboard(outcome["status"])
            self._notice(MOVED_HERE)

    # -- new PC: after the move ------------------------------------------------

    def _dashboard_button(self, primary=True) -> None:
        w = self.wizard
        w._button(w.navigation, "Open dashboard", lambda: w.show_dashboard(w.current_status),
                  primary=primary).pack(side="right")

    def _watch_public(self, page, answered, quiet) -> None:
        """Keep checking the public address while this page is shown."""
        origin = self._origin()
        self.reach.set("Checking " + self._hostname() + "...")

        def check():
            try:
                answers = bool(self.backend.public_answers(origin))
            except Exception:  # noqa: BLE001 - an unreachable address is simply "no"
                answers = False
            self._post(lambda: result(answers))

        def result(answers):
            self.checking = False
            if not self.wizard.closed and self.wizard.page == page:
                self.reach.set(answered if answers else quiet)

        def poll():
            if self.backend is not None and not self.checking:
                self.checking = True
                threading.Thread(target=check, name="VemalsEarsHostCheck", daemon=True).start()

        self._tick(page, poll, delay=5000)

    def show_repoint(self) -> None:
        w, c = self.wizard, self._colors()
        host, port = self._hostname(), str(w.settings.get("port", ""))
        self._page("transfer-repoint", "Point " + host + " at this PC",
                   "The host runs here now. Members can connect again once your forwarding reaches this PC.")
        self._text("Send HTTPS and WebSocket traffic for " + host + " to http://127.0.0.1:" + port
                   + " on this PC, the same way it reached the old PC.", color=c.TEXT)
        tk.Label(w.body, textvariable=self.reach, fg=c.GOLD, bg=c.PANEL, anchor="w", justify="left",
                 wraplength=690, font=("Segoe UI", 12, "bold")).pack(fill="x", pady=(14, 14))
        self._text("This page keeps checking. In Discord, run /vem start once to bring captions back.")
        self._dashboard_button()
        self._watch_public("transfer-repoint", host + " answers. Members can connect.",
                           host + " does not reach a host yet.")

    def show_tunnel_wait(self) -> None:
        w, c = self.wizard, self._colors()
        if w.busy:
            return
        host = self._hostname()
        self._page("transfer-tunnel", "Waiting for the old PC to disconnect",
                   {"answers": "The host runs here, but the old PC still answers at " + host + ".",
                    "unchecked": "The host runs here, but this PC could not check that the old PC stopped "
                                 "answering at " + host + "."}.get(
                       self.tunnel_reason, "The host runs here, but its public connection is still off."))
        self._text("This PC's public connection stays off until it has seen that the old PC stopped answering, "
                   "so members are never split between two PCs.", color=c.TEXT)
        if self.tunnel_reason == "unchecked":
            self._text("Check this PC's internet connection, then choose Connect members.")
        else:
            self._text("Check that the old PC shows This host has moved. If it is still shutting down, "
                       "wait a minute.")
        tk.Label(w.body, textvariable=self.reach, fg=c.GOLD, bg=c.PANEL, anchor="w", justify="left",
                 wraplength=690, font=("Segoe UI", 12, "bold")).pack(fill="x", pady=(14, 14))
        self._dashboard_button(primary=False)
        w._button(w.navigation, "Connect members", self.connect_members, primary=True).pack(side="right", padx=(0, 10))
        self._watch_public("transfer-tunnel", "The old PC still answers at " + host + ".",
                           "No host answers at " + host + " right now. Choose Connect members.")

    def connect_members(self) -> None:
        def failed(text):
            self._notice(text)

        if self._job(lambda: self._outcome(self.backend.start_tunnel()), self._received, failed):
            self._notice("Checking the old PC, then connecting members...")

    def show_waiting(self, status) -> None:
        w, c = self.wizard, self._colors()
        if w.busy:
            return
        self.waiting = dict(status or {})
        staged = self.waiting.get("staged", True)
        name = str(w.settings.get("guild_name") or "the host")
        self._page("transfer-waiting", "Waiting for the old PC",
                   "This PC has a copy of " + name + ", but the old PC has not let go of it yet. "
                   "Nothing runs here until it does.")
        if staged:
            self._text("On the old PC, open Vemal's Ears Host. If it shows Transfer interrupted, choose "
                       "Finish the move. Then type the release code it shows:", color=c.TEXT)
            frame, _entry = w._field(w.body, "transfer_release_code", "Release code from the old PC")
            frame.pack(fill="x", pady=(4, 10))
            failures = int(self.waiting.get("code_failures", 0) or 0)
            if failures:
                # Counted for information only: the right code always works.
                self._text("Wrong release codes so far: " + str(failures) + ". The right code still works; "
                           "read it again on the old PC.", color=c.GOLD, size=9)
            w._button(w.navigation, "Start this host", self.activate_code, primary=True).pack(side="right")
        else:
            self._text("The copy on this PC is not complete. Discard it and start the move again.", color=c.GOLD)
        tk.Frame(w.body, height=1, bg=c.BORDER).pack(fill="x", pady=14)
        others = tk.Frame(w.body, bg=c.PANEL)
        others.pack(fill="x")
        w._button(others, "Discard and start again", self.show_discard).pack(side="left")
        if staged and (self.waiting.get("override") or {}).get("possible"):
            w._link(others, "The old PC is gone for good", "", command=self.show_override).pack(
                side="left", padx=(20, 0))

    def activate_code(self) -> None:
        w = self.wizard
        text = w.values["transfer_release_code"].get().strip()
        if not text:
            self._notice("Type the release code shown on the old PC.")
            return
        self._hide(text)

        def done(outcome):
            w.values["transfer_release_code"].set("")
            self._received(outcome)

        if self._job(lambda: self._outcome(self.backend.activate(text)), done, self._notice):
            self._notice("Checking the release code and starting this host...")

    def show_activating(self) -> None:
        w = self.wizard
        self._page("transfer-activating", "Start this host",
                   "The old PC let go of this host, but it did not finish starting here.")
        self._text("Nothing else is needed from the old PC.")
        w._button(w.navigation, "Start this host", self.resume_activation, primary=True).pack(side="right")

    def resume_activation(self) -> None:
        if self._job(lambda: self._outcome(self.backend.activate()), self._received, self._notice):
            self._notice("Starting this host...")

    def show_discard(self) -> None:
        w, c = self.wizard, self._colors()
        if w.busy:
            return
        self.discard_confirmed.set(False)
        self._page("transfer-discard", "Discard this copy?",
                   "This erases the copy of the host on this PC so the move can start again.")
        self._text("Do this when the old PC is hosting again, for example after Keep hosting here.", color=c.TEXT)
        self._text("If the old PC shows This host has moved, do NOT discard: this copy is then the only "
                   "working host. Go back and type the release code instead.", color=c.RED)
        self._checkbox("The old PC is still the host. Erase the copy on this PC.", self.discard_confirmed)
        w._button(w.navigation, "Back", lambda: self.show_waiting(self.waiting)).pack(side="left")
        w._button(w.navigation, "Discard and start again", self.discard).pack(side="right")

    def discard(self) -> None:
        if not self.discard_confirmed.get():
            self._notice("Tick the box to confirm that the old PC is still the host.")
            return
        if self._job(self.backend.discard_pending,
                     lambda _result: self._reopen("The copy was erased. This PC is empty again."), self._notice):
            self._notice("Erasing the copy on this PC...")

    def show_override(self) -> None:
        w, c = self.wizard, self._colors()
        override = dict(self.waiting.get("override") or {})
        if w.busy or not override.get("possible"):
            return
        w.values["transfer_attestation"].set("")
        self._page("transfer-override", "Emergency: the old PC is gone for good",
                   "Only for an old PC that is broken, lost or wiped and will never host again.")
        self._text("If the old PC is only switched off and hosts again later, two bots would run at once. "
                   "If the old PC can still be opened, go back and use Finish the move there instead.", color=c.RED)
        self._text("This step also refuses while the host's public address still answers, and it needs this "
                   "PC to be online to check that.")
        tk.Label(w.body, textvariable=self.clock, fg=c.GOLD, bg=c.PANEL, anchor="w", justify="left",
                 wraplength=690).pack(fill="x", pady=(6, 12))
        self._text("To confirm, type this sentence:", color=c.TEXT, pady=(0, 6))
        self._display(w.body, "Sentence", str(override.get("phrase", "")), size=11).pack(fill="x", pady=(0, 10))
        frame, _entry = w._field(w.body, "transfer_attestation", "Type the sentence here")
        frame.pack(fill="x")
        w._button(w.navigation, "Back", lambda: self.show_waiting(self.waiting)).pack(side="left")
        self.unlock_button = w._button(w.navigation, "Start this host without the old PC",
                                       self.activate_override, disabled=True)
        self.unlock_button.pack(side="right")
        self.unlocked = False
        unlocks = time.monotonic() + int(override.get("wait_remaining", 0) or 0)

        def countdown():
            left = unlocks - time.monotonic()
            if left > 0:
                self.clock.set("Wait for the old PC first. This step unlocks in " + clock_text(left + 0.999) + ".")
                return True
            self.unlocked = True
            self.clock.set("The wait is over. This step is unlocked.")
            if self.unlock_button is not None and self.unlock_button.winfo_exists():
                self.unlock_button.configure(state="normal")
            return False

        self._tick("transfer-override", countdown)

    def activate_override(self) -> None:
        w = self.wizard
        if not self.unlocked or w.page != "transfer-override":
            return
        text = w.values["transfer_attestation"].get()

        def done(outcome):
            w.values["transfer_attestation"].set("")
            self._received(outcome)

        if self._job(lambda: self._outcome(self.backend.activate_by_override(text)), done, self._notice):
            self._notice("Checking that the old PC is gone, then starting this host...")
