"""FastLZ (levels 1 and 2) -- the codec behind FM8 preset payloads.

Verified against the plugin: for a real .nfm8 the byte range
[file_offset_of_header + 8 : +8 + compressedSize] is a FastLZ level-1 stream
whose decoded length equals the declared uncompressed size exactly.

The format is Ariya Hidayat's FastLZ, reimplemented here from the published
format so that no Native Instruments code is needed at runtime.

Stream selection: level = src[0] >> 5, 0 -> level 1, 1 -> level 2.
Token format:
    ctrl = src[ip++]
    if ctrl >= 32:
        length = (ctrl >> 5) - 1
        offset = (ctrl & 31) << 8
        if length == 6: length += src[ip++]
        ref = len(out) - offset - src[ip++] - 1     # distance is biased by 1
        length += 3
    else:
        copy (ctrl + 1) literal bytes
"""


def decompress(src, level=None, maxout=1 << 24):
    if level is None:
        if not src:
            return b''
        level = 1 if (src[0] >> 5) == 0 else 2
    ip = 0
    n = len(src)
    out = bytearray()
    if n == 0:
        return bytes(out)
    ctrl = src[ip]
    ip += 1
    while True:
        if ctrl >= 32:
            length = (ctrl >> 5) - 1
            offset = (ctrl & 31) << 8
            if length == 6:
                if ip >= n:
                    break
                length += src[ip]
                ip += 1
            if ip >= n:
                break
            ref = len(out) - offset - src[ip] - 1
            ip += 1
            length += 3
            if ref < 0:
                return None
            try:
                for _ in range(length):
                    out.append(out[ref])
                    ref += 1
            except IndexError:
                return None
        else:
            length = ctrl + 1
            out += src[ip:ip + length]
            ip += length
        if ip >= n or len(out) >= maxout:
            break
        ctrl = src[ip]
        ip += 1
    return bytes(out)


def decompress_block(buf, off=0):
    """Decode one `[u32 plainSize][u32 compSize][stream]` record at `off`."""
    import struct
    plain, comp = struct.unpack_from('<II', buf, off)
    data = decompress(buf[off + 8:off + 8 + comp], 1, plain + 64)
    if data is None or len(data) != plain:
        return None
    return data


def find_blocks(buf, min_size=64):
    """Locate every plausible embedded FastLZ block in `buf`."""
    import struct
    out = []
    for i in range(len(buf) - 9):
        plain, comp = struct.unpack_from('<II', buf, i)
        if not (min_size <= comp <= 0x20000 and min_size <= plain <= 0x200000):
            continue
        if (buf[i + 8] >> 5) != 0:
            continue
        dec = decompress(buf[i + 8:i + 8 + comp], 1, plain + 64)
        if dec is not None and len(dec) == plain:
            out.append(i)
    return out


if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else 'tmp/base.bin'
    data = open(path, 'rb').read()
    blocks = find_blocks(data)
    print('%s: %d bytes, FastLZ blocks at %s' % (path, len(data), [hex(b) for b in blocks]))
    for b in blocks:
        dec = decompress_block(data, b)
        print('  block %#x -> %d bytes, head %s' % (b, len(dec), dec[:16].hex(' ')))


# --------------------------------------------------------------------------
# Level-1 compressor (FastLZ reference algorithm)
# --------------------------------------------------------------------------
HASH_LOG = 13
HASH_SIZE = 1 << HASH_LOG
MAX_COPY = 32
MAX_DISTANCE = 8191
MAX_LEN = 263   # longest match one token can encode: 8 + 255


def _hash(v):
    return ((v * 2654435769) & 0xffffffff) >> (32 - HASH_LOG)


def _read32(buf, i):
    return buf[i] | (buf[i + 1] << 8) | (buf[i + 2] << 16) | (buf[i + 3] << 24)


def _literals(out, src, start, runs):
    i = start
    while runs >= MAX_COPY:
        out.append(MAX_COPY - 1)
        out += src[i:i + MAX_COPY]
        i += MAX_COPY
        runs -= MAX_COPY
    if runs > 0:
        out.append(runs - 1)
        out += src[i:i + runs]
        i += runs
    return i


def _match(out, length, distance):
    """Encode a match.  Decoder: n = ctrl >> 5; if n == 7: n = 6 + extra;
    total = n + 2."""
    if length <= 8:
        out.append(((length - 2) << 5) | (distance >> 8))
        out.append(distance & 0xFF)
    else:
        out.append((7 << 5) | (distance >> 8))
        out.append(min(length - 9, 255))
        out.append(distance & 0xFF)


def compress(src, level=1):
    """FastLZ level-1 compressor.  Emits [run][literals] and [token][offset]."""
    n = len(src)
    if n < 4:
        return None
    ip_limit = n - 12
    ip_bound = n - 4
    htab = [0] * HASH_SIZE
    out = bytearray()
    ip = 0
    anchor = 0
    while ip < ip_limit:
        hval = _read32(src, ip)
        h = _hash(hval)
        ref = htab[h]
        htab[h] = ip
        distance = ip - ref - 1
        if ref < ip and distance < MAX_DISTANCE and _read32(src, ref) == hval:
            if anchor < ip:
                _literals(out, src, anchor, ip - anchor)
            start = ip
            ip += 3
            ref += 3
            while ip < ip_bound and (ip - start) < MAX_LEN and src[ip] == src[ref]:
                ip += 1
                ref += 1
            _match(out, ip - start, distance)
            anchor = ip
            if ip < ip_limit:
                htab[_hash(_read32(src, ip - 2))] = ip - 2
                htab[_hash(_read32(src, ip - 1))] = ip - 1
        else:
            ip += 1
    if anchor < n:
        _literals(out, src, anchor, n - anchor)
    return bytes(out)


