# FM8 breakpoint envelopes — file format and decoder

Status: **solved for the nine breakpoint envelopes (A–F, X, Z, Pitch)**, the
per-envelope `P.Env` / `Env Loop` / `Env Rel` flags, and their position in all
1.7.x payloads tested.  The raw key/velocity-scaling record runs that follow are
documented but not fully interpreted.

This document supersedes the open questions in
[`ENVELOPES_HANDOFF.md`](ENVELOPES_HANDOFF.md).  All values below were measured
from `.nfm8` files and checked against the FM8 1.7.14 GUI; no Native Instruments
code or data is involved.

## TL;DR

* The envelopes are **not** VST parameters and are not in
  `data/param_schema.json`.  They are records in the `FM8E` payload.
* The envelope section is anchored to the end of the four `FM8E` metadata
  strings (`header_end`), **not** to a fixed payload offset:
  * breakpoint records at `header_end + 1505`;
  * 27 flag bytes at `header_end + 1118`.
* A record is `u8 nseg` followed by `nseg + 1` little-endian `float32` values.
  There is no compacting and no bit packing: every value is a full 4-byte float.
* Three parallel runs of nine records follow each other:
  **times**, **levels**, **slopes**.  The nine destinations, in order, are
  `A, B, C, D, E, F, X, Z, Pitch`.
* Times are stored in **milliseconds**.  The first value is the absolute time of
  BP1; every later value is a delta from the previous breakpoint.  FM8 displays
  seconds, i.e. milliseconds / 1000.
* Levels and slopes are stored directly in `0..1`.
* The grammar is identical in files written by FM8 **1.7.4, 1.7.8 and 1.7.14**.
  Only the header length changes, which is why the anchors are header-relative.
* Decode with `python fm8env.py patch.nfm8`, or
  `fm8param.Patch(path).envelopes()`.

## 1. Where the data lives

`fm8param` already locates the `FM8E` payload and decompresses it.  The payload
begins with:

```
u32  'FM8E'        (bytes 45 38 4d 46)
u32  0xcf
u32  0x23bc
4 x { u32 len, ASCII bytes }     name, author, comment, description
```

`header_end` is the first byte after the fourth string.  For the test patches:

| patch | payload start | header_end | header length |
|---|---:|---:|---:|
| `tmp/base.bin` (1.7.14 default) | 204 | 260 | 56 |
| `tmp/Whitty_Lead_.nfm8` (1.7.8) | 188 | 269 | 81 |

The envelope anchors are:

| data | header-relative | base (1.7.14) payload-relative | Whitty (1.7.8) payload-relative |
|---|---:|---:|---:|
| 27 flag bytes | +1118 | +1174 | +1199 |
| 27 breakpoint records | +1505 | +1561 | +1586 |

In other words:

```python
from fm8param import Patch
import fm8env

p = Patch('patch.nfm8')
he = fm8env.header_end(p)          # end of the four strings
flags_at = he + 1118               # 27 bytes
breakpoints_at = he + 1505         # first record
```

`fm8env.locate_breakpoints()` first tries `header_end + 1505`, validates the
27-record structure, and only scans a window if that validation fails.

## 2. Record grammar

A breakpoint record is:

```
u8    nseg            number of segments = number of breakpoints - 1
f32   value[0..nseg]  nseg + 1 values, little-endian
```

* The record is byte-aligned.  Values are ordinary 4-byte `float32`, not the
  compact/tokenised form seen in some other parts of the payload.
* `nseg` can be 0 (one breakpoint) through 30 (31 breakpoints) in the factory
  corpus.  The decoder allows up to 96.
* `value[0]` of the time run is the **absolute** time of BP1.
* For `i >= 1`, `value[i]` of the time run is the **relative** time from BP
  `i` to BP `i+1`.

The three runs are consecutive, nine records each:

| records | run | meaning |
|---|---|---|
| 0–8 | times | BP1 absolute time, then segment deltas, milliseconds |
| 9–17 | levels | level of each breakpoint, `0..1` |
| 18–26 | slopes | slope of each breakpoint, `0..1` |

The destination order inside every run is:

```
index  0   1   2   3   4   5   6   7   8
dest   A   B   C   D   E   F   X   Z   Pitch
```

**Structural invariant (used by the validator):** envelope `i` must have the
same `nseg` in the time, level and slope runs.  This is what makes the run
locatable without guessing offsets.

The records are variable length, so the end of the section depends on the
breakpoint counts.  The two scaling runs follow (see §6).

