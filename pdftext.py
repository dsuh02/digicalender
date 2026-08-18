"""
Just enough of a PDF to read the text out of one, with nothing installed.

This exists because the project has no third-party packages and no build step,
and a statement PDF is the only way some institutions will hand over their data
at all. A full PDF implementation is enormous; a *text extractor* is not, because
almost all of the format — fonts, colour spaces, images, transparency, shading —
has no bearing on where a string sits on the page.

What is actually needed is small:

  * find the pages, in page order,
  * run each page's content stream far enough to track the text matrix, and
  * report every string with the device coordinates it was drawn at.

Coordinates are the whole point. A statement is a TABLE, and a table read as a
flat run of words is ambiguous in exactly the places that matter — which column
a number belongs to. Everything here preserves x and y so the caller can rebuild
the grid instead of guessing from reading order.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
* **Encryption.** Even empty-password PDFs need RC4/AES to read. `extract()`
  raises rather than returning the encrypted bytes as if they were text.
* **Font encodings.** Bytes are decoded as cp1252, which is right for the
  WinAnsi text nearly every business PDF emits. A document with a custom or
  symbolic encoding will produce mojibake — it will not crash, and it will not
  be silently plausible either, because the caller matches against known labels.
* **Scanned documents.** A page of images has no text to find. `extract()`
  returns an empty page, which callers must treat as "unreadable", not "empty".

That last one is the important limitation to keep in mind: this reads PDFs that
*contain* text, not PDFs that are pictures of text.
"""

from __future__ import annotations

import re
import zlib

# A literal string can nest parentheses, so it cannot be matched with a regex;
# everything here is scanned rather than pattern-matched for that reason.
_DELIM = b"()<>[]{}/%"
_WS = b"\x00\t\n\x0c\r "

_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t",
            ord("b"): b"\b", ord("f"): b"\x0c", ord("("): b"(",
            ord(")"): b")", ord("\\"): b"\\"}


class PdfError(Exception):
    """The file is not a PDF this module can read."""


# ------------------------------------------------------------------ objects

def _objects(raw: bytes) -> dict[int, bytes]:
    """Every `N 0 obj ... endobj` body, by object number.

    The cross-reference table is ignored on purpose. It is the part of a PDF
    most likely to be stale — incremental updates, linearisation and generators
    that miscount byte offsets all break it — while the object bodies themselves
    are almost always intact. Scanning finds them regardless, and a later
    definition wins, which is exactly the incremental-update rule.
    """
    out: dict[int, bytes] = {}
    for m in re.finditer(rb"(?:^|[\s>])(\d+)\s+(\d+)\s+obj\b", raw):
        num = int(m.group(1))
        end = raw.find(b"endobj", m.end())
        out[num] = raw[m.end():end if end != -1 else len(raw)]
    return out


_REF = re.compile(rb"(\d+)\s+\d+\s+R")


def _deref(val: bytes, objs: dict[int, bytes]) -> bytes:
    """Follow `N 0 R` to the object body; return `val` unchanged if it is direct."""
    m = _REF.fullmatch(val.strip())
    if not m:
        return val
    return objs.get(int(m.group(1)), b"")


def _stream_of(body: bytes, objs: dict[int, bytes]) -> bytes:
    """The decoded stream inside an object body, or b'' if it has none."""
    m = re.search(rb"stream\r?\n", body)
    if not m:
        return b""
    start = m.end()

    # Prefer the declared /Length: a Flate stream can legitimately contain the
    # bytes "endstream", and searching for them would truncate it mid-way.
    data = None
    lm = re.search(rb"/Length\s+([^/>\]]+)", body[:m.start()])
    if lm:
        raw_len = _deref(lm.group(1).strip(), objs).strip()
        if raw_len.isdigit():
            n = int(raw_len)
            tail = body[start + n:start + n + 20]
            if re.match(rb"\s*endstream", tail):
                data = body[start:start + n]
    if data is None:
        end = body.find(b"endstream", start)
        data = body[start:end if end != -1 else len(body)]

    filters = re.findall(rb"/(FlateDecode|LZWDecode|DCTDecode|JPXDecode|"
                         rb"CCITTFaxDecode|RunLengthDecode|ASCII85Decode)",
                         body[:m.start()])
    for f in filters:
        if f == b"FlateDecode":
            try:
                data = zlib.decompress(data)
            except zlib.error:
                # Some writers pad or truncate; salvage what decompressed.
                try:
                    data = zlib.decompressobj().decompress(data)
                except zlib.error:
                    return b""
        elif f in (b"DCTDecode", b"JPXDecode", b"CCITTFaxDecode"):
            return b""                  # an image: no text to find
        else:
            return b""                  # a filter we do not implement
    return data


