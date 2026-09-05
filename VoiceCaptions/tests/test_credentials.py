import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from VoiceCaptions.credentials import (
    BOT_INSTALL_PERMISSIONS,
    CREDENTIAL_TARGET,
    CredentialStoreError,
    CredentialValidationError,
    DiscordCredentials,
    WindowsCredentialStore,
    build_bot_install_url,
    validate_credentials,
)

APPLICATION_ID = "123456789012345678"
BOT_TOKEN = "test.token.value_that_is_long_enough"


class FakeCredentialApi:
    def __init__(self):
        self.record = None
        self.deleted = []

    def write(self, target, username, secret):
        self.record = (target, username, secret)

    def read(self, target):
        if self.record is None or self.record[0] != target:
            return None
        return self.record[1], self.record[2]

    def delete(self, target):
        self.deleted.append(target)
        if self.record is not None and self.record[0] == target:
            self.record = None


class FailingWriteApi(FakeCredentialApi):
    def write(self, target, username, secret):
        raise OSError(f"failure for {username} and {secret.decode('utf-8')}")


class CredentialsTests(unittest.TestCase):
    def test_save_uses_exact_windows_target_and_round_trips(self):
        api = FakeCredentialApi()
        store = WindowsCredentialStore(api)
        expected = validate_credentials(APPLICATION_ID, BOT_TOKEN)

        store.save(expected)

        self.assertEqual(
            (CREDENTIAL_TARGET, APPLICATION_ID, BOT_TOKEN.encode("utf-8")),
            api.record,
        )
        self.assertEqual(expected, store.load())

    def test_delete_removes_only_the_voice_caption_credential(self):
        api = FakeCredentialApi()
        store = WindowsCredentialStore(api)
        store.save(DiscordCredentials(APPLICATION_ID, BOT_TOKEN))

        store.delete()

        self.assertEqual([CREDENTIAL_TARGET], api.deleted)
        self.assertIsNone(store.load())

    def test_install_url_has_only_the_required_permissions_and_scopes(self):
        url = urlparse(build_bot_install_url(APPLICATION_ID))
        query = parse_qs(url.query)

        self.assertEqual("https", url.scheme)
        self.assertEqual("discord.com", url.netloc)
        self.assertEqual("/oauth2/authorize", url.path)
        self.assertEqual([APPLICATION_ID], query["client_id"])
        self.assertEqual([str(BOT_INSTALL_PERMISSIONS)], query["permissions"])
        self.assertEqual(["bot applications.commands"], query["scope"])
        self.assertEqual({"client_id", "permissions", "scope"}, set(query))
        self.assertEqual(1_051_648, BOT_INSTALL_PERMISSIONS)

    def test_application_id_and_token_are_redacted_from_repr(self):
        credentials = DiscordCredentials(APPLICATION_ID, BOT_TOKEN)

        rendered = repr(credentials)

        self.assertNotIn(APPLICATION_ID, rendered)
        self.assertNotIn(BOT_TOKEN, rendered)
        self.assertEqual(rendered, str(credentials))
        self.assertIn("redacted", rendered)

    def test_validation_errors_never_echo_entered_values(self):
        bad_application_id = "not-an-id"
        bad_token = "this token contains a private space"

        with self.assertRaises(CredentialValidationError) as app_error:
            validate_credentials(bad_application_id, BOT_TOKEN)
        with self.assertRaises(CredentialValidationError) as token_error:
            validate_credentials(APPLICATION_ID, bad_token)

        self.assertNotIn(bad_application_id, str(app_error.exception))
        self.assertNotIn(APPLICATION_ID, str(token_error.exception))
        self.assertNotIn(bad_token, str(token_error.exception))

    def test_storage_failure_does_not_leak_values(self):
        store = WindowsCredentialStore(FailingWriteApi())

        with self.assertRaises(CredentialStoreError) as caught:
            store.save(DiscordCredentials(APPLICATION_ID, BOT_TOKEN))

        message = str(caught.exception)
        self.assertNotIn(APPLICATION_ID, message)
        self.assertNotIn(BOT_TOKEN, message)

    def test_save_has_no_plaintext_file_fallback(self):
        api = FakeCredentialApi()
        store = WindowsCredentialStore(api)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            previous = Path.cwd()
            try:
                os.chdir(root)
                store.save(DiscordCredentials(APPLICATION_ID, BOT_TOKEN))
            finally:
                os.chdir(previous)

            self.assertEqual([], list(root.iterdir()))

    def test_invalid_saved_bytes_fail_closed_without_echoing_them(self):
        api = FakeCredentialApi()
        api.record = (CREDENTIAL_TARGET, APPLICATION_ID, b"private\xffvalue")
        store = WindowsCredentialStore(api)

        with self.assertRaises(CredentialStoreError) as caught:
            store.load()

        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
