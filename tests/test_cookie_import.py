"""Offline credential-import tests: no real cookies, accounts or network calls."""
import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("cookie_import", Path(__file__).parents[1] / "scripts" / "update_cookie.py")
IMPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORT)


class CookieImportTests(unittest.TestCase):
    """Preserve bind mounts and unrelated metadata while refreshing credentials."""

    def setUp(self):
        """Create disposable fixtures. Args: None. Returns: None."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.target = self.root / "cookie.txt"
        self.source = self.root / "incoming.txt"
        self.tokens = self.root / "tokens.json"
        self.source.write_text("SID=new; SAPISID=new-sapi; __Secure-1PSID=new-session")
        self.original = {"cookie": "SID=old; SAPISID=old-sapi", "sapisid": "old-sapi", "auth_user": 2,
                         "accounts_cookie": "SID=old-accounts; SAPISID=old-sapi", "custom": "keep"}
        self.target.write_text(json.dumps(self.original))
        self.tokens.write_text(json.dumps({"at": "AOvx-fresh-test"}))

    def test_dry_run_does_not_change_credentials(self):
        """Require explicit apply. Args: None. Returns: None."""
        before = self.target.read_bytes()
        report = IMPORT.update(self.source, self.target, self.tokens)
        self.assertFalse(report["applied"])
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.bak-*")), [])

    def test_apply_preserves_inode_backup_permissions_and_metadata(self):
        """Keep the existing bind-mount inode and secure backup. Args: None. Returns: None."""
        before, inode = self.target.read_bytes(), self.target.stat().st_ino
        report = IMPORT.update(self.source, self.target, self.tokens, apply=True)
        self.assertTrue(report["applied"])
        self.assertTrue(report["inode_preserved"])
        self.assertEqual(self.target.stat().st_ino, inode)
        self.assertEqual(Path(report["backup"]).read_bytes(), before)
        self.assertEqual(stat.S_IMODE(Path(report["backup"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        updated = json.loads(self.target.read_text())
        self.assertEqual(updated["auth_user"], 2)
        self.assertEqual(updated["custom"], "keep")
        self.assertEqual(updated["sapisid"], "new-sapi")
        self.assertEqual(updated["xsrf_token"], "AOvx-fresh-test")
        self.assertNotIn("accounts_cookie", updated)
        self.assertTrue(report["stale_accounts_cookie_removed"])

    def test_matching_accounts_cookie_is_preserved(self):
        """Do not discard a jar belonging to the same session identity. Args: None. Returns: None."""
        self.original["accounts_cookie"] = "SID=accounts; SAPISID=new-sapi"
        self.target.write_text(json.dumps(self.original))
        IMPORT.update(self.source, self.target, self.tokens, apply=True)
        self.assertEqual(json.loads(self.target.read_text())["accounts_cookie"], self.original["accounts_cookie"])

    def test_invalid_input_is_rejected_before_backup_or_write(self):
        """Missing credentials do not replace a working file. Args: None. Returns: None."""
        before = self.target.read_bytes()
        self.source.write_text("SID=only-one-cookie")
        with self.assertRaises(ValueError):
            IMPORT.update(self.source, self.target, self.tokens, apply=True)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.bak-*")), [])

    def test_unrecognized_token_is_not_claimed_as_fresh(self):
        """Do not confuse other page tokens with SNlM0e. Args: None. Returns: None."""
        self.tokens.write_text(json.dumps({"at": "AFWL-not-xsrf"}))
        report = IMPORT.update(self.source, self.target, self.tokens)
        self.assertFalse(report["fresh_xsrf_available"])
