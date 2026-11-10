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
