"""Read only RPM envelopes to bind helper signing to host-selected packages."""

import base64
import hashlib
import struct
import subprocess
import tempfile
from pathlib import Path

from helper_protocol import MAX_DATA

HEADER_MAGIC = b"\x8e\xad\xe8\x01\0\0\0\0"


def read_header(stream, region):
    intro = stream.read(16)
    if len(intro) != 16 or intro[:8] != HEADER_MAGIC:
        raise RuntimeError("Invalid RPM header")
    count, size = struct.unpack(">II", intro[8:])
    if not count or count * 16 + size + 16 > MAX_DATA:
        raise RuntimeError("RPM header exceeds signing limit")
    indexes, data = stream.read(count * 16), stream.read(size)
    if len(indexes) != count * 16 or len(data) != size:
        raise RuntimeError("Truncated RPM header")
    entries = [struct.unpack(">IIiI", indexes[i:i + 16]) for i in range(0, len(indexes), 16)]
    tag, kind, offset, length = entries[0]
    if (tag, kind, length) != (region, 7, 16) or offset < 0 or offset + 16 > size:
        raise RuntimeError("Unsupported RPM header region")
    if struct.unpack(">IIiI", data[offset:offset + 16]) != (region, 7, -count * 16, 16):
        raise RuntimeError("RPM header contains mutable entries")
    tags = [entry[0] for entry in entries]
    if tags != sorted(set(tags)):
        raise RuntimeError("RPM header contains duplicate or unordered tags")
    for _, _, offset, _ in entries:
        if offset < 0 or offset >= size:
            raise RuntimeError("Invalid RPM header offset")
    return intro + indexes + data, entries, data


class RpmEnvelope:
    def __init__(self, path):
        with path.open("rb") as stream:
            self.lead = stream.read(96)
            if len(self.lead) != 96 or self.lead[:4] != b"\xed\xab\xee\xdb" or self.lead[4] not in (3, 4):
                raise RuntimeError("Unsupported RPM package envelope")
            signature, entries, data = read_header(stream, 62)
            padding = stream.read((-len(signature)) % 8)
            if padding != bytes((-len(signature)) % 8):
                raise RuntimeError("Invalid RPM signature padding")
            self.header, _, _ = read_header(stream, 63)
            self.payload_digest = hashlib.file_digest(stream, "sha256").digest()
        self.signatures = []
        for tag, kind, offset, count in entries:
            if tag in (267, 268) and kind == 7 and 0 < count <= len(data) - offset:
                self.signatures.append(data[offset:offset + count])
            elif tag == 278 and kind == 8:
                if count > len(data) - offset:
                    raise RuntimeError("Invalid RPM signature count")
                for _ in range(count):
                    end = data.find(b"\0", offset)
                    if end < 0:
                        raise RuntimeError("Truncated RPM OpenPGP signature")
                    self.signatures.append(base64.b64decode(data[offset:end], validate=True))
                    offset = end + 1

    def verify_output(self, output, signer):
        result = RpmEnvelope(output)
        if (self.lead, self.header, self.payload_digest) != (result.lead, result.header, result.payload_digest):
            raise RuntimeError("Repository helper modified RPM package content")
        if signer is None:
            return
        with tempfile.TemporaryDirectory(prefix="yesb-rpm-verify-") as directory:
            source, signature = Path(directory) / "header", Path(directory) / "signature"
            source.write_bytes(self.header)
            last_error = None
            for packet in result.signatures:
                signature.write_bytes(packet)
                try:
                    signer.verify(signature, source)
                    return
                except (RuntimeError, subprocess.CalledProcessError) as error:
                    # an existing signature may belong to a different key
                    last_error = error
            raise RuntimeError("Returned RPM has no valid signature from the configured key") from last_error


class RpmSigningPolicy:
    def __init__(self, packages):
        self.packages = {path.name: RpmEnvelope(path) for path in packages}
        self.approved = {package.header for package in self.packages.values()}

    def authorize(self, data):
        if data not in self.approved:
            raise RuntimeError("Helper requested signing data outside the selected RPM headers")
