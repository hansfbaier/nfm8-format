"""FastLZ decompressor (levels 1 and 2).

FastLZ is Ariya Hidayat's LZ77 variant; the FM8 preset container uses
NI::SOUND::fastlz_compress for the EncryptionItem payload.  Implemented from
the published format so the tool needs no NI code at runtime.
"""


def decompress(src, level=1, maxout=1 << 20):
    ip = 0
    n = len(src)
    out = bytearray()
    if n == 0:
        return bytes(out)
    ctrl = src[ip] & 31
    ip += 1
    while True:
        if ctrl >= 32:
            length = (ctrl >> 5) - 1
            ofs = (ctrl & 31) << 8
            if level == 1:
                if length == 6:
                    if ip >= n:
                        break
                    length += src[ip]; ip += 1
                if ip >= n:
                    break
                ref = len(out) - ofs - src[ip] - 1; ip += 1
                length += 3
            else:
                if length == 7:
                    if ip >= n:
                        break
                    length += src[ip]; ip += 1
                if ip >= n:
                    break
                ref = len(out) - ofs - src[ip] - 1; ip += 1
                length += 3
            if ref < 0:
                return None
            for _ in range(length):
                out.append(out[ref])
                ref += 1
                if len(out) >= maxout:
                    return bytes(out)
        else:
            length = ctrl + 1
            if level == 2 and length == 32:
                # level 2 uses a longer literal run
                while ip < n:
                    b = src[ip]; ip += 1; length += b
                    if b != 255:
                        break
            if ip + length > n:
                out += src[ip:]
                ip = n
            else:
                out += src[ip:ip + length]
                ip += length
        if ip >= n:
            break
        ctrl = src[ip]; ip += 1
        if len(out) >= maxout:
            break
    return bytes(out)


if __name__ == '__main__':
    import sys
    target = open('tmp/dblock.bin', 'rb').read()
    f = open('tmp/base.bin', 'rb').read()
    for start in (0x40e, 0x417, 0x419, 0x41a, 0x48d):
        for lvl in (1, 2):
            for extra in (0, 1, 2, 3):
                try:
                    o = decompress(f[start + extra:], lvl, 1 << 16)
                except Exception:
                    continue
                if not o:
                    continue
                m = 0
                while m < min(len(o), len(target)) and o[m] == target[m]:
                    m += 1
                if m >= 4 or len(o) > 3000:
                    print('start %#x +%d lvl %d -> %5d bytes, match %d, head %s' % (
                        start, extra, lvl, len(o), m, o[:12].hex(' ')))
