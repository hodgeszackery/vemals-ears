"""Shared test roots for CI planning and local test discovery.

Register roots here; new test files inside a root are discovered automatically.
The Discord adapter uses its own Node runner.
"""

PYTHON_TEST_ROOTS = (
    ("GuildHub/tests", "GuildHub.tests"),
    ("VoiceCaptions/tests", "VoiceCaptions.tests"),
    ("VoiceCaptions/stt/tests", "VoiceCaptions.stt.tests"),
    ("tests", "tests"),
)

REQUIRED_TEST_ROOTS = (
    *((relative, "test_*.py") for relative, _prefix in PYTHON_TEST_ROOTS),
    ("VoiceCaptions/discord_adapter/test", "*.test.js"),
    ("VoiceCaptions/discord_adapter/tests", "*.test.mjs"),
)
