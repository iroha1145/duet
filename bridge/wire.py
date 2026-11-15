"""Small bounded protobuf/Connect codec for the inspected ConversationService subset."""
import json
import struct
import zlib

MAX_MESSAGE = 8 * 1024 * 1024


def varint(value):
    if not 0 <= value < 1 << 64:
        raise ValueError("unsigned 64-bit integer required")
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def integer(tag, value):
    return varint(tag << 3) + varint(int(value))


def blob(tag, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return varint((tag << 3) | 2) + varint(len(value)) + value


def parse(data):
    if len(data) > MAX_MESSAGE:
        raise ValueError("message too large")
    pos = 0

    def read_varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError("truncated varint")
            byte = data[pos]
            pos += 1
            if shift == 63 and byte > 1:
                raise ValueError("varint overflow")
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError("varint overflow")

    fields = {}
    while pos < len(data):
        key = read_varint()
        tag, kind = key >> 3, key & 7
        if not 0 < tag < 1 << 29:
            raise ValueError("invalid field number")
        if kind == 0:
            value = read_varint()
        elif kind in (1, 2, 5):
            length = read_varint() if kind == 2 else (8 if kind == 1 else 4)
            if pos + length > len(data):
                raise ValueError("truncated field")
            value = data[pos:pos + length]
            pos += length
        else:
            raise ValueError("unsupported wire type")
        fields.setdefault(tag, []).append(value)
    return fields


def raw(fields, tag, default=b""):
    value = fields.get(tag, [default])[-1]
    if not isinstance(value, bytes):
        raise ValueError("length-delimited field required")
    return value


def text(fields, tag, default=""):
    return raw(fields, tag, default.encode()).decode("utf-8")


def gunzip(data):
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    result = decoder.decompress(data, MAX_MESSAGE + 1)
    if len(result) > MAX_MESSAGE or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid or excessive gzip payload")
    return result


def frame(payload, flags=0):
    if len(payload) > MAX_MESSAGE or flags not in (0, 1, 2):
        raise ValueError("invalid Connect frame")
    return struct.pack(">BI", flags, len(payload)) + payload


def trailers(code=None, message=None):
    value = {}
    if code:
        value["error"] = {"code": code, "message": message or code}
    return frame(json.dumps(value, separators=(",", ":")).encode(), 2)


class FrameDecoder:
    """Incremental decoder; chunk boundaries have no protocol significance."""
    def __init__(self, encoding="identity"):
        self.encoding = encoding
        self.buffer = bytearray()
        self.ended = False

    def feed(self, chunk):
        self.buffer.extend(chunk)
        result = []
        while len(self.buffer) >= 5:
            flags, length = struct.unpack(">BI", self.buffer[:5])
            if self.ended or flags not in (0, 1, 2) or length > MAX_MESSAGE:
                raise ValueError("invalid Connect envelope")
            if len(self.buffer) < 5 + length:
                break
            payload = bytes(self.buffer[5:5 + length])
            del self.buffer[:5 + length]
            if flags == 1:
                if self.encoding != "gzip":
                    raise ValueError("unsupported Connect compression")
                payload = gunzip(payload)
            self.ended = flags == 2
            result.append((flags, payload))
        return result

    def finish(self):
        if self.buffer:
            raise ValueError("truncated Connect envelope")
