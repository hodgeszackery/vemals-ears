"""Guild-hosted services for the WoW companion suite."""

PROTOCOL_VERSION = 1

# The client version lives in GuildHub/client.py and nowhere else.
# A copy here said 0.1.0 through two releases while the real one
# moved to 0.2.2, and packaging/publish_release.py only knows how to
# update the real one - so the copy could only ever drift further.
# Nothing imported it. Import it from .client if you need it.
