"""The standalone worker image must contain its complete headless runtime."""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CaptionWorkerImageTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / "GuildHub/Dockerfile.worker").read_text(encoding="ascii")

    def test_whisper_build_installs_every_shared_runtime_library(self):
        self.assertIn("-DGGML_BACKEND_DL=ON", self.dockerfile)
        self.assertIn("cmake --build build --parallel 2", self.dockerfile)
        self.assertNotIn("--target whisper-server", self.dockerfile)
        self.assertIn("cmake --install build", self.dockerfile)
        for library in ("libwhisper.so", "libggml.so", "libggml-base.so"):
            self.assertIn("test -e /opt/whisper/lib/" + library, self.dockerfile)
        self.assertIn("test -e /opt/whisper/bin/libggml-cpu.so", self.dockerfile)
        self.assertLess(
            self.dockerfile.index("cmake --build build --parallel 2"),
            self.dockerfile.index("cmake --install build"),
        )

    def test_runtime_contains_worker_sources_and_stays_non_root_and_private(self):
        for source in (
            "GuildHub/host.py", "GuildHub/worker_health.py",
            "GuildHub/quick_chat_worker.py", "VoiceCaptions/coordinator.py",
            "VoiceCaptions/prosody.py", "VoiceCaptions/stt/vocabulary.py",
            "VoiceCaptions/discord_adapter/src/",
        ):
            self.assertIn(source, self.dockerfile)
        self.assertRegex(self.dockerfile, r"(?m)^USER vemalsears$")
        self.assertNotRegex(self.dockerfile, r"(?m)^EXPOSE\s")
        self.assertIn(
            "VEMALS_EARS_WORKER_HEALTH_FILE=/tmp/vemals-ears-worker.state",
            self.dockerfile,
        )
        self.assertIn('CMD ["python", "-m", "GuildHub.host", "--watch"]', self.dockerfile)

    def test_copied_python_payload_imports_without_the_checkout_or_gui(self):
        # Copy only the Python sources named by the image. Importing directly
        # from the repository used to hide missing settings/member_overlay.
        sources = re.findall(
            r"(?m)^\s+((?:GuildHub|VoiceCaptions)/[a-z_./]+\.py)\s*\\?\s*$",
            self.dockerfile,
        )
        self.assertIn("GuildHub/host.py", sources)
        with tempfile.TemporaryDirectory() as folder:
            payload = Path(folder)
            for relative in sources:
                destination = payload / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)
            probe = "\n".join((
                "import pathlib, sys",
                "root = pathlib.Path(sys.argv[1]).resolve()",
                "sys.path.insert(0, str(root))",
                "from GuildHub import host",
                "assert callable(host.main)",
                "assert 'tkinter' not in sys.modules",
                "for name, module in tuple(sys.modules.items()):",
                "    if name.split('.')[0] in {'GuildHub', 'VoiceCaptions'}:",
                "        assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name",
                "print('HEADLESS-WORKER-IMPORT-OK')",
            ))
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", probe, str(payload)],
                cwd=payload, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("HEADLESS-WORKER-IMPORT-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
