#!/usr/bin/env python3
"""
fm8_oracle.py - verify .nfm8 edits against the real FM8 plugin.

Loads two patches through FM8.dll (via vsthost) and compares the resulting
plugin states.  A metadata-only edit must produce differences *only* inside the
16-byte GUID slots, because FM8 regenerates GUIDs on every save.

usage:
    python tools/fm8_oracle.py original.nfm8 edited.nfm8 [--dll PATH] [--keep]

Environment:
    FM8_DLL   default path to FM8.dll (overrides the built-in guess)
"""

import argparse
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VSTHOST = os.path.join(ROOT, 'vsthost', 'vsthost.exe')

DEFAULT_DLLS = [
    r'C:\Program Files\Native Instruments\VSTPlugins 64 bit\FM8.dll',
    r'C:\Program Files\Native Instruments\VSTPlugins 32 bit\FM8.dll',
    r'C:\Program Files\Native Instruments\FM8\FM8.dll',
]


def find_dll(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get('FM8_DLL')
    if env:
        return env
    for p in DEFAULT_DLLS:
        if os.path.exists(p):
            return p
    raise SystemExit('FM8.dll not found; pass --dll or set FM8_DLL')


def roundtrip(dll, patch, out):
    if not os.path.exists(VSTHOST):
        raise SystemExit('vsthost.exe missing - run `make` in vsthost/ first')
    r = subprocess.run([VSTHOST, dll, 'setchunk', patch, out],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise SystemExit('vsthost failed for %s:\n%s%s' % (patch, r.stdout, r.stderr))
    return open(out, 'rb').read()


def guid_slots(data):
    """Byte ranges holding regenerated 16-byte IDs.

    FM8 writes three kinds of 16-byte object id, each after a different marker:

        'hsin'                      -> id at +12   (container node)
        'hsin' + 40 07 40 00 10     -> id at +9    (param-block marker)
        40 23 e0 03 53 10           -> id at +6    (embedded sub-object)

    Matching loosely on purpose: over-masking a few bytes is acceptable for this
    diagnostic check, under-masking produces false failures.
    """
    slots = []
    for marker, off in ((b'hsin', 12), (b'hsin', 9),
                        (bytes.fromhex('4023e0035310'), 6)):
        i = data.find(marker)
        while i >= 0:
            slots.append((i + off, i + off + 16))
            i = data.find(marker, i + 1)
    return slots


def diff_outside_guids(a, b):
    n = min(len(a), len(b))
    slots = guid_slots(a)
    bad = []
    for i in range(n):
        if a[i] == b[i]:
            continue
        if any(s <= i < e for s, e in slots):
            continue
        bad.append(i)
    return bad, len(a) - n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('original')
    ap.add_argument('edited')
    ap.add_argument('--dll')
    ap.add_argument('--keep', action='store_true', help='keep the dumped chunks')
    args = ap.parse_args()

    dll = find_dll(args.dll)
    tmp = tempfile.mkdtemp(prefix='fm8-oracle-')
    a_bin = os.path.join(tmp, 'a.bin')
    b_bin = os.path.join(tmp, 'b.bin')

    print('dll     :', dll)
    a = roundtrip(dll, args.original, a_bin)
    b = roundtrip(dll, args.edited, b_bin)
    bad, lendiff = diff_outside_guids(a, b)
    runs = []
    for i in bad:
        if runs and i == runs[-1][1]:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])

    print('state A : %d bytes (%s)' % (len(a), args.original))
    print('state B : %d bytes (%s)' % (len(b), args.edited))
    if lendiff:
        print('length difference:', lendiff)
    if bad:
        print('FAIL: %d differing bytes outside GUID slots' % len(bad))
        print('      runs:', ['%#x+%d' % (s, e - s) for s, e in runs[:12]])
        if args.keep:
            print('      chunks kept in', tmp)
        return 1
    print('OK: plugin states are identical apart from regenerated GUIDs')
    if args.keep:
        print('chunks kept in', tmp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
