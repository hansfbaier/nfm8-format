#!/usr/bin/env python3
"""
nfm8.py - reader/writer for Native Instruments FM8 patch files (.nfm8)

The .nfm8 file is byte-for-byte the VST plugin state chunk returned by
FM8.dll via effGetChunk(), and FM8.dll accepts such a file through
effSetChunk().  Tested against FM8 1.7.8-written patches and the 1.7.14
64-bit plugin.

This module is dependency-free (Python 3.6+, stdlib only).

Container layout
----------------
  +0   u32  total file size in bytes
  +4   u32  0
  +8   u32  1
  +12  node tree, terminated by u32 0

Nodes
-----
  'hsin' ("nish")  container node
      +4   u32 a = 1
      +8   u32 b = 0
      +12  16-byte GUID (new random value on every save)
      +28  u32 payload length
      +32  u32 f = 0
      +36  payload (child nodes and/or raw data)

  'DSIN' ("NISD")  typed value node
      +4   u32 type id   (1 object, 101 module, 106/108/116 leaf,
                          115 list, 118 root, 121 map)
      +8   u32 payload length
      +12  u32 c
      +16  u32 d
      +20  payload

Metadata
--------
  u32 name_chars,    UTF-16LE name      e.g. "FM8Clap"
  u32 author_chars,  UTF-16LE author    e.g. "not set"
  u32 comment_chars, UTF-16LE comment
  ...32 bytes... , u32 1, u32 1, u32 1
  u32 3, UTF-16LE "FM8"                 (product tag)
  u32 0, u32 0
  u32 count, then count * ( u32 klen, key_utf16, u32 vlen, value_utf16 )

  keys are prefixed with "\\@":
      \\@color  \\@devicetypeflags  \\@soundtype  \\@tempo
      \\@verl   \\@verm            \\@visib

Synthesis parameters
--------------------
Stored in the type-116 ('t') subtree as raw bytes.  The blob starts with:

      u32 table_size   per patch (3291..3771 in the reference library)
      u32 stream_len   length of the encoded stream that follows
      u8  codec        = 2
      3 bytes          == table_size & 0xFFFFFF
      7 bytes          == 60 00 00 01 20 05 03
      stream           begins with the ASCII marker "hsin"

Values are little-endian float32 written with only their significant bytes:
"80 3d" is the upper half of float32 0x3d800000 == 0.0625, while 0.4 is stored
verbatim as "cd cc cc 3e".  See docs/FORMAT.md for the evidence and for what is
still undecoded (the stream-position -> parameter-id table).
"""

import argparse
import json
import struct
import sys

def u32(b, o): return struct.unpack_from('<I', b, o)[0]
def p32(v):   return struct.pack('<I', v)

NODE_TYPES = {1: 'object', 101: 'module', 106: 'leaf106', 108: 'leaf108',
              115: 'list', 116: 'params', 118: 'root', 121: 'map'}


class Node:
    __slots__ = ('tag', 'off', 'type_id', 'size', 'c', 'd', 'guid', 'payload', 'end')

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def kind(self):
        return ('container' if self.tag == 'hsin'
                else NODE_TYPES.get(self.type_id, 'type%d' % self.type_id))

    def __repr__(self):
        return '<%s @%#06x %s size=%d>' % (self.tag, self.off, self.kind, self.size)


