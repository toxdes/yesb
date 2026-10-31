"""Bounded JSON Lines protocol shared by the host and container runner."""

import base64
import json

VERSION = 1
MAX_DATA = 16 * 1024 * 1024
MAX_LINE = 24 * 1024 * 1024


def read_message(stream):
    line = stream.readline(MAX_LINE + 1)
    if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
        raise RuntimeError("Helper protocol: missing, truncated, or oversized message")
    message = json.loads(line)
    if not isinstance(message, dict) or type(message.get("version")) is not int or message["version"] != VERSION:
        raise RuntimeError("Helper protocol: unsupported message version")
    return message


def write_message(stream, kind, **fields):
    message = json.dumps({"version": VERSION, "kind": kind, **fields}).encode() + b"\n"
    if len(message) > MAX_LINE:
        raise RuntimeError("Helper protocol: oversized message")
    stream.write(message)
    stream.flush()


def encode_data(data):
    if len(data) > MAX_DATA:
        raise RuntimeError("Helper protocol: signing input exceeds 16 MiB")
    return base64.b64encode(data).decode("ascii")


def decode_data(value):
    if not isinstance(value, str) or len(value) > MAX_LINE:
        raise RuntimeError("Helper protocol: invalid signing data")
    data = base64.b64decode(value, validate=True)
    if len(data) > MAX_DATA:
        raise RuntimeError("Helper protocol: signing input exceeds 16 MiB")
    return data
