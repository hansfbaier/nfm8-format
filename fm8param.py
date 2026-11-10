#!/usr/bin/env python3
"""Read and write FM8 `.nfm8` synthesis parameters without the plugin.

A preset is a tagged node tree.  One node, the `EncryptionItem`, carries a
`[u32 uncompressedSize][u32 compressedSize][FastLZ level-1 stream]` record whose
plaintext is another node tree containing the `'tsrp'` chunk.  That chunk is the
`FM8E` parameter payload: a little-endian structure in which each VST parameter
occupies a fixed-width field.

`data/param_schema.json` records, for every VST parameter index, the payload
offset of its field and the measured transfer function from the 0..1 parameter
value to the stored value:

    f32    little-endian float32, value = a + b*v  (rounded when `quantised`)
    uN     little-endian integer,  value = round(a + b*v) mod 2^(8*N)
    enumN  N discrete stored values, listed in `values`

Everything here was derived by measurement and from the public FastLZ format; no
vendor code is involved.

Breakpoint envelopes are **not** VST parameters and are not in
`param_schema.json`; read them with `p.envelopes()` (implemented in `fm8env.py`).
The schema offsets above are relative to the 1.7.14 reference layout.  A patch
with a different header length or envelope-section size shifts later fields;
pass `layout=True` to `get`/`set` to apply the correction.

    import fm8param
    p = fm8param.Patch('preset.nfm8')
    print(p.get(200))            # current 0..1 value of parameter 200
    p.set(200, 0.75)
    p.write('out.nfm8')
    print(p.envelopes()['A'].breakpoints)
    print(p.get(178, layout=True))
"""
import json
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'analysis'))
import fastlz  # noqa: E402

SCHEMA_PATH = os.path.join(_HERE, 'data', 'param_schema.json')
HSIN = b'hsin'
DSIN = b'DSIN'
ONE = 1


def load_schema(path=SCHEMA_PATH):
    with open(path, encoding='utf-8') as fh:
        raw = json.load(fh)['params']
    return {int(k): v for k, v in raw.items()}


# --------------------------------------------------------------------------
# transfer functions
# --------------------------------------------------------------------------

def field_width(meta):
    model = meta['model']
    if model == 'f32':
        return 4
    if model.startswith('u'):
        return int(model[1:]) // 8
    return 1


def encode_value(meta, v):
    """Stored bytes for parameter value `v` (0..1); None for enum fields."""
    model = meta['model']
    if model == 'f32':
        x = meta['a'] + meta['b'] * v
        if meta.get('quantised'):
            x = round(x)
        return struct.pack('<f', x)
    if model.startswith('u'):
        x = round(meta['a'] + meta['b'] * v) % meta['mod']
        return x.to_bytes(field_width(meta), 'little')
    return None


def decode_value(meta, raw):
    """Inverse of encode_value: the 0..1 parameter value, or None for enums."""
    model = meta['model']
    b = meta.get('b', 0)
    if not b:
        return 0.0
    if model == 'f32':
        x = struct.unpack('<f', raw)[0]
    elif model.startswith('u'):
        x = int.from_bytes(raw, 'little')
        mod = meta.get('mod', 256)
        # A signed field wraps: bring the stored value into [a, a + mod) so the
        # affine inverse is continuous.  Step forward, never back, because the
        # field is stored modulo the width.
        a0 = meta['a']
        if b > 0:
            while x < a0:
                x += mod
        else:
            while x > a0:
                x -= mod
    else:
        return None
    return (x - meta['a']) / b


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------

