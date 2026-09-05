import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from VoiceCaptions.coordinator import AdapterProtocolDecoder, SegmentAssembler


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "discord_adapter"


NODE_SCRIPT = textwrap.dedent(
    """
    import { PassThrough } from "node:stream";
    import OpusScript from "opusscript";
    import { PcmJsonlAudioSink } from "./src/audio_sink.js";
    import { JsonlEmitter } from "./src/protocol.js";

    const encoder = new OpusScript(48000, 2, OpusScript.Application.AUDIO);
    const source = Buffer.alloc(960 * 2 * 2);
    for (let offset = 0; offset < source.length; offset += 2) {
      source.writeInt16LE(4000, offset);
    }
    const packet = encoder.encode(source, 960);
    const stream = new PassThrough({ objectMode: true });
    const sink = new PcmJsonlAudioSink({ emitter: new JsonlEmitter() });
    const task = sink.acceptOpusStream({
      segmentId: "123456789012345678.234567890123456789.1.1",
      globalSequence: 1,
      audienceGeneration: 2,
      audienceRevision: 3,
      userId: "234567890123456789",
      displayName: "Mira",
      character: {
        characterName: "Mira",
        realm: "Moon Guard",
        wowClass: "priest",
        role: "healer",
      },
      stream,
    });
    for (let index = 0; index < 5; index += 1) {
      stream.write(packet);
    }
    stream.end();
    await task;
    encoder.delete();
    """
).strip()


def safe_node_environment():
    allowed = {
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
    return {name: value for name, value in os.environ.items() if name.upper() in allowed}


class NodePcmContractTests(unittest.TestCase):
    def test_real_node_audio_events_assemble_in_python(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("Node is not installed")
        if not (ADAPTER / "node_modules" / "opusscript" / "package.json").is_file():
            self.skipTest("Discord adapter dependencies are not installed")

        result = subprocess.run(
            [node, "--input-type=module", "--eval", NODE_SCRIPT],
            cwd=ADAPTER,
            env=safe_node_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(0, result.returncode, "Node PCM fixture failed safely")
        self.assertEqual("", result.stderr)

        decoder = AdapterProtocolDecoder()
        assembler = SegmentAssembler()
        completed = None
        for line in result.stdout.splitlines():
            candidate = assembler.accept(decoder.parse_line(line))
            if candidate is not None:
                self.assertIsNone(completed)
                completed = candidate

        self.assertIsNotNone(completed)
        self.assertEqual(3200, len(completed.pcm))
        self.assertEqual(2, completed.audience_generation)
        self.assertEqual(3, completed.audience_revision)
        self.assertEqual("Mira", completed.speaker.display_name)
        self.assertEqual("Mira", completed.speaker.character_name)
        self.assertEqual("healer", completed.speaker.role)
        completed.wipe()
        self.assertEqual(bytearray(), completed.pcm)


if __name__ == "__main__":
    unittest.main(verbosity=2)