## 3. Time interpretation

For stored time values `v[0..n]` (milliseconds):

```
AbsT[0] = v[0]
AbsT[i] = AbsT[i-1] + v[i]          (i >= 1)

RelT[0] = v[0]
RelT[i] = v[i]                      (i >= 1)
```

FM8's fields show `AbsT / 1000` and `RelT / 1000` seconds, rounded to three
decimals.

Example (`B`, stored `[1, 100, 40]`):

| BP | AbsT | RelT | FM8 display |
|---|---:|---:|---|
| 1 | 1 ms | 1 ms | `0.001 / 0.001` |
| 2 | 1 + 100 = 101 ms | 100 ms | `0.101 / 0.100` |
| 3 | 101 + 40 = 141 ms | 40 ms | `0.141 / 0.040` |

This matches the GUI screenshots in `tmp/printC.bmp` (B BP1) and
`tmp/printBP2.bmp` (B BP3).

## 4. Levels and slopes

Levels and slopes are stored directly in `0..1`, in breakpoint order.  FM8
rounds the display to three decimals; the stored float can differ by one or two
ULPs.  For example `0.649` is stored as `de 24 26 3f` = `0.64900005`, which the
GUI displays as `0.649`.

A level of `0.0` is sometimes stored as a tiny positive value such as `1e-12`.
Treat values below `1e-9` as zero when comparing with the GUI.

## 5. Flags: `P.Env`, `Env Loop`, `Env Rel`

The 27 flags are a contiguous byte array.  Each is a boolean (`0` or `1`;
values `128`/`255` are accepted by the decoder as non-zero).

```
header_end + 1118 + (index - 629)
```

or, on the 1.7.14 schema layout, payload offset `1174 + (index - 629)`.

| parameter indices | flag | destinations |
|---|---|---|
| 629–637 | `P.Env` | A, B, C, D, E, F, X, Z, Pitch |
| 638–646 | `Env Loop` | A, B, C, D, E, F, X, Z, Pitch |
| 647–655 | `Env Rel` | A, B, C, D, E, F, X, Z, Pitch |

The offsets were confirmed with the VST oracle: setting parameters `630`, `638`,
`647` and `655` to `0.0` changes exactly bytes `1175`, `1183`, `1192` and `1200`
in the 1.7.14 payload.  All 1155 factory patches have a valid flag array.

> The earlier note that the +49 shift was needed for this patch applies to
> parameter blocks **after** the envelope section.  At the envelope/flag
> anchors, the correct version rule is header-relative; Whitty (1.7.8) is
> `schema_offset + 25`, not `+ 49`, for the flag bytes.

## 6. The two scaling runs

After the 27 breakpoint records the same record grammar continues for exactly
two more runs of nine records:

| run | records | observed values | interpretation |
|---|---|---|---|
| `scaling_a` | 9 | `0..127` (e.g. `0, 60, 127`) | probably key/velocity positions |
| `scaling_b` | 9 | mostly `-80..0`, sometimes `0..1` | probably dB/amounts for those positions |

The Env page exposes numeric `Key Scaling` and `Vel Scaling` fields; the VST
parameters `Op. X EnvKeyScal` / `EnvVelScal` (indices 602–619) are mapped by
`param_schema.json`.  The two nine-record runs are almost certainly the
corresponding curve data, but which run is key and which is velocity has not
been proven against the GUI.  `fm8env` therefore exposes them raw
(`EnvelopeSection.scaling_a`, `.scaling_b`) instead of inventing a breakpoint
interpretation.

Base patch example: `scaling_a` is `[0, 60, 127]` for all nine envelopes and
`scaling_b` is `[0, 0, 0]`, i.e. a flat, inactive scaling curve.

The bytes immediately after `scaling_b` are **not** envelope data.  They are the
next parameter section of the `FM8E` payload.  In the 1.7.14 reference chunk
`tmp/base.bin`, the envelope section is 577 bytes (`1561..2138` payload-
relative) and the next mapped schema field is parameter 178 at offset 2212.

### 6.1 Effect on parameter offsets (the `+49` mystery)

The envelope section is variable length and sits in the middle of the payload.
Every `data/param_schema.json` field **after** it shifts by the difference in
envelope size.  The reference chunk used to measure the schema has a 56-byte
header and a 577-byte envelope section:

```
shift_before = header_length - 56
shift_after  = (header_length - 56) + (envelope_size - 577)
```

For `Whitty_Lead_` (1.7.8):