class Patch:
    """An .nfm8 patch.  `Patch(path)` or `Patch(data=bytes)`."""

    def __init__(self, path=None, data=None):
        if path is not None:
            data = open(path, 'rb').read()
        if data is None:
            raise ValueError('need path= or data=')
        self.path = path
        self.data = bytearray(data)
        if len(self.data) < 16 or bytes(self.data[12:16]) not in (b'hsin', b'DSIN'):
            raise ValueError('not an FM8 patch: no hsin/DSIN node at offset 12')

    # ---------------- structure ----------------
    def tree(self):
        """Parse the node tree properly.  Returns [(node, parent, depth)].

        A node's payload is treated as children when it parses cleanly as a
        sequence of nodes that exactly fills the declared payload length;
        otherwise the payload is raw data.  This matters because a node's raw
        payload can contain the bytes 'hsin'/'DSIN' by accident (e.g. the
        parameter blob does), so a pure scan for those fourccs produces false
        positives that would corrupt size fix-ups.
        """
        d, n = self.data, len(self.data)
        out = []

        def parse(off, limit, parent, depth):
            if off + 20 > limit:
                return None
            fourcc = d[off:off+4]
            if fourcc == b'hsin':
                if off + 36 > limit: return None
                if d[off+4:off+8] != p32(1): return None
                if d[off+8:off+12] != p32(0): return None
                size = u32(d, off+28)
                node = Node(tag='hsin', off=off, type_id=None, size=size,
                            c=u32(d, off+32), d=None, guid=bytes(d[off+12:off+28]),
                            payload=off+36, end=off+36+size)
                hdr = 36
            elif fourcc == b'DSIN':
                tid = u32(d, off+4)
                if tid > 300: return None
                size = u32(d, off+8)
                node = Node(tag='DSIN', off=off, type_id=tid, size=size,
                            c=u32(d, off+12), d=u32(d, off+16), guid=None,
                            payload=off+20, end=off+20+size)
                hdr = 20
            else:
                return None

            idx = len(out)
            out.append((node, parent, depth))

            ps = node.payload
            child_end = min(node.end, n)
            # A payload may be children, raw data, or children followed by raw
            # fields; parse children greedily from the start and keep whatever
            # parses cleanly.
            cur = ps
            while cur < child_end and d[cur:cur+4] in (b'hsin', b'DSIN'):
                r = parse(cur, child_end, idx, depth+1)
                if r is None or r <= cur:
                    break
                cur = r
            return node.end

        off = 12
        while off < n:
            if d[off:off+4] == p32(0):     # separator between top-level nodes
                off += 4
                continue
            nxt = parse(off, n, None, 0)
            if nxt is None or nxt <= off:
                break
            off = nxt
        return out

    def nodes(self):
        """All nodes from the parsed tree (outermost first)."""
        return [node for node, _parent, _depth in self.tree()]

    # DSIN nodes do not consistently put their payload length in the same
    # header field: types 101/108/116/118/121 use the field at +12 (c) in most
    # files, while the outermost module node puts it at +8 (b).  hsin always
    # uses +28.  _size_at() returns the offset of the field whose span covers
    # `pos`, preferring the larger (and therefore enclosing) span.
    def _size_at(self, node, pos):
        if node.tag == 'hsin':
            return node.off + 28
        for off in (node.off + 12, node.off + 8):
            size = u32(self.data, off)
            if node.payload <= pos <= node.payload + size + 16:
                return off
        return None

    def ancestors(self, pos):
        """[(node, size_field_offset)] whose declared span covers `pos`."""
        out = []
        d = self.data
        for i in range(12, len(d) - 20):
            tag = d[i:i+4]
            if tag == b'hsin':
                if i + 36 > len(d): continue
                if d[i+4:i+8] != p32(1) or d[i+8:i+12] != p32(0): continue
                node = Node(tag='hsin', off=i, type_id=None, size=u32(d, i+28),
                            c=0, d=None, guid=None, payload=i+36,
                            end=i+36+u32(d, i+28))
            elif tag == b'DSIN':
                tid = u32(d, i+4)
                if tid > 300: continue
                node = Node(tag='DSIN', off=i, type_id=tid, size=u32(d, i+8),
                            c=u32(d, i+12), d=u32(d, i+16), guid=None,
                            payload=i+20, end=i+20+u32(d, i+12))
            else:
                continue
            f = self._size_at(node, pos)
            if f is not None:
                out.append((node, f))
        out.sort(key=lambda t: t[0].payload)
        return out

    def blob_info(self):
        """Locate the synthesis-parameter blob.

        Identified by a self-checking header signature rather than by tree
        walking (the type-116 node appears twice in the tree):

            +0x00 u32 table_size
            +0x04 u32 stream_len
            +0x08 u8  codec (2)
            +0x09 3 bytes == table_size & 0xFFFFFF
            +0x0c 7 bytes == 60 00 00 01 20 05 03
            +0x13 <stream>  begins with the ASCII marker 'hsin'

        The stream is [data_offset, data_offset + stream_len) and is followed by
        a few trailing bytes (usually 2) that are not counted in stream_len.
        """
        d = self.data
        n = len(d)
        marker = bytes.fromhex('60000001200503')
        for p in range(0x100, n - 0x17):
            if d[p+19:p+23] != b'hsin' or d[p+12:p+19] != marker:
                continue
            table_size = u32(d, p)
            stream_len = u32(d, p+4)
            codec = d[p+8]
            if not (256 <= table_size <= (1 << 20) and 1 <= codec <= 8):
                continue
            if d[p+9:p+12] != p32(table_size)[:3]:
                continue
            start = p + 0x13
            return {'offset': p, 'data_offset': start,
                    'end': min(n, start + stream_len),
                    'table_size': table_size, 'stream_len': stream_len,
                    'codec': codec, 'trailing': n - (start + stream_len)}
        return None

    def param_blob(self):
        """(start, end) of the encoded synthesis-parameter stream, or None."""
        info = self.blob_info()
        if not info: return None
        return info['data_offset'], info['end']

    # ---------------- metadata ----------------
    def _meta_offset(self):
        i = self.data.find('\\@color'.encode('utf-16-le'))
        return None if i < 0 else i - 8

    def read_meta(self):
        """Property map as a dict (keys keep their '\\@' prefix)."""
        start = self._meta_offset()
        if start is None: return {}
        d = self.data
        cnt = u32(d, start)
        if cnt > 256: return {}
        i, kv = start + 4, {}
        for _ in range(cnt):
            kl = u32(d, i); i += 4
            k = bytes(d[i:i+2*kl]).decode('utf-16-le'); i += 2*kl
            vl = u32(d, i); i += 4
            v = bytes(d[i:i+2*vl]).decode('utf-16-le'); i += 2*vl
            kv[k] = v
        return kv

    @staticmethod
    def _dec(d, a, b):
        if b - a < 0: return None
        try:
            s = bytes(d[a:b]).decode('utf-16-le')
        except Exception:
            return None
        if any((ord(c) < 32 and c not in '\t\n') for c in s):
            return None
        return s

    def _fields_before(self, end, window=420):
        d = self.data
        out = {}
        for p in range(max(0, end - window), end):
            n = u32(d, p)
            if n > 200: continue
            e = p + 4 + 2*n
            if e > end: continue
            s = self._dec(d, p+4, e)
            if s is None: continue
            out[p] = (n, s, e)
        return out

    def read_strings(self):
        """name / author / comment (plus their byte offsets) or None."""
        d = self.data
        prod = p32(3) + 'FM8'.encode('utf-16-le')
        mm = self._meta_offset()
        anchors = []
        if mm is not None:
            j = d.rfind(prod, 0, mm)
            if j > 0: anchors.append(j)
        j = d.find(prod)
        while j > 0:
            anchors.append(j)
            j = d.find(prod, j+1)
        for anchor in anchors:
            fields = self._fields_before(anchor)
            best = None
            for p, (n, s, e) in fields.items():
                if not s.strip(): continue
                chain, cur = [(p, n, s)], e
                while cur in fields:
                    n2, s2, e2 = fields[cur]
                    chain.append((cur, n2, s2)); cur = e2
                gap = anchor - (chain[-1][0] + 4 + 2*chain[-1][1])
                if 0 <= gap <= 96:
                    score = (len(chain), -gap)
                    if best is None or score > best[0]:
                        best = (score, chain)
            if best:
                ch = best[1]
                return {'name': ch[0][2], 'name_off': ch[0][0],
                        'author': ch[1][2] if len(ch) > 1 else '',
                        'author_off': ch[1][0] if len(ch) > 1 else None,
                        'comment': ch[2][2] if len(ch) > 2 else '',
                        'comment_off': ch[2][0] if len(ch) > 2 else None}
        return None

    def summary(self):
        s = self.read_strings() or {}
        return {'file': self.path, 'size': len(self.data),
                'name': s.get('name'), 'author': s.get('author'),
                'comment': s.get('comment'), 'meta': self.read_meta(),
                'blob': self.blob_info()}

    # ---------------- editing ----------------
    def _grow(self, pos, delta):
        """Patch every enclosing length field after a resize at `pos`."""
        if delta == 0: return
        for _node, off in self.ancestors(pos):
            self.data[off:off+4] = p32(u32(self.data, off) + delta)
        self.data[0:4] = p32(u32(self.data, 0) + delta)

    def _write_field(self, off, old_chars, value):
        seg = p32(len(value)) + value.encode('utf-16-le')
        old_len = 4 + 2*old_chars
        self.data[off:off+old_len] = seg
        self._grow(off, len(seg) - old_len)

    def set_strings(self, name=None, author=None, comment=None):
        s = self.read_strings()
        if s is None:
            raise RuntimeError('name/author fields not found')
        todo = []
        if comment is not None and s['comment_off'] is not None:
            todo.append((s['comment_off'], len(s['comment']), comment))
        if author is not None:
            if s['author_off'] is None:
                off = s['name_off'] + 4 + 2*len(s['name'])
                seg = p32(len(author)) + author.encode('utf-16-le')
                self.data[off:off] = seg
                self._grow(off, len(seg))
            else:
                todo.append((s['author_off'], len(s['author']), author))
        if name is not None:
            todo.append((s['name_off'], len(s['name']), name))
        for off, ln, val in sorted(todo, reverse=True):
            self._write_field(off, ln, val)

    def set_meta(self, key, value):
        """Set one property-map entry, e.g. set_meta('color', '3')."""
        if not key.startswith('\\@'): key = '\\@' + key
        start = self._meta_offset()
        if start is None:
            raise RuntimeError('property map not found')
        d = self.data
        cnt = u32(d, start)
        i = start + 4
        for _ in range(cnt):
            kl = u32(d, i)
            k = bytes(d[i+4:i+4+2*kl]).decode('utf-16-le')
            vl_at = i + 4 + 2*kl
            vl = u32(d, vl_at)
            v_at = vl_at + 4
            if k == key:
                new = value.encode('utf-16-le')
                d[v_at:v_at+2*vl] = new
                d[vl_at:vl_at+4] = p32(len(value))
                self._grow(vl_at, len(new) - 2*vl)
                return
            i = v_at + 2*vl
        raise KeyError('%s not in property map' % key)

    def save(self, path=None):
        open(path or self.path, 'wb').write(self.data)


