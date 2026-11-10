# FM8 envelope reverse-engineering handoff

Status: **resolved**.  The breakpoint envelopes, their flags and their
header-relative location are decoded and validated.  The full specification,
decoder API and corpus results are in [`ENVELOPES.md`](ENVELOPES.md); this note
is kept as a short record of how the open questions were closed.

## What changed since the open handoff

The original note said the record grammar was unsolved and that the envelope
data sat in an unmapped region around payload-relative `0x64a-0x8d5`.  That was
correct for the file's *parameter* offsets, but the envelope records are not
part of the parameter stream:

* they are anchored to the end of the four `FM8E` metadata strings, at
  `header_end + 1505` (flags at `header_end + 1118`);
* a record is `u8 nseg` followed by `nseg + 1` full, byte-aligned little-endian
  `float32` values (there is no compact float and no bit packing);
* three runs of nine records follow each other: times, levels, slopes;
* the destination order is `A, B, C, D, E, F, X, Z, Pitch`;
* times are stored in milliseconds: BP1 absolute, then per-segment deltas;
* the same grammar is used by the 1.7.4, 1.7.8 and 1.7.14 files tested.

The original `+49` shift observation was real, and it is now explained: it
applies to parameter fields **after** the envelope section, where
`+49 = (header 81 - 56) + (envelope section 601 - 577)`.  Fields before the
section (including the flag array) shift by `+25`.  The envelope/flag anchors
are header-relative, so they do not need either shift.  See
[`ENVELOPES.md`](ENVELOPES.md) §6.1.

## Corrected oracle values for `Whitty_Lead_`

The earlier A BP2 row (`0.113 / 0.112 / 0.653 / 0.528`) was read after an
exploratory click had dragged the point.  The clean file contains:

| dest | BP | AbsT | RelT | Level | Slope |
|---|---:|---:|---:|---:|---:|
| A | 1 | 0.001 | 0.001 | 1.000 | 0.247 |
| A | 2 | 0.114 | 0.113 | 0.649 | 0.528 |
| A | 3 | 0.214 | 0.100 | 0.649 | 0.500 |
| A | 4 | 0.254 | 0.040 | 0.000 | 0.917 |
| B/C/D | 1 | 0.001 | 0.001 | 1.000 | 0.247 |
| B/C/D | 2 | 0.101 | 0.100 | 1.000 | 0.500 |
| B/C/D | 3 | 0.141 | 0.040 | 0.000 | 0.917 |
| E | 1 | 0.001 | 0.001 | 1.000 | **0.227** |
| E | 2–3 | same as B | | | |
| F | 1 | 0.001 | 0.001 | 1.000 | 0.247 |
| F | 2 | 0.101 | 0.100 | 1.000 | 0.500 |
| F | 3 | **0.105** | **0.004** | 0.000 | 0.917 |
| X/Z | 1–3 | 0.001 / 0.501 / 0.502 | 0.001 / 0.500 / 0.001 | 1 / 1 / 0 | 0.5 / 0.9 / 0.9 |
| Pitch | 1–4 | 0.001 / 0.002 / 0.102 / 0.103 | 0.001 / 0.001 / 0.100 / 0.001 | 0 | 0.5 |

C is identical to B/D/E in this patch.  F has the separate 4 ms release the
original note suspected.

## How to decode

```python
import fm8param

p = fm8param.Patch('patch.nfm8')
envs = p.envelopes()               # A-F, X, Z, Pitch
for bp in envs['A'].breakpoints:
    print(bp.index, bp.abs_time_s, bp.rel_time_s, bp.level, bp.slope)
```

or:

```bash
python fm8env.py patch.nfm8 --raw
python fm8env.py patch.nfm8 -o F --json
python tools/check_envelopes.py
```

Validation currently reports:

```
GUI oracle: OK (A/B/D/F/Pitch and flags)
corpus: 1155 files
  breakpoint invariants : 1155/1155
  no sentinel times      : 1084/1155
  flag arrays 0/1/128/255: 1155/1155
writer oracle: OK (A times/level/slope and F flag survive FM8)
resize structure: OK (grow/shrink offsets; FM8 acceptance is separate)
```

The 71 files with sentinel times are unused X/Z envelopes containing
sentinel/uninitialised-looking time floats; they are flagged by `Envelope.has_sentinel` rather
than silently interpreted as musical time.

## What is still not done

* **Resizing envelopes.** Same-size writes (values and flags) are implemented
  and validated through FM8's `effSetChunk`.  Adding/removing breakpoints is
  implemented as `resize=True`, but FM8 1.7.14 reverted resized envelopes to the
  default shape in tests, so resizing is experimental.
* Proving the exact `Key Scaling` / `Vel Scaling` semantics of the two
  nine-record scaling runs that follow the slopes.
* Making the layout correction automatic by default (it is available as
  `fm8param.Patch.get(..., layout=True)` and `fm8env.schema_offset`).
* LFO shapes and other modulation curves not shown on Expert → Env.

Everything else in the original handoff (the GUI automation notes, the
acceptance targets, the recommendation to reload after exploratory clicks) still
applies and is preserved in [`ENVELOPES.md`](ENVELOPES.md).
