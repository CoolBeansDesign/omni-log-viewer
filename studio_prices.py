"""Read the ink prices set in xTool Studio on this computer.

xTool Studio keeps them in its Chromium "Local Storage" (a LevelDB database)
under the key DT002_PRICE_PARAMS_V<n>, e.g.
    {"currency": "USD($)", "colorInkPrice": 19.99, "whiteInkPrice": 19.99, ...}
The values are prices per cartridge. This module only reads the database
files; it never writes to them. Pure Python: a minimal LevelDB table/log
reader and snappy decoder, so no extra packages are needed.
"""

import json
import os
import re
import struct

STUDIO_DIRS = [
    os.path.expanduser('~/Library/Application Support/xTool Studio/Local Storage/leveldb'),      # macOS
    os.path.join(os.environ.get('APPDATA', ''), 'xTool Studio', 'Local Storage', 'leveldb'),  # Windows
]
KEY_RX = re.compile(rb'DT002_PRICE_PARAMS_V(\d+)$')


def _snappy(buf):
    pos, shift = 0, 0
    while buf[pos] & 0x80:      # skip the uncompressed-length varint
        pos += 1
    pos += 1
    out = bytearray()
    while pos < len(buf):
        tag = buf[pos]; pos += 1
        kind = tag & 3
        if kind == 0:                                   # literal
            ln = tag >> 2
            if ln >= 60:
                nb = ln - 59
                ln = int.from_bytes(buf[pos:pos + nb], 'little'); pos += nb
            ln += 1
            out += buf[pos:pos + ln]; pos += ln
            continue
        if kind == 1:
            ln = ((tag >> 2) & 7) + 4
            off = ((tag >> 5) << 8) | buf[pos]; pos += 1
        elif kind == 2:
            ln = (tag >> 2) + 1
            off = int.from_bytes(buf[pos:pos + 2], 'little'); pos += 2
        else:
            ln = (tag >> 2) + 1
            off = int.from_bytes(buf[pos:pos + 4], 'little'); pos += 4
        for _ in range(ln):                             # copies may overlap
            out.append(out[-off])
    return bytes(out)


def _varint(b, p):
    r = s = 0
    while True:
        x = b[p]; p += 1
        r |= (x & 0x7f) << s
        if x < 0x80:
            return r, p
        s += 7


def _block(data, off, size):
    raw = data[off:off + size]
    return _snappy(raw) if data[off + size] == 1 else raw


def _block_entries(blk):
    restarts = struct.unpack('<I', blk[-4:])[0]
    end = len(blk) - 4 - 4 * restarts
    p, key = 0, b''
    while p < end:
        shared, p = _varint(blk, p)
        fresh, p = _varint(blk, p)
        vlen, p = _varint(blk, p)
        key = key[:shared] + blk[p:p + fresh]; p += fresh
        yield key, blk[p:p + vlen]; p += vlen


def _table(path):
    data = open(path, 'rb').read()
    footer = data[-48:]
    p = 0
    _, p = _varint(footer, p); _, p = _varint(footer, p)
    ioff, p = _varint(footer, p); isz, p = _varint(footer, p)
    for _, handle in _block_entries(_block(data, ioff, isz)):
        o, q = _varint(handle, 0); s, q = _varint(handle, q)
        for k, v in _block_entries(_block(data, o, s)):
            yield k[:-8], v                             # drop sequence number


def _log(path):
    data = open(path, 'rb').read()
    recs, buf, p = [], b'', 0
    while p + 7 <= len(data):
        ln = struct.unpack('<H', data[p + 4:p + 6])[0]; typ = data[p + 6]
        frag = data[p + 7:p + 7 + ln]; p += 7 + ln
        if typ == 1:
            recs.append(frag)
        elif typ == 2:
            buf = frag
        elif typ == 3:
            buf += frag
        elif typ == 4:
            recs.append(buf + frag); buf = b''
        if 32768 - (p % 32768) < 7:
            p = (p // 32768 + 1) * 32768
    for r in recs:
        q = 12
        while q < len(r):
            t = r[q]; q += 1
            kl, q = _varint(r, q); k = r[q:q + kl]; q += kl
            if t == 1:
                vl, q = _varint(r, q); v = r[q:q + vl]; q += vl
                yield k, v


def _decode(v):
    # Chromium stores the string with a 1-byte encoding prefix (0 = UTF-16LE, 1 = Latin-1)
    if v[:1] == b'\x00':
        return v[1:].decode('utf-16-le', 'replace')
    return v[1:].decode('latin-1', 'replace')


def read_prices():
    """Return {'currency', 'perCartridge': {name: price}, 'source'} or None."""
    for d in STUDIO_DIRS:
        if not os.path.isdir(d):
            continue
        best = None                     # (version, file order, value)
        files = sorted(os.listdir(d))   # newer files sort later and win
        for order, fn in enumerate(files):
            path = os.path.join(d, fn)
            reader = _table if fn.endswith('.ldb') else _log if fn.endswith('.log') else None
            if not reader:
                continue
            try:
                for k, v in reader(path):
                    m = KEY_RX.search(k)
                    if m and v:
                        cand = (int(m.group(1)), order, v)
                        if best is None or cand[:2] >= best[:2]:
                            best = cand
            except Exception:           # unreadable or half-written file: skip it
                continue
        if best:
            try:
                raw = json.loads(_decode(best[2]))
            except ValueError:
                continue
            prices = {k: float(v) for k, v in raw.items() if k.endswith('InkPrice') and isinstance(v, (int, float))}
            if any(prices.values()):
                return {'currency': raw.get('currency') or '', 'perCartridge': prices,
                        'source': 'xTool Studio on this computer'}
    return None


if __name__ == '__main__':
    print(json.dumps(read_prices(), indent=1))
