"""Receipt photos: real-type check by magic bytes and removal of every metadata block, in memory.

No image library: the file is only parsed at container level (JPEG segments, PNG chunks, WebP RIFF chunks) and rebuilt
from the parts that are needed to display it. Pixels are never decoded or re-encoded, so nothing hostile reaches a
native decoder and the text of the receipt keeps its full quality. What is dropped: EXIF (including GPS and camera
serial), XMP, IPTC/Photoshop, ICC profiles, comments, thumbnails and every PNG text/time/ancillary chunk. The only
thing that survives is the JPEG orientation, rewritten as a minimal EXIF segment with that single tag, so a portrait
photo is still read upright by the vision model.
"""
import re
import zlib

MAX_IMAGE_BYTES = 6 * 1024 * 1024
JPEG, PNG, WEBP = "image/jpeg", "image/png", "image/webp"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = frozenset({b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"})
_WEBP_KEEP = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF"})
_WEBP_PICTURE = frozenset({b"VP8 ", b"VP8L", b"ANMF"})
_VP8X_METADATA_FLAGS = 0x20 | 0x08 | 0x04  # ICC profile, EXIF, XMP
_JPEG_KEEP = frozenset({0xDB, 0xC4, 0xCC, 0xDD, 0xDA, 0xEE})  # DQT, DHT, DAC, DRI, SOS, Adobe APP14 (colour transform)
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_JPEG_SCAN_END = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")


class ImageRejectedError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _corrupt() -> ImageRejectedError:
    return ImageRejectedError("image_corrupt", "La imagen está dañada o no se puede leer.")


def sniff(data: bytes) -> str | None:
    """The real type from the first bytes (the client's Content-Type and the file name are ignored); None if unsupported."""
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    if data[:8] == _PNG_SIGNATURE:
        return PNG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    return None


def sanitize(data: bytes) -> tuple[bytes, str]:
    """``(clean_bytes, mime)``; raises ``ImageRejectedError`` for an unsupported or structurally broken file."""
    mime = sniff(data)
    if mime is None:
        raise ImageRejectedError("unsupported_image_type", "Solo se aceptan imágenes JPEG, PNG o WebP.")
    return {JPEG: _jpeg, PNG: _png, WEBP: _webp}[mime](data), mime


# --- JPEG -----------------------------------------------------------------------------------------------------------

def _orientation(tiff: bytes) -> int:
    """The EXIF orientation (1-8) of a TIFF block; 1 when absent or malformed. Bounds-checked: the input is hostile."""
    try:
        order = {b"II": "little", b"MM": "big"}[tiff[:2]]
        if int.from_bytes(tiff[2:4], order) != 42:
            return 1
        offset = int.from_bytes(tiff[4:8], order)
        count = int.from_bytes(tiff[offset:offset + 2], order)
        for index in range(min(count, 64)):
            entry = tiff[offset + 2 + 12 * index: offset + 14 + 12 * index]
            if len(entry) == 12 and int.from_bytes(entry[:2], order) == 0x0112:
                value = int.from_bytes(entry[8:10], order)
                return value if 1 <= value <= 8 else 1
    except (KeyError, ValueError):
        pass
    return 1


def _orientation_segment(value: int) -> bytes:
    """APP1 with a TIFF holding only the orientation tag."""
    tiff = b"MM\x00\x2a\x00\x00\x00\x08" + b"\x00\x01" + b"\x01\x12\x00\x03\x00\x00\x00\x01" + value.to_bytes(2, "big") + b"\x00\x00" + b"\x00\x00\x00\x00"
    body = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + (len(body) + 2).to_bytes(2, "big") + body


def _jpeg(data: bytes) -> bytes:
    size, pos = len(data), 2
    kept: list[bytes] = []
    orientation, frame, closed = 1, False, False
    while pos < size:
        if data[pos] != 0xFF:
            raise _corrupt()
        while pos < size and data[pos] == 0xFF:
            pos += 1  # fill bytes
        if pos >= size:
            raise _corrupt()
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            closed = True
            break
        if marker == 0xD8:
            raise _corrupt()
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # standalone markers carry no length
        if pos + 2 > size:
            raise _corrupt()
        length = int.from_bytes(data[pos:pos + 2], "big")
        end = pos + length
        if length < 2 or end > size:
            raise _corrupt()
        if marker == 0xE1 and data[pos + 2:pos + 8] == b"Exif\x00\x00":
            orientation = _orientation(data[pos + 8:end])
        if marker in _JPEG_SOF:
            frame = True
        if marker in _JPEG_KEEP or marker in _JPEG_SOF:
            kept.append(b"\xff" + bytes([marker]) + data[pos:end])
        pos = end
        if marker == 0xDA:
            found = _JPEG_SCAN_END.search(data, pos)
            stop = found.start() if found else size
            kept.append(data[pos:stop])
            pos = stop
            if found is None:
                break
    if not frame or not closed:
        raise _corrupt()
    head = _orientation_segment(orientation) if orientation != 1 else b""
    return b"\xff\xd8" + head + b"".join(kept) + b"\xff\xd9"


# --- PNG ------------------------------------------------------------------------------------------------------------

def _png(data: bytes) -> bytes:
    size, pos = len(data), len(_PNG_SIGNATURE)
    out, first, pixels, closed = [_PNG_SIGNATURE], True, False, False
    while pos + 12 <= size:
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > size or (first and kind != b"IHDR"):
            raise _corrupt()
        first = False
        if kind in _PNG_KEEP:
            if int.from_bytes(data[end - 4:end], "big") != zlib.crc32(data[pos + 4:end - 4]):
                raise _corrupt()
            out.append(data[pos:end])
            pixels = pixels or kind == b"IDAT"
        pos = end
        if kind == b"IEND":
            closed = True
            break
    if first or not pixels or not closed:
        raise _corrupt()
    return b"".join(out)


# --- WebP -----------------------------------------------------------------------------------------------------------

def _webp(data: bytes) -> bytes:
    declared = int.from_bytes(data[4:8], "little") + 8
    size = min(declared, len(data))
    pos, chunks, picture = 12, [], False
    if declared < 20:
        raise _corrupt()
    while pos + 8 <= size:
        kind = data[pos:pos + 4]
        length = int.from_bytes(data[pos + 4:pos + 8], "little")
        end = pos + 8 + length
        if end > size:
            raise _corrupt()
        if kind in _WEBP_KEEP:
            payload = bytearray(data[pos + 8:end])
            if kind == b"VP8X":
                if not payload:
                    raise _corrupt()
                payload[0] &= ~_VP8X_METADATA_FLAGS & 0xFF
            chunk = kind + length.to_bytes(4, "little") + bytes(payload) + (b"\x00" if length % 2 else b"")
            chunks.append(chunk)
            picture = picture or kind in _WEBP_PICTURE
        pos = end + (length % 2)
    if not picture:
        raise _corrupt()
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body
