#!/usr/bin/env python3
"""Prepare package repositories inside Debian/Fedora without host credentials."""

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from helper_protocol import decode_data, encode_data, read_message, write_message


def input_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise RuntimeError("Invalid helper input path")
    return Path("/input") / name


class RpmSigner:
    def __init__(self, public_key, reader, writer):
        self.public_key = public_key
        self.reader = reader
        self.writer = writer

    def sign_packages(self, packages, fingerprint):
        public_key = Path("/tmp/release-public.asc")
        public_key.write_text(self.public_key)
        database = "/tmp/rpm-keyring"
        subprocess.run(["rpmkeys", "--dbpath", database, "--import", str(public_key)], check=True, stdout=sys.stderr)
        # the outer stdin/stdout protocol stays owned by this runner
        with tempfile.TemporaryDirectory(prefix="rpm-sign-", dir="/tmp") as directory:
            endpoint = str(Path(directory) / "sign.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(endpoint)
                listener.listen(1)
                listener.settimeout(0.2)
                command = [
                    "rpmsign", "--addsign", "--define", f"_openpgp_sign_id {fingerprint}",
                    "--define", f"_gpg_name {fingerprint}",
                    "--define", "__gpg_sign_cmd() /usr/bin/python3 /opt/yesb/rpm_sign_adapter.py %{shescape:%{1}} %{shescape:%{2}}",
                ]
                environment = {**os.environ, "YESB_SIGN_SOCKET": endpoint}
                for package in sorted(packages):
                    query = ["rpm", "--nosignature", "-qp", "--queryformat", "%{NAME}:%{VERSION}:%{RELEASE}:%{ARCH}:%{PAYLOADSHA256}:%{PAYLOADSHA512}:%{PAYLOADSHA3_256}", str(package)]
                    original = subprocess.check_output(query)
                    if all(value in {b"", b"(none)"} for value in original.rsplit(b":", 3)[-3:]):
                        raise RuntimeError("RPM package has no supported payload digest")
                    process = subprocess.Popen([*command, str(package)], env=environment, stdin=subprocess.DEVNULL, stdout=sys.stderr)
                    try:
                        while process.poll() is None:
                            try:
                                connection, _ = listener.accept()
                            except socket.timeout:
                                continue
                            with connection, connection.makefile("rwb") as stream:
                                connection.settimeout(600)
                                request = read_message(stream)
                                if request.get("kind") != "sign":
                                    raise RuntimeError("Invalid RPM adapter request")
                                data = decode_data(request.get("data"))
                                write_message(self.writer, "sign", data=encode_data(data))
                                response = read_message(self.reader)
                                if response.get("kind") != "signature":
                                    raise RuntimeError("Host rejected RPM signing")
                                write_message(stream, "signature", data=encode_data(decode_data(response.get("data"))))
                        if process.wait() != 0:
                            raise RuntimeError("rpmsign failed")
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
                    result = subprocess.run([
                        "rpmkeys", "--dbpath", database, "--checksig", str(package),
                    ], capture_output=True, text=True, check=True)
                    if "signatures OK" not in result.stdout:
                        raise RuntimeError("RPM package has no verified signature")
                    if subprocess.check_output(query) != original:
                        raise RuntimeError("RPM signing changed package identity or payload digest")


def main():
    request = read_message(sys.stdin.buffer)
    if request.get("kind") != "prepare":
        raise RuntimeError("Expected a repository preparation request")
    operation = request.get("operation")
    packages = [input_path(name) for name in request["packages"]]
    options = request["options"]
    reader, writer = sys.stdin.buffer, sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):
        if operation == "apt":
            from release_apt import build_repo
            build_repo(Path("/output"), packages, **options)
        elif operation == "rpm":
            from release_rpm import build_repo
            os.environ.pop("GPG_KEY_ID", None)
            if request.get("key"):
                os.environ["GPG_KEY_ID"] = request["key"]
            existing = options.pop("existing_metadata", None)
            if existing:
                options["existing_metadata"] = input_path(existing)
            signer = RpmSigner(request.get("public_key"), reader, writer)
            build_repo(Path("/output"), packages, signer=signer.sign_packages, **options)
        else:
            raise RuntimeError("Unsupported repository helper operation")
    write_message(sys.stdout.buffer, "complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Repository helper failed: {error}", file=sys.stderr)
        sys.exit(1)
