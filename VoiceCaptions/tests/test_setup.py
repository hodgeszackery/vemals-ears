import inspect
import queue
import unittest

import VoiceCaptions.setup as setup_module
from VoiceCaptions.credentials import DiscordCredentials
from VoiceCaptions.setup import save_setup
from VoiceCaptions.setup import run_runtime_install
from VoiceCaptions.runtime_installer import (
    AdapterPreparation,
    InstallOutcome,
    InstallProgress,
    InstalledRuntime,
    RuntimeInstallError,
)

APPLICATION_ID = "123456789012345678"
BOT_TOKEN = "test.token.value_that_is_long_enough"


class RecordingStore:
    def __init__(self):
        self.saved = None

    def save(self, credentials):
        self.saved = credentials

    def load(self):
        return None

    def delete(self):
        self.saved = None


class SetupBoundaryTests(unittest.TestCase):
    def test_save_boundary_returns_link_without_returning_secret(self):
        store = RecordingStore()

        result = save_setup(store, APPLICATION_ID, BOT_TOKEN)

        self.assertEqual(DiscordCredentials(APPLICATION_ID, BOT_TOKEN), store.saved)
        self.assertIn(APPLICATION_ID, result.install_url)
        self.assertNotIn(BOT_TOKEN, result.install_url)
        self.assertEqual({"install_url"}, set(result.__dict__))

    def test_setup_module_does_not_open_browser_or_contact_discord(self):
        source = inspect.getsource(setup_module)

        self.assertNotIn("import webbrowser", source)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("requests.", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn(".env", source)

    def test_runtime_install_runs_only_through_explicit_button_worker(self):
        source = inspect.getsource(setup_module.DiscordSetupWindow)

        self.assertIn('"Install local voice tools"', source)
        self.assertIn("self._install_runtime", source)
        self.assertIn("threading.Thread", source)
        self.assertNotIn("runtime_installer()", source.split("def _install_runtime", 1)[0])

    def test_runtime_worker_reports_progress_and_safe_result(self):
        events = queue.Queue()
        runtime = InstalledRuntime(
            root=setup_module.Path("runtime"),
            server_path=setup_module.Path("runtime/whisper-server.exe"),
            model_path=setup_module.Path("runtime/ggml-base.en.bin"),
        )

        def installer(*, progress):
            progress(InstallProgress("download", "Downloading...", 0.5))
            return InstallOutcome(
                runtime=runtime,
                adapter=AdapterPreparation(True, "Discord voice helper is ready."),
            )

        run_runtime_install(installer, events.put)

        first = events.get_nowait()
        second = events.get_nowait()
        self.assertEqual("progress", first[0])
        self.assertEqual(0.5, first[1].fraction)
        self.assertEqual(("done", InstallOutcome(runtime, AdapterPreparation(True, "Discord voice helper is ready."))), second)

    def test_runtime_progress_is_redrawn_when_the_canvas_is_laid_out(self):
        class StubCanvas:
            def __init__(self):
                self.width = 1
                self.drawn = None

            def winfo_width(self):
                return self.width

            def coords(self, item, *points):
                self.drawn = points

        class StubWindow:
            _set_runtime_progress = setup_module.DiscordSetupWindow._set_runtime_progress
            _redraw_runtime_progress = setup_module.DiscordSetupWindow._redraw_runtime_progress

            def __init__(self):
                self.runtime_progress = StubCanvas()
                self.runtime_progress_fill = 1

        window = StubWindow()
        window._set_runtime_progress(1.0)
        self.assertEqual((0, 0, 1, 8), window.runtime_progress.drawn)

        window.runtime_progress.width = 548
        window._redraw_runtime_progress()

        self.assertEqual((0, 0, 546, 8), window.runtime_progress.drawn)
        self.assertIn(
            'self.runtime_progress.bind("<Configure>", self._redraw_runtime_progress)',
            inspect.getsource(setup_module.DiscordSetupWindow),
        )

    def test_runtime_worker_hides_unexpected_error_details(self):
        events = queue.Queue()

        def installer(*, progress):
            raise ValueError("secret token and private path")

        run_runtime_install(installer, events.put)

        kind, message = events.get_nowait()
        self.assertEqual("error", kind)
        self.assertEqual("Local voice tools could not be installed.", message)
        self.assertNotIn("secret", message)

    def test_runtime_worker_keeps_known_installer_message_nontechnical(self):
        events = queue.Queue()

        def installer(*, progress):
            raise RuntimeInstallError("The official download could not be completed.")

        run_runtime_install(installer, events.put)

        self.assertEqual(
            ("error", "The official download could not be completed."),
            events.get_nowait(),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
