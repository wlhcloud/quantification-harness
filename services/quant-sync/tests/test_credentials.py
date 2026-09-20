import unittest

from quant_sync.credentials import CredentialCodec, PREFIX


class CredentialCodecTest(unittest.TestCase):
    def test_encrypts_and_decrypts(self):
        codec = CredentialCodec("a-long-test-only-secret")
        encrypted = codec.encode("upstream-token")
        self.assertTrue(encrypted.startswith(PREFIX))
        self.assertNotIn("upstream-token", encrypted)
        self.assertEqual(codec.decode(encrypted), "upstream-token")

    def test_plaintext_is_backward_compatible(self):
        self.assertEqual(CredentialCodec().decode("legacy-token"), "legacy-token")


if __name__ == "__main__":
    unittest.main()
