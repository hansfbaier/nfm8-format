"""FastLZ levels 1 and 2 -- the codec behind FM8 preset payloads.

Verified against the plugin: a real .nfm8 stores a
`[u32 uncompressedSize][u32 compressedSize][FastLZ stream]` record, and the
stream decodes to exactly the declared size.  The format is Ariya Hidayat's
FastLZ; both directions are reimplemented here from the published format and
from the compiled FM8 code so that no Native Instruments code is needed at
runtime.

Token format (decoder):
    n = ctrl >> 5                       # ctrl = src[ip]
    if ctrl >= 32:
        if n == 7: n = 6 + extra        # extra byte follows
        length = n + 2
        ref = len(out) - ((ctrl & 31) << 8) - src[ip] - 1
    else:
        copy n+1 literal bytes
"""
import struct

HASH_SIZE = 1 << 13
MAX_COPY = 32
MAX_DISTANCE = 8191
MAX_LEN = 264


# ---------------------------------------------------------------- decoding
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
            nbits = ctrl >> 5
            if nbits == 7:
                if ip >= n:
                    break
                nbits = 7 + src[ip]
                ip += 1
            length = nbits + 2
            if ip >= n:
                break
            offset = (ctrl & 31) << 8
            ref = len(out) - offset - src[ip] - 1
            ip += 1
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
    plain, comp = struct.unpack_from('<II', buf, off)
    data = decompress(buf[off + 8:off + 8 + comp], 1, plain + 64)
    if data is None or len(data) != plain:
        return None
    return data


def find_blocks(buf, min_size=64):
    out = []
    for i in range(len(buf) - 9):
        plain, comp = struct.unpack_from('<II', buf, i)
        if not (min_size <= comp < plain <= 0x200000):
            continue
        if (buf[i + 8] >> 5) != 0:
            continue
        dec = decompress(buf[i + 8:i + 8 + comp], 1, plain + 64)
        if dec is not None and len(dec) == plain:
            out.append(i)
    return out


# ---------------------------------------------------------------- encoding
def _hash(buf, i):
    """Three-byte hash, as compiled into FM8's fastlz1_compress.

    h = (((b2<<8)|b1) ^ ((b1<<8)|b0) ^ (((b1<<8)|b0) >> 3)) & 0x1fff
    """
    s0 = buf[i]
    s1 = buf[i + 1]
    s2 = buf[i + 2]
    b = (s2 << 8) | s1
    c = (s1 << 8) | s0
    return (b ^ c ^ (c >> 3)) & 0x1FFF


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
    # FM8 splits matches of 265+ bytes into 262-byte chunks (extra byte 0xfd)
    while length >= 265:
        out.append(0xE0 | (distance >> 8))
        out.append(0xFD)
        out.append(distance & 0xFF)
        length -= 262
    if length <= 8:
        out.append(((length - 2) << 5) | (distance >> 8))
        out.append(distance & 0xFF)
    else:
        out.append(0xE0 | (distance >> 8))
        out.append(length - 9)
        out.append(distance & 0xFF)


def compress(src):
    """FastLZ level-1 compressor matching NI::SOUND::fastlz1_compress."""
    n = len(src)
    if n <= 3:
        return None
    htab = [0] * HASH_SIZE
    out = bytearray()
    ip = 2
    anchor = 0
    limit = n - 12
    bound = n - 2
    while ip < limit:
        h = _hash(src, ip)
        ref = htab[h]
        htab[h] = ip
        distance = ip - ref - 1
        if (ref < ip and distance < MAX_DISTANCE
                and src[ref] == src[ip]
                and src[ref + 1] == src[ip + 1]
                and src[ref + 2] == src[ip + 2]):
            if anchor < ip:
                _literals(out, src, anchor, ip - anchor)
            start = ip
            ip += 3
            r = ref + 3
            while ip < bound and src[ip] == src[r]:
                ip += 1
                r += 1
            _match(out, ip - start, distance)
            anchor = ip
            if ip + 2 < n:
                for back in (2, 1):
                    j = ip - back
                    if j + 2 < n:
                        htab[_hash(src, j)] = j
        else:
            ip += 1
    if anchor < n:
        _literals(out, src, anchor, n - anchor)
    return bytes(out)


if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else 'tmp/base.bin'
    data = open(path, 'rb').read()
    blocks = find_blocks(data)
    print('%s: %d bytes, FastLZ blocks at %s' % (path, len(data), [hex(b) for b in blocks]))
    for b in blocks:
        dec = decompress_block(data, b)
        comp = data[b + 8:b + 8 + struct.unpack_from('<I', data, b + 4)[0]]
        re = compress(dec)
        print('  block %#x -> %d bytes, head %s' % (b, len(dec), dec[:16].hex(' ')))
        print('    recompressed %d bytes, identical to stored: %s' % (len(re), re == comp))