def _page_contents(raw: bytes, objs: dict[int, bytes]) -> list[bytes]:
    """Content streams, one entry per page, in page order.

    Walks /Root -> /Pages -> /Kids so pages come out in the order a reader sees
    them. File order is NOT page order in general, and a statement whose loan
    table spills onto a second page is exactly the case where getting that
    backwards would silently reorder columns.
    """
    root = re.search(rb"/Root\s+(\d+)\s+\d+\s+R", raw)
    pages_num = None
    if root:
        cat = objs.get(int(root.group(1)), b"")
        m = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", cat)
        if m:
            pages_num = int(m.group(1))
    if pages_num is None:
        for num, body in objs.items():
            if re.search(rb"/Type\s*/Pages\b", body):
                pages_num = num
                break
    if pages_num is None:
        raise PdfError("no page tree found")

    order: list[int] = []
    seen: set[int] = set()

    def walk(num: int) -> None:
        if num in seen or len(order) > 5000:
            return                      # a malformed /Kids can be cyclic
        seen.add(num)
        body = objs.get(num, b"")
        km = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
        if km:
            for kid in _REF.finditer(km.group(1)):
                walk(int(kid.group(1)))
        elif re.search(rb"/Type\s*/Page\b", body):
            order.append(num)

    walk(pages_num)

    out: list[bytes] = []
    for num in order:
        body = objs.get(num, b"")
        cm = re.search(rb"/Contents\s*(\[.*?\]|\d+\s+\d+\s+R)", body, re.S)
        if not cm:
            out.append(b"")
            continue
        # /Contents may be one stream or an array of them, and an array is a
        # single stream split at an arbitrary byte — possibly mid-operator — so
        # the parts are joined before being interpreted, never run separately.
        parts = [_stream_of(objs.get(int(r.group(1)), b""), objs)
                 for r in _REF.finditer(cm.group(1))]
        out.append(b"\n".join(p for p in parts if p))
    return out


# ------------------------------------------------------------------ scanning

