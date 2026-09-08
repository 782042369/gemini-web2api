"""Import a locally staged Cookie header without leaking it or replacing a bind-mount inode.

Dry-run by default. --apply is an explicit credential update, not a deployment.
Unrelated JSON metadata is preserved. An accounts-domain jar with mismatched
SAPISID is removed from the active document (the complete original is backed up)
so keepalive cannot overwrite the new session with old account credentials.
"""
import argparse
import json
import os
import re
import time
from pathlib import Path


def pairs(header):
    """Validate cookie syntax. Args: Cookie header text. Returns: cookie-name/value map."""
    if not isinstance(header, str) or any(ord(char) < 32 or ord(char) == 127 for char in header):
        raise ValueError("Cookie header must contain no control characters")
    result = {}
    for item in header.split(";"):
        name, separator, value = item.strip().partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_!-]+", name):
            raise ValueError("invalid Cookie header item")
        result[name] = value
    return result


def update(source, target, token_source=None, apply=False):
    """Prepare/update one credential file. Args: input paths, apply flag. Returns: redacted result."""
    incoming = source.read_text().strip()
    fresh = pairs(incoming)
    if not all(fresh.get(name) for name in ("SID", "SAPISID", "__Secure-1PSID")):
        raise ValueError("required authentication cookies are missing")
    before = target.read_bytes()
    old = json.loads(before) if before.lstrip().startswith(b"{") else {"cookie": before.decode().strip()}
    data = dict(old)
    data["cookie"] = incoming
    data["sapisid"] = fresh["SAPISID"]
    tokens = json.loads(token_source.read_text()) if token_source else {}
    at = tokens.get("at")
    valid_at = isinstance(at, str) and at.startswith("AOvx")
    if valid_at:
        data["xsrf_token"] = at
    account_pairs = pairs(old["accounts_cookie"]) if old.get("accounts_cookie") else {}
    accounts_match = account_pairs.get("SAPISID") == fresh["SAPISID"] if account_pairs.get("SAPISID") else None
    if accounts_match is False:
        data.pop("accounts_cookie", None)
    report = {"applied": False, "cookie_count": len(fresh), "fresh_xsrf_available": valid_at,
              "identity_changed": old.get("sapisid") != fresh["SAPISID"],
              "accounts_cookie_preserved": "accounts_cookie" in data,
              "stale_accounts_cookie_removed": accounts_match is False,
              "accounts_sapisid_matches": accounts_match}
    if not apply:
        return report
    backup = target.with_name(target.name + ".bak-cookie-" + str(time.time_ns()))
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(before)
        output.flush()
        os.fsync(output.fileno())
    encoded = json.dumps(data, ensure_ascii=False).encode()
    with target.open("r+b") as output:
        if output.read() != before:
            raise RuntimeError("credential file changed concurrently; no update performed")
        inode = os.fstat(output.fileno()).st_ino
        output.seek(0)
        output.write(encoded)
        output.truncate()
        output.flush()
        os.fchmod(output.fileno(), 0o600)
        os.fsync(output.fileno())
    after = json.loads(target.read_text())
    if after.get("cookie") != incoming or after.get("sapisid") != fresh["SAPISID"]:
        raise RuntimeError("credential verification failed")
    report.update(applied=True, backup=str(backup), inode_preserved=target.stat().st_ino == inode)
    return report


def main():
    """Import a staged header. Args: CLI paths and explicit apply flag. Returns: exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--tokens", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = update(args.source, args.target, args.tokens, args.apply)
    except Exception as exc:
        print(json.dumps({"applied": False, "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