# ---------------------------------- CLI ------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='nfm8.py', description='Read/write Native Instruments FM8 .nfm8 patches.',
        epilog='See docs/FORMAT.md for the file format notes.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('info', help='show metadata (name, author, properties)')
    p.add_argument('files', nargs='+')
    p.add_argument('--json', action='store_true')

    p = sub.add_parser('nodes', help='list container nodes with offsets')
    p.add_argument('file')

    p = sub.add_parser('blob', help='extract the raw parameter blob')
    p.add_argument('file'); p.add_argument('out')

    p = sub.add_parser('hexdump', help='annotated hexdump of the parameter stream')
    p.add_argument('file')
    p.add_argument('--start', type=lambda v: int(v, 0), default=None,
                   help='stream-relative start offset (default: blob header)')
    p.add_argument('--len', type=lambda v: int(v, 0), default=256)

    for name, helptext in (('set-name', 'set patch name'),
                           ('set-author', 'set author'),
                           ('set-comment', 'set comment')):
        p = sub.add_parser(name, help=helptext)
        p.add_argument('file'); p.add_argument('value')
        p.add_argument('-o', '--out', help='write here instead of in place')

    p = sub.add_parser('set-meta', help='set a property, e.g. color / soundtype / visib')
    p.add_argument('file'); p.add_argument('key'); p.add_argument('value')
    p.add_argument('-o', '--out', help='write here instead of in place')

    args = ap.parse_args(argv)

    if args.cmd == 'info':
        out = []
        for f in args.files:
            s = Patch(f).summary()
            if args.json:
                out.append(s); continue
            print('== %s (%d bytes)' % (f, s['size']))
            print('   name    : %r' % s['name'])
            print('   author  : %r' % s['author'])
            print('   comment : %r' % s['comment'])
            if s['blob']:
                b = s['blob']
                print('   params  : offset %#x, table_size %d, stream_len %d, codec %d'
                      % (b['offset'], b['table_size'], b['stream_len'], b['codec']))
            for k, v in s['meta'].items():
                print('   %-18s: %r' % (k, v))
        if args.json:
            print(json.dumps(out, indent=2))
        return 0

    if args.cmd == 'nodes':
        for node, _parent, depth in Patch(args.file).tree():
            print('%s%06x  %-4s %-10s type=%-4s size=%-5d payload=%06x..%06x'
                  % ('  ' * depth, node.off, node.tag, node.kind,
                     node.type_id, node.size, node.payload, node.end))
        return 0

    if args.cmd == 'blob':
        p = Patch(args.file)
        r = p.param_blob()
        if not r:
            print('no parameter blob found', file=sys.stderr); return 1
        a, b = r
        open(args.out, 'wb').write(bytes(p.data[a:b]))
        print('%s: wrote %d bytes (stream %#x..%#x)' % (args.out, b-a, a, b))
        return 0

    if args.cmd == 'hexdump':
        p = Patch(args.file)
        info = p.blob_info()
        if not info:
            print('no parameter blob found', file=sys.stderr); return 1
        start = info['data_offset'] if args.start is None else info['data_offset'] + args.start
        print('blob header %#x  table_size=%d stream_len=%d codec=%d  stream %#x..%#x'
              % (info['offset'], info['table_size'], info['stream_len'],
                 info['codec'], info['data_offset'], info['end']))
        data = p.data
        end = min(len(data), start + args.len)
        for off in range(start, end, 16):
            chunk = bytes(data[off:off+16])
            text = ''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)
            print('%06x  %-47s  %s' % (off, chunk.hex(' '), text))
        return 0

    if args.cmd in ('set-name', 'set-author', 'set-comment'):
        field = args.cmd[4:]
        p = Patch(args.file)
        p.set_strings(**{field: args.value})
        p.save(args.out)
        print('%s: %s -> %r (%d bytes)'
              % (args.out or args.file, field, args.value, len(p.data)))
        return 0

    if args.cmd == 'set-meta':
        p = Patch(args.file)
        p.set_meta(args.key, args.value)
        p.save(args.out)
        print('%s: %s -> %r (%d bytes)'
              % (args.out or args.file, args.key, args.value, len(p.data)))
        return 0

    return 1


if __name__ == '__main__':
    sys.exit(main())
