# Vemal's Ears local STT boundary

This package is the privacy boundary between per-user Discord audio and the
caption event stream. It is usable now with a deterministic fake backend for
UI and pipeline testing. The real backend targets the official whisper.cpp
`whisper-server`, but stays unavailable unless local assets pass capability
detection.

## Privacy and concurrency guarantees

- Audio enters as owned PCM16 mono `bytearray` data. File paths are not part of
  the public audio contract.
- The service wipes and releases each audio buffer after success, failure,
  explicit cancellation, caller cancellation, or timeout.
- Transcript and audio content are never logged or written by this package.
- Same-speaker segments are ordered behind one lock. Different speakers use a
  bounded pool, with two concurrent speakers by default.
- The queue is bounded, and the deadline includes both queue time and inference
  time. An unavailable or unhealthy backend fails closed.
- Every segment passes a required local Silero speech gate before STT. Silence
  and non-speech return an empty result without invoking the transcription
  model. Gate load or inference failures fail closed.
- Warcraft terms and in-memory character/player hints are passed through a
  short, sanitized whisper initial prompt.

The Python process cannot guarantee erasure of copies made before ownership is
transferred or copies retained by a native decoder. The contract avoids making
those copies and clears the buffer it owns.

## Real local backend

`WhisperCppBackend` starts a small pool of persistent local server processes.
Each process is bound to `127.0.0.1`, loads the model once, and receives a WAV
assembled in memory over HTTP. The launcher deliberately does not pass
`--convert`, so the official server reads WAV bytes from memory instead of
using ffmpeg temporary files. Credential-like environment variables are not
inherited by the child process. Temperature fallback is disabled for short,
independent Discord utterances. Build b4938 hard-codes `no_context=true` in
the server; it does not expose a `--no-context` command-line option.

Run the companion as a normal desktop process, not as administrator. The
official whisper.cpp server also recommends avoiding elevated execution.

Set these variables through the companion setup layer:

```text
VOICE_CAPTIONS_WHISPER_SERVER=C:\path\to\whisper-server.exe
VOICE_CAPTIONS_WHISPER_FALLBACK_SERVER=C:\path\to\cpu\whisper-server.exe
VOICE_CAPTIONS_WHISPER_MODEL=C:\path\to\ggml-large-v3-turbo-q5_0.bin
VOICE_CAPTIONS_WHISPER_DLL=C:\path\to\cpu\whisper.dll
VOICE_CAPTIONS_WHISPER_VAD_MODEL=C:\path\to\ggml-silero-v6.2.0.bin
```

The VAD wrapper uses the C API exported by the same pinned whisper.cpp build.
It loads the bundled CPU backend explicitly, maintains one VAD context per
concurrent speaker, and clears its temporary float PCM copy after every call.

The setup window performs the only download action. It pins official b4938
Windows archives, the large-v3-turbo q5 model, and Silero V6.2 to immutable
URLs and exact SHA-256 hashes. NVIDIA CUDA 12.4 is selected only when a
compatible driver is detected. A verified CPU server is always retained and
is tried automatically if the GPU server cannot start. Nothing downloads on
import, startup, or capability detection. Official server documentation is at:

https://github.com/ggml-org/whisper.cpp/tree/master/examples/server

## Offline checks

These use no Discord token, model, server, socket, or external network:

```text
python -m unittest discover -s VoiceCaptions/stt/tests -v
python -m VoiceCaptions.stt.harness
```

The HTTP client accepts an injected `WhisperHttpTransport`; tests exercise WAV
multipart construction and response validation with a fake transport.
