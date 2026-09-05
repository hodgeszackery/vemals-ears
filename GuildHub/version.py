"""The one place the client version is written.

No imports on purpose. Anything that needs the number can have it
cheaply - the companion's Preferences panel reads it without pulling in
client.py's websocket stack - and packaging/publish_release.py has
exactly one line to rewrite.

There used to be a second copy in GuildHub/__init__.py. It said 0.1.0
through two releases while the real one moved to 0.2.2, because the
release tooling only ever updated one of them. Do not add a third.
"""

CLIENT_VERSION = "0.5.8"