```
header_length = 81                    -> +25
envelope_size  = 375 + 113 + 113 = 601 -> +24 extra
shift_after    = 25 + 24 = 49
```

That is exactly the empirical `+49` in the original handoff.  It applies only to
fields after the envelope section; fields before it (including the flag array)
shift by `+25`.  `fm8env.schema_offset(patch, offset)` implements both cases, and
`EnvelopeSection.parameter_shift` exposes the post-section value.

The envelope section size varies widely across the factory library (521 to 2725
bytes in the 1155-patch corpus), so a fixed schema offset cannot be correct for
every patch.  `fm8param`'s default `get`/`set` use the reference offsets;
pass `layout=True` to apply the correction:

```python
p = fm8param.Patch('Whitty_Lead_.nfm8')
p.get(178)               # reference layout (wrong for this patch)
p.get(178, layout=True)  # 0.34 - A->A BL, using the +49 shift
```

The standalone helper is `fm8env.schema_offset(patch, offset)`.

A quick sanity check over 30 randomly chosen factory patches: all 150 sampled
post-envelope fields (matrix/output-level parameters) decoded in `0..1` with
`layout=True`, versus 142 with the fixed reference offsets.  This is a
plausibility check, not a substitute for value-level validation.

## 7. X/Z sentinel times

The X and Z envelopes are often unused.  Their **time arrays can then contain sentinel or
uninitialised-looking floats**, while their levels/slopes remain valid defaults.  This
is data, not a decoder bug: the values are present in the file and the tag
invariant still holds.

* 71 of 1155 factory patches contain at least one time value above
  `1_000_000 ms` (1000 s).
* 3 patches contain extreme values above `1e12` (up to about `2.4e15`),
  almost always in the X or Z time record.

`fm8env.Envelope.has_sentinel` is true when any stored or cumulative time is
non-finite, negative, or above `SENTINEL_MS` (1e6 ms).  Do **not** present
cumulative times of such an envelope as musical time; show the raw values and
the sentinel warning instead.

## 8. Version differences

The grammar was checked on 1155 factory patches spanning FM8 1.7.4
(`FM7 Legacy`, `Transient Attacks`, `Effects`) and newer files, plus the 1.7.8
`Whitty_Lead_` and the 1.7.14 default chunk:

| version | header length | envelope anchor | grammar |
|---|---|---|---|
| 1.7.4 | 34–140 bytes (varies with metadata) | `header_end + 1505` | identical |
| 1.7.8 | 81 bytes (`Whitty_Lead_`) | `header_end + 1505` | identical |
| 1.7.14 | 56 bytes (`base.bin`) | `header_end + 1505` | identical |

The 1.7.4 files include envelopes with up to 31 breakpoints and very long
segments; the newer files in this corpus are usually simpler.  The time unit is
milliseconds in both.

## 9. Worked example: `Whitty_Lead_`

Clean file `tmp/Whitty_Lead_.nfm8` (1.7.8), decoded by `fm8env`:

| dest | BP | AbsT | RelT | Level | Slope |
|---|---:|---:|---:|---:|---:|
| A | 1 | 0.001 | 0.001 | 1.000 | 0.247 |
| A | 2 | 0.114 | 0.113 | 0.649 | 0.528 |
| A | 3 | 0.214 | 0.100 | 0.649 | 0.500 |
| A | 4 | 0.254 | 0.040 | 0.000 | 0.917 |
| B | 1 | 0.001 | 0.001 | 1.000 | 0.247 |
| B | 2 | 0.101 | 0.100 | 1.000 | 0.500 |
| B | 3 | 0.141 | 0.040 | 0.000 | 0.917 |
| C | — | identical to B | | | |
| D | — | identical to B | | | |
| E | 1 | 0.001 | 0.001 | 1.000 | **0.227** |
| E | 2 | 0.101 | 0.100 | 1.000 | 0.500 |
| E | 3 | 0.141 | 0.040 | 0.000 | 0.917 |
| F | 1 | 0.001 | 0.001 | 1.000 | 0.247 |
| F | 2 | 0.101 | 0.100 | 1.000 | 0.500 |
| F | 3 | **0.105** | **0.004** | 0.000 | 0.917 |
| X | 1–3 | 0.001 / 0.501 / 0.502 | 0.001 / 0.500 / 0.001 | 1 / 1 / 0 | 0.5 / 0.9 / 0.9 |
| Z | 1–3 | same as X | | | |
| Pitch | 1 | 0.001 | 0.001 | 0.000 | 0.500 |
| Pitch | 2 | 0.002 | 0.001 | 0.000 | 0.500 |
| Pitch | 3 | 0.102 | 0.100 | 0.000 | 0.500 |
| Pitch | 4 | 0.103 | 0.001 | 0.000 | 0.500 |