def _unescape(b: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(b)
    while i < n:
        if b[i] != 0x5C:                # backslash
            out.append(b[i])
            i += 1
            continue
        i += 1
        if i >= n:
            break
        c = b[i]
        if c in _ESCAPES:
            out += _ESCAPES[c]
            i += 1
        elif 0x30 <= c <= 0x37:         # up to three octal digits
            digits = ""
            while i < n and len(digits) < 3 and 0x30 <= b[i] <= 0x37:
                digits += chr(b[i])
                i += 1
            out.append(int(digits, 8) & 0xFF)
        elif c in (0x0A, 0x0D):         # line continuation: emits nothing
            i += 1
            if c == 0x0D and i < n and b[i] == 0x0A:
                i += 1
        else:
            out.append(c)               # \q is just q
            i += 1
    return bytes(out)


def _tokens(data: bytes):
    """Content-stream tokens as ('num'|'str'|'name'|'op', value)."""
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
            continue
        if c == 0x25:                   # % comment
            j = data.find(b"\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == 0x28:                   # ( literal string
            depth, j, buf = 1, i + 1, bytearray()
            while j < n:
                ch = data[j]
                if ch == 0x5C:
                    buf += data[j:j + 2]
                    j += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                buf.append(ch)
                j += 1
            yield "str", _unescape(bytes(buf))
            i = j
            continue
        if c == 0x3C:                   # < hex string, or << dict
            if data[i:i + 2] == b"<<":
                yield "op", "<<"
                i += 2
                continue
            j = data.find(b">", i)
            j = n if j < 0 else j
            hexs = re.sub(rb"[^0-9A-Fa-f]", b"", data[i + 1:j])
            if len(hexs) % 2:
                hexs += b"0"
            yield "str", bytes.fromhex(hexs.decode())
            i = j + 1
            continue
        if data[i:i + 2] == b">>":
            yield "op", ">>"
            i += 2
            continue
        if c in b"[]{}>":
            yield "op", chr(c)
            i += 1
            continue
        if c == 0x2F:                   # /Name
            j = i + 1
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            yield "name", data[i + 1:j].decode("latin-1")
            i = j
            continue
        j = i
        while j < n and data[j] not in _WS and data[j] not in _DELIM:
            j += 1
        tok = data[i:j] or data[i:i + 1]
        i = j if j > i else i + 1
        try:
            yield "num", float(tok)
        except ValueError:
            yield "op", tok.decode("latin-1", "replace")


def _mul(m: tuple, n: tuple) -> tuple:
    """Multiply two PDF matrices [a b c d e f]."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _page_items(content: bytes) -> list[dict]:
    """Every string drawn on one page, as {x, y, text}.

    The text matrix is tracked through Tm/Td/TD/T*, and multiplied by the
    current transformation matrix, because a page that translates its whole
    content with `cm` (these statements shift everything by 54pt) would
    otherwise report coordinates that no two pages agree on.
    """
    items: list[dict] = []
    stack: list[tuple] = []
    ctm = _IDENTITY
    tm = lm = _IDENTITY
    leading = 0.0
    operands: list = []

    def show(raw: bytes) -> None:
        nonlocal tm
        s = raw.decode("cp1252", "replace")
        if s.strip():
            m = _mul(tm, ctm)
            items.append({"x": round(m[4], 2), "y": round(m[5], 2), "text": s})

    for kind, val in _tokens(content):
        if kind in ("num", "str", "name"):
            operands.append(val)
            if len(operands) > 64:
                del operands[:-16]      # a malformed stream must not grow forever
            continue
        op = val
        try:
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                ctm = stack.pop() if stack else _IDENTITY
            elif op == "cm" and len(operands) >= 6:
                ctm = _mul(tuple(float(v) for v in operands[-6:]), ctm)
            elif op == "BT":
                tm = lm = _IDENTITY
            elif op == "Tm" and len(operands) >= 6:
                tm = lm = tuple(float(v) for v in operands[-6:])
            elif op == "TL" and operands:
                leading = float(operands[-1])
            elif op in ("Td", "TD") and len(operands) >= 2:
                tx, ty = float(operands[-2]), float(operands[-1])
                if op == "TD":
                    leading = -ty
                tm = lm = _mul((1, 0, 0, 1, tx, ty), lm)
            elif op == "T*":
                tm = lm = _mul((1, 0, 0, 1, 0, -leading), lm)
            elif op == "Tj" and operands:
                show(operands[-1])
            elif op == "'" and operands:
                tm = lm = _mul((1, 0, 0, 1, 0, -leading), lm)
                show(operands[-1])
            elif op == '"' and operands:
                tm = lm = _mul((1, 0, 0, 1, 0, -leading), lm)
                show(operands[-1])
            elif op == "TJ":
                # An array of strings and kerning numbers. The numbers move the
                # pen by a font-size-scaled amount; that is ignored here because
                # kerning never spans a column, and tracking it would mean
                # parsing font widths for no gain in table reconstruction.
                for v in operands:
                    if isinstance(v, bytes):
                        show(v)
        except (TypeError, ValueError, IndexError):
            pass                        # one bad operator must not lose the page
        operands = []
    return items


def extract(data: bytes) -> list[list[dict]]:
    """Pages of positioned text: `[[{x, y, text}, ...], ...]`, in page order.

    y is PDF-native, so it grows UPWARD from the bottom of the page. Callers
    that want reading order sort by `-y`.
    """
    if not data.startswith(b"%PDF"):
        raise PdfError("not a PDF file")
    if re.search(rb"/Encrypt\s+\d+\s+\d+\s+R", data):
        raise PdfError("this PDF is encrypted; save an unprotected copy first")
    objs = _objects(data)
    if not objs:
        raise PdfError("no PDF objects found; the file may be truncated")
    return [_page_items(c) for c in _page_contents(data, objs)]


# --------------------------------------------------------------------- rows

def rows(items: list[dict], tol: float = 3.0) -> list[dict]:
    """Group items into visual rows: `[{y, items: [...]}, ...]`, top-down.

    Rows are grown by single linkage — an item joins the row below it while it
    is within `tol` of the previous item — because a table label frequently sits
    a point or two off its own values, and a two-line label straddles them. The
    default of 3pt is under a quarter of a normal line pitch, so distinct rows
    stay distinct while a wrapped label stays with the numbers it describes.

    Within a row, items are ordered top-then-left, which keeps a wrapped label
    reading in the order it was written.
    """
    if not items:
        return []
    ordered = sorted(items, key=lambda i: (-i["y"], i["x"]))
    out: list[dict] = []
    group = [ordered[0]]
    for it in ordered[1:]:
        if abs(it["y"] - group[-1]["y"]) <= tol:
            group.append(it)
        else:
            out.append(group)
            group = [it]
    out.append(group)
    return [{"y": max(g, key=lambda i: i["y"])["y"],
             "items": sorted(g, key=lambda i: (-i["y"], i["x"]))} for g in out]


def text_of(row: dict, sep: str = " ") -> str:
    return re.sub(r"\s+", " ", sep.join(i["text"] for i in row["items"])).strip()


def page_text(items: list[dict]) -> str:
    """The page as plain text in reading order — for prose, never for tables."""
    return "\n".join(text_of(r) for r in rows(items))