class Patch:
    """An .nfm8 file with parameter access."""

    def __init__(self, path=None, data=None, schema=None):
        if path is not None:
            data = open(path, 'rb').read()
        if data is None:
            raise ValueError('need path= or data=')
        self.data = bytearray(data)
        self.schema = schema or load_schema()
        self._locate()

    def _locate(self):
        blocks = fastlz.find_blocks(self.data)
        if not blocks:
            raise ValueError('no FastLZ record found - not an FM8 preset?')
        self.block = blocks[0]
        plain_len, comp_len = struct.unpack_from('<II', self.data, self.block)
        self.plain_len, self.comp_len = plain_len, comp_len
        self.inner = bytearray(fastlz.decompress(
            bytes(self.data[self.block + 8:self.block + 8 + comp_len]), 1))
        if len(self.inner) != plain_len:
            raise ValueError('record says %d bytes, decoded %d'
                             % (plain_len, len(self.inner)))
        self.payload = self.inner.find(b'E8MF')
        if self.payload < 0:
            raise ValueError("no 'FM8E' payload in the decoded tree")

    def _size_fields(self):
        """Outer length fields whose span encloses the record.

        Only tags outside the compressed payload count: the stream is full of
        byte sequences that look like node headers, and treating those as nodes
        silently corrupts unrelated words.
        """
        d, n = self.data, len(self.data)
        rec_start = self.block
        rec_end = self.block + 8 + self.comp_len
        found = []
        for i in range(0, n - 16):
            if rec_start <= i < rec_end:
                continue
            if d[i:i + 4] == HSIN and int.from_bytes(d[i + 4:i + 8], 'little') == ONE:
                candidates = ((i - 12, 8), (i + 28, 4))
            elif d[i:i + 4] == DSIN:
                candidates = ((i - 8, 8), (i + 12, 4))
            else:
                continue
            for off, width in candidates:
                if off < 0 or off + width > n:
                    continue
                span = int.from_bytes(d[off:off + width], 'little')
                if not 0 < span <= n:
                    continue
                if off + width + span >= rec_end and off + span >= rec_start:
                    if (off, width) not in [(o, w) for o, w, _ in found]:
                        found.append((off, width, span))
        return found

    # -- parameter access ----------------------------------------------------
    def _field(self, index, layout=False):
        meta = self.schema.get(index)
        if meta is None:
            raise KeyError('parameter %d is not in the schema' % index)
        offset = meta['offset']
        if layout:
            # The schema offsets were measured on the 1.7.14 reference chunk.
            # A different header length or envelope-section size shifts every
            # field after the envelope section; fm8env knows the correction.
            import fm8env
            offset = fm8env.schema_offset(self, offset)
        return meta, self.payload + offset, field_width(meta)

    def get(self, index, *, layout=False):
        """Current 0..1 value of parameter `index`.

        `layout=True` applies the version/envelope-section offset correction
        (needed for patches whose envelope block differs from the 1.7.14
        reference).  The default preserves the measured reference offsets.
        """
        meta, off, width = self._field(index, layout)
        return decode_value(meta, bytes(self.inner[off:off + width]))

    def set(self, index, value, *, layout=False):
        """Set parameter `index` to a 0..1 value; see `get()` for `layout`."""
        meta, off, width = self._field(index, layout)
        raw = encode_value(meta, value)
        if raw is None:
            raise ValueError('parameter %d is an enum; write its raw value' % index)
        self.inner[off:off + width] = raw

    def enum_values(self, index):
        meta = self.schema.get(index)
        if meta and meta['model'].startswith('enum'):
            return list(meta['values'])
        return None

    # -- breakpoint envelopes ------------------------------------------------
    def envelopes(self):
        """Decode the nine breakpoint envelopes (A-F, X, Z, Pitch).

        Returns a ``{destination: fm8env.Envelope}`` mapping.  Envelope data
        is not a VST parameter and is not covered by ``param_schema.json``;
        see docs/ENVELOPES.md.
        """
        import fm8env
        return fm8env.read_envelopes(self)

    def envelope_section(self):
        """Decode envelopes plus the raw key/velocity-scaling record runs."""
        import fm8env
        return fm8env.read_section(self)

    def env_flags(self):
        """Return the P.Env / Env Loop / Env Rel flags per destination."""
        import fm8env
        return fm8env.read_flags(self)

    def keyscales(self):
        """Return the pitch/level/slope keyscale curves."""
        import fm8env
        return fm8env.read_keyscales(self)

    def write_keyscales(self, keyscales, resize=False):
        """Write keyscale curves; same-size by default."""
        import fm8env
        return fm8env.write_keyscales(self, keyscales, resize=resize)

    # -- write ---------------------------------------------------------------
    def to_bytes(self):
        stream = fastlz.compress(bytes(self.inner))
        delta = len(stream) - self.comp_len
        out = bytearray(self.data)
        sizes = self._size_fields()
        out[self.block:self.block + 8 + self.comp_len] = (
            struct.pack('<II', self.plain_len, len(stream)) + stream)
        for off, width, span in sizes:
            out[off:off + width] = (span + delta).to_bytes(width, 'little')
        return bytes(out)

    def write(self, path):
        with open(path, 'wb') as fh:
            fh.write(self.to_bytes())
