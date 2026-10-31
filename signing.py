"""Host OpenPGP operations; private keys never enter packaging helpers."""

import os
import re
import subprocess
import tempfile
from pathlib import Path


class GpgSigner:
    def __init__(self, key, passphrase=None):
        result = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--fingerprint", "--list-secret-keys", "--", key],
            capture_output=True, text=True, check=True,
        )
        fingerprints = [line.split(":")[9] for line in result.stdout.splitlines() if line.startswith("fpr:")]
        if not fingerprints:
            raise RuntimeError("No private signing key matches GPG_KEY_ID")
        if sum(line.startswith("sec:") for line in result.stdout.splitlines()) != 1:
            raise RuntimeError("GPG_KEY_ID must select exactly one private key")
        self.fingerprint = fingerprints[0]
        selected = key.removeprefix("0x").rstrip("!").upper()
        if re.fullmatch(r"[A-F0-9]{16,64}", selected):
            matches = [value for value in fingerprints if value.endswith(selected)]
            if len(matches) != 1:
                raise RuntimeError("GPG_KEY_ID does not select a unique signing fingerprint")
            self.fingerprint = matches[0]
        self.selector = self.fingerprint + ("!" if key.endswith("!") else "")
        if not re.fullmatch(r"[A-F0-9]{40,64}", self.fingerprint):
            raise RuntimeError("GPG returned an invalid signing fingerprint")
        self.passphrase = passphrase
        self.public_key = subprocess.check_output([
            "gpg", "--batch", "--armor", "--export-options", "export-minimal",
            "--export", "--", self.fingerprint,
        ]).decode("ascii")

    def sign(self, data):
        """Return a detached binary signature using only the configured key."""
        command = ["gpg", "--batch", "--yes", "--local-user", self.selector, "--output", "-", "--detach-sign"]
        payload = None
        if self.passphrase is not None:
            if "\n" in self.passphrase or "\r" in self.passphrase:
                raise RuntimeError("GPG_PASSPHRASE must be a single line")
            command += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
            payload = self.passphrase.encode() + b"\n"
        with tempfile.TemporaryDirectory(prefix="yesb-sign-") as directory:
            source = Path(directory) / "input"
            source.write_bytes(data)
            return subprocess.check_output([*command, "--", str(source)], input=payload)

    def verify(self, signature, source):
        result = subprocess.run(
            ["gpg", "--batch", "--status-fd", "1", "--verify", str(signature), str(source)],
            capture_output=True, text=True, check=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("[GNUPG:] VALIDSIG "):
                fields = line.split()
                if self.fingerprint in (fields[2], fields[-1]):
                    return
        raise RuntimeError("Signature does not match the configured signing key")


def signer_from_env():
    key = os.environ.get("GPG_KEY_ID")
    return GpgSigner(key, os.environ.get("GPG_PASSPHRASE") or None) if key else None
