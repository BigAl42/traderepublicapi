"""Tests for secret redaction helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "tr-adapter"
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

from redact import redact_secrets  # noqa: E402


class RedactSecretsTest(unittest.TestCase):
    def test_session_token(self):
        text = 'sessionToken":"abc123secret"'
        self.assertIn("***", redact_secrets(text))
        self.assertNotIn("abc123secret", redact_secrets(text))

    def test_phone_and_env(self):
        text = "TR_PHONE=+491234567890 TR_PIN=1234"
        out = redact_secrets(text)
        self.assertIn("TR_PHONE=***", out)
        self.assertIn("TR_PIN=***", out)
        self.assertNotIn("491234567890", out)

    def test_bearer(self):
        self.assertEqual(redact_secrets("Bearer super-secret"), "Bearer ***")


if __name__ == "__main__":
    unittest.main()