All 27 flags are `1` in this patch.

Raw time arrays:

```
A      [1, 113, 100, 40]     levels [1, 0.649, 0.649, 0]   slopes [0.247, 0.528, 0.5, 0.917]
B/C/D  [1, 100, 40]          levels [1, 1, 0]              slopes [0.247, 0.5, 0.917]
E      [1, 100, 40]          levels [1, 1, 0]              slopes [0.227, 0.5, 0.917]
F      [1, 100, 4]           levels [1, 1, 0]              slopes [0.247, 0.5, 0.917]
X/Z    [1, 500, 1]           levels [1, 1, 0]              slopes [0.5, 0.9, 0.9]
Pitch  [1, 1, 100, 1]        levels [0, 0, 0, 0]           slopes [0.5, 0.5, 0.5, 0.5]
```

### Corrections to the earlier handoff

The handoff values for A BP2 were taken after an exploratory click that dragged
the point.  The clean stored values are:

| field | handoff (clicked) | clean file |
|---|---:|---:|
| AbsT | 0.113 | **0.114** |
| RelT | 0.112 | **0.113** |
| Level | 0.653 | **0.649** |
| Slope | 0.528 | 0.528 |

The GUI screenshots confirm A BP3 `0.214 / 0.100 / 0.649 / 0.500`, B BP1
`0.001 / 1.000 / 0.247`, B BP3 `0.141 / 0.040 / 0.000 / 0.917`, and F's
separate 4 ms release.  C is identical to B/D/E in this patch; E differs only in
its BP1 slope (`0.227`).

## 10. Validation

`python tools/check_envelopes.py` performs two checks:

1. **GUI oracle** on `tmp/Whitty_Lead_.nfm8`: A, B, D, F and Pitch values plus
   all flags.
2. **Corpus** over the installed factory library
   (`C:\Program Files (x86)\Common Files\Native Instruments\FM8\Sounds`).

Current result:

```
GUI oracle: OK (A/B/D/F/Pitch and flags)
corpus: 1155 files
  breakpoint invariants : 1155/1155
  no sentinel times      : 1084/1155
  flag arrays 0/1/128/255: 1155/1155
writer oracle: OK (A times/level/slope and F flag survive FM8)
resize structure: OK (grow/shrink offsets; FM8 acceptance is separate)
```

The 71 files with sentinel times are expected (unused X/Z envelopes with sentinel/uninitialised-looking time data).  The flag
offsets were additionally confirmed one at a time with the VST oracle.

## 11. Writing envelopes

### Safe in-place writer (validated)

`fm8env.write_envelopes(patch, {destination: values})` rewrites the time,
level and slope values without changing any breakpoint count, so the payload
size is unchanged and every following parameter offset stays valid.  Flags are
written at the same time.

```python
import fm8param, fm8env

p = fm8param.Patch('patch.nfm8')
envs = p.envelopes()
envs['A'].time_values_ms[0] = 3.0       # BP1 absolute time, milliseconds
envs['A'].levels[1] = 0.8               # BP2 level
envs['A'].slopes[2] = 0.65              # BP3 slope
envs['F'].flags['pitch_env'] = False    # P.Env toggle

fm8env.write_envelopes(p, {'A': envs['A'], 'F': envs['F']})
p.write('edited.nfm8')
```

Accepted input for each destination:

* an `fm8env.Envelope` from `p.envelopes()`;
* a dict with `time_values_ms`, `levels`, `slopes`;
* the `raw` dict produced by `--json`;
* a dict with `breakpoints` in GUI units (`abs_time_s`, `rel_time_s`,
  `level`, `slope`), as produced by `Envelope.to_dict()`.

The same-size path is validated through FM8 itself:

```
writer oracle: OK (A times/level/slope and F flag survive FM8)
resize structure: OK (grow/shrink offsets; FM8 acceptance is separate)
```

The test edits `tmp/base.bin` (a real 1.7.14 chunk), writes it, loads it via
`effSetChunk`, and requires FM8's returned state to contain the edited values.
`python tools/check_envelopes.py` runs it when `FM8.dll` and `vsthost.exe` are
available.

JSON apply workflow:

