#!/usr/bin/env python3
"""Implement RPM's signing command through the container runner's local socket."""

import os
import socket
import sys
from pathlib import Path

from helper_protocol import MAX_DATA, decode_data, encode_data, read_message, write_message


def main():
    if len(sys.argv) != 3:
        raise RuntimeError("RPM signer expects an input and signature-output path")
    if sys.argv[1] == "-":
        data = sys.stdin.buffer.read(MAX_DATA + 1)
    else:
        with Path(sys.argv[1]).open("rb") as source:
            data = source.read(MAX_DATA + 1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(600)
        connection.connect(os.environ["YESB_SIGN_SOCKET"])
        with connection.makefile("rwb") as stream:
            write_message(stream, "sign", data=encode_data(data))
            response = read_message(stream)
    if response.get("kind") != "signature":
        raise RuntimeError("Host rejected RPM signature request")
    signature = decode_data(response.get("data"))
    if sys.argv[2] == "-":
        sys.stdout.buffer.write(signature)
        sys.stdout.buffer.flush()
    else:
        Path(sys.argv[2]).write_bytes(signature)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"RPM signing failed: {error}", file=sys.stderr)
        sys.exit(1)
