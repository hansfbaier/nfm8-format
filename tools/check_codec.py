#!/usr/bin/env python3
"""Prove the codec round-trip on real files.

For every FastLZ block found in the given files, decompress it and compress the
result again, then compare with the bytes that were stored.  A block passes only
if the re-encoded stream is identical byte for byte - not merely decodable.

    python tools/check_codec.py *.nfm8
    python tools/check_codec.py ../Sounds

This is an independent implementation; no vendor code is involved.  Any file the
tool can read is enough to run the check - factory presets are never
redistributed by this repository, only read in place from the user's own copy.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'analysis'))
import fastlz  # noqa: E402


def check_file(path):
    """Return (blocks, passed, failures) for one file."""
    data = open(path, 'rb').read()
    passes = failures = 0
    detail = []
    for pos in fastlz.find_blocks(data):
        plain_len, comp_len = struct.unpack_from('<II', data, pos)
        stored = data[pos + 8:pos + 8 + comp_len]
        try:
            plain = fastlz.decompress(stored, 1)
        except Exception as exc:                       # malformed block
            failures += 1
            detail.append('block %#x: decompress failed (%s)' % (pos, exc))
            continue
        if len(plain) != plain_len:
            failures += 1
            detail.append('block %#x: decoded %d bytes, header says %d'
                          % (pos, len(plain), plain_len))
            continue
        again = fastlz.compress(plain)
        if again == stored:
            passes += 1
        else:
            failures += 1
            first = next((i for i in range(min(len(again), len(stored)))
                          if again[i] != stored[i]), min(len(again), len(stored)))
            detail.append('block %#x: %d bytes vs %d stored, first diff at %d'
                          % (pos, len(again), len(stored), first))
    return passes, failures, detail


def expand(paths):
    for arg in paths:
        if os.path.isdir(arg):
            for name in sorted(os.listdir(arg)):
                if name.lower().endswith('.nfm8'):
                    yield os.path.join(arg, name)
        else:
            yield arg


def main(argv):
    paths = list(expand(argv[1:] or ['.']))
    if not paths:
        print('no input files')
        return 2
    total_p = total_f = files_ok = 0
    for path in paths:
        try:
            p, f, detail = check_file(path)
        except (OSError, ValueError) as exc:
            print('%-40s  skip (%s)' % (os.path.basename(path), exc))
            continue
        if p == 0 and f == 0:
            print('%-40s  no FastLZ block' % os.path.basename(path))
            continue
        total_p += p
        total_f += f
        files_ok += (f == 0)
        status = 'ok' if f == 0 else 'FAIL'
        print('%-40s  %-4s  %d block(s) identical' % (os.path.basename(path), status, p))
        for line in detail:
            print('    ' + line)
    print()
    print('%d file(s), %d block(s) re-encoded byte-identically, %d failed'
          % (len(paths), total_p, total_f))
    return 0 if total_f == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