```bash
python fm8env.py patch.nfm8 --json > env.json
# edit env.json (raw arrays or breakpoints)
python fm8env.py patch.nfm8 --apply env.json --output edited.nfm8
```

### Resizing breakpoint counts (experimental, not accepted by FM8 in tests)

`fm8env.write_envelopes(..., resize=True)` rebuilds the 27 breakpoint records,
keeps the two scaling runs, splices the new section, and fixes:

* the inner tree total-size word;
* the enclosing `hsin`/`DSIN` sizes;
* the payload length immediately before the `FM8E` block;
* the 1.7.14 `tsrp` chunk length;
* the FastLZ uncompressed length and the outer container lengths.

The resulting file round-trips through this decoder (the new breakpoint counts
and offsets are correct), but FM8 1.7.14's `effSetChunk` **reverted resized
envelopes to the default 3-point shape** for both grow and shrink tests.  The
missing constraint is not identified.  Foreign 1.7.4/1.7.8 program files also
fall back to the default state through `effSetChunk`, so a 1.7.14 file with a
non-default envelope could not be obtained to isolate it.  Do not rely on
resizing for files you intend to load into FM8; verify any resized file with the
real plugin and keep the same-size path for production edits.

## 12. API and CLI

```python
import fm8param, fm8env

p = fm8param.Patch('patch.nfm8')

envs = p.envelopes()                 # {dest: fm8env.Envelope}
a = envs['A']
for bp in a.breakpoints:             # 1-based, GUI units
    print(bp.index, bp.abs_time_s, bp.rel_time_s, bp.level, bp.slope)

print(envs['F'].flags)               # {'pitch_env': True, 'loop': True, 'release': True}
print(envs['Z'].has_sentinel)        # True for an unused Z envelope

section = p.envelope_section()       # also exposes scaling_a/b
print(section.scaling_a.values)      # raw post-slope records
print(section.parameter_shift)       # shift for later schema offsets

fm8env.write_envelopes(p, {'A': a})          # same-size values/flags
fm8env.write_flags(p, {'F': {'pitch_env': False}})
```

CLI:

```bash
python fm8env.py patch.nfm8                  # all nine envelopes
python fm8env.py patch.nfm8 -o A --raw       # one envelope, stored arrays too
python fm8env.py patch.nfm8 --json           # machine-readable
python fm8env.py patch.nfm8 --json > env.json
python fm8env.py patch.nfm8 --apply env.json --output edited.nfm8
python fm8env.py --schema                    # print data/envelope_schema.json
```

`data/envelope_schema.json` records the layout, flag table, validation counts
and the decoded `Whitty_Lead_` example.

## 13. Limitations / open items

* **Writing.**  Same-size edits (values and flags, no breakpoint-count change)
  are implemented and validated through FM8's `effSetChunk`.  Breakpoint-count
  resizing is implemented (`resize=True`) but experimental: FM8 1.7.14 reverted
  resized envelopes to the default shape in testing, so verify before using it.
* **Scaling semantics.**  The two nine-record runs after the slopes are exposed
  raw.  Their exact mapping to the GUI's `Key Scaling` / `Vel Scaling` curves is
  inferred, not proven.
* **X/Z sentinel data.**  Unused X/Z envelopes can contain sentinel or
  uninitialised-looking time floats.  The decoder flags them; it does not sanitise them.
* **Breakpoints are not VST parameters.**  Completing the VST parameter schema
  will not expose them, and they cannot be set with `effSetParameter`.
* **Future versions.**  The anchors are validated per file and the decoder falls
  back to a structural scan, but a future FM8 version could change the record
  grammar or the header-relative offset.
* **Filter/LFO envelopes.**  The nine destinations are the operator/filter/pitch
  envelopes shown on Expert → Env.  LFO shapes and other modulation curves are
  not decoded here.

## 14. Reproduce

```bash
# all nine envelopes of the test patch
python fm8env.py tmp/Whitty_Lead_.nfm8 --raw

# one envelope as JSON
python fm8env.py tmp/Whitty_Lead_.nfm8 -o F --json

# full validation (needs the installed FM8 factory library)
python tools/check_envelopes.py

# only the GUI oracle, no corpus scan
python tools/check_envelopes.py --no-corpus
```

The GUI screenshots used for the oracle are in `tmp/` (`printC.bmp`,
`printBP2.bmp`, `A_BP1.bmp`, `A_BP1b.bmp`, `Aclean.bmp`, `stateA.bmp`,
`print1.bmp`).
