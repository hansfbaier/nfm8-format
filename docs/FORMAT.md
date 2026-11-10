# FM8 `.nfm8` patch file format — reverse engineering notes

Target: Native Instruments **FM8** (files written by 1.7.8, plugin installed 1.7.14, x64).

## TL;DR

1. **`.nfm8` *is* the FM8 VST plugin state chunk.** It is byte-for-byte what
   `FM8.dll` returns from `effGetChunk` (opcode 23) and what it accepts through
   `effSetChunk` (opcode 24). Verified by loading every file in this folder into
   the real plugin.
   * plugin exposes **1094 automatable VST parameters** and **128 programs**.
2. The container is a small **tagged node tree** (`hsin` / `DSIN`) with
   length-prefixed UTF-16LE metadata (name, author, comment, colour, tags, …).
3. The actual synth settings live in one **raw byte blob** inside the
   `DSIN`-type-116 node. It is a *positionally stable* but **variable-length**
   encoding of little-endian `float32` values (a 2-byte token like `80 3d` is the
   upper half of `float32 0x3d800000` = `0.0625`). The framing is partially
   decoded (see §5); the value/parameter mapping is not.
4. Practical consequence: the reliable way to *read or write* synthesis
   parameters is to let `FM8.dll` do it (`vsthost/` in this folder). Metadata
   (name/author/…) *can* be edited offline and is verified to keep the patch
   bit-identical to FM8.

## 1. Verification method

Everything below was confirmed with the installed plugin rather than by guessing:

```
vsthost.exe "C:\Program Files\Native Instruments\VSTPlugins 64 bit\FM8.dll" setchunk patch.nfm8 out.bin
```

`setchunk` loads the file, `out.bin` is the plugin's own re-encoding. Round-trip
of an untouched `.nfm8` reproduces the same parameter stream (only the random
GUIDs differ), which proves the file really is the plugin state.

Second oracle: sweeping one parameter at a time and diffing the resulting chunks
(`vsthost ... sweep`, `probe`, `dispsweep`) — this is how the value encoding
below was established.

## 2. File header

| offset | type | value |
|--------|------|-------|
| +0x00  | u32  | total file size in bytes |
| +0x04  | u32  | 0 |
| +0x08  | u32  | 1 |
| +0x0c  | node | root node (`hsin`) |

All integers are **little-endian**. The file has no magic string at offset 0;
detect it by `hsin`/`DSIN` at offset 12 and by `data[0] == len(data)`.

## 3. Node format

The fourcc bytes are ASCII, but NI stores them as a little-endian u32, so they
read as `hsin` = `"nish"` and `DSIN` = `"NISD"` when byte-swapped.

### `hsin` — container node (36-byte header)

| offset | field |
|--------|-------|
| +0  | `'hsin'` |
| +4  | u32 `1` |
| +8  | u32 `0` |
| +12 | 16-byte GUID — **new random value on every save** |
| +28 | u32 payload length |
| +32 | u32 `0` |
| +36 | payload: child nodes and/or raw bytes |

### `DSIN` — typed value node (20-byte header)

| offset | field |
|--------|-------|
| +0  | `'DSIN'` |
| +4  | u32 type id |
| +8  | u32 payload length |
| +12 | u32 `c` |
| +16 | u32 `d` |
| +20 | payload |

Observed type ids: `1` object *(`d` = number of following fields)*, `101` module,
`106`, `108`, `115` list, `116` parameter holder, `118` root/variant,
`121` map. Most leaf nodes use payload length 1 containing a small index.

### Three sites that hold a regenerated 16-byte object id

FM8 rewrites these on every save, so they must be masked when comparing chunks:

| marker | id position | where |
|--------|-------------|-------|
| `hsin` | +12 | every container node |
| `hsin` then `40 07 40 00 10` | +9 | marker inside the parameter blob |
| `40 23 e0 03 53 10` | +6 | embedded sub-object in the parameter stream |

Caveat: the outermost `DSIN` type-101 node always declares `size = payload + 12`,
i.e. its declared end is 12 bytes **past** EOF, and the last 12 bytes of the file
really are a fixed trailer. Either NI counts the file trailer as payload, or the
field is a logical rather than a physical size; FM8 does not validate it
strictly, but `nfm8.py` updates it anyway so the container stays self-consistent.

### Example tree (`FM8Clap.nfm8`)

```
000000  u32 total size = 1869
000004  u32 0
000008  u32 1
00000c  hsin  size=150   GUID 378e790f-c0c3-c749-87a0-de23d25ff459
000030    DSIN 118 'v'   (variant)
000044      DSIN 106 'j'
000058        DSIN 1
0000ca    DSIN 101 'e'   (module: carries name/author/comment + properties)
0000de      hsin
000102        DSIN 101 'e'
000116          DSIN 106 'j'
00012a            DSIN 1
000173        DSIN 108 'l'   (metadata block)
000187          hsin
0001ab            DSIN 108 'l'
0001bf              DSIN 1        <- name / author / comment
000325            DSIN 121 'y'
000339              hsin
00035d                DSIN 121 'y'
000371                  DSIN 1
0003ad          hsin
0003d1            DSIN 116 't'   (synthesis parameters)
0003e5              DSIN 115 's'
0003f9                DSIN 1
00040e                  <parameter blob>
...
0074b                   4-byte marker 0d 62 65 85 + trailer
```

The whole tree ends with a 4-byte `0` and a small trailer
(`01 00 00 00 · 00 · 01 00 00 00 · 00 01 00 00 00 · 00 00 00 00 · 00`).

## 4. Metadata

### 4.1 Name / author / comment

Plain length-prefixed UTF-16LE strings, **length counted in characters**:

```
u32 7  "FM8Clap"     <- name
u32 7  "not set"     <- author
u32 0                <- comment (empty)
... 32 zero bytes ...
u32 1, u32 1, u32 1
u32 3  "FM8"         <- product tag
u32 0, u32 0
```

### 4.2 Property map

Directly after the product tag:

```
u32 count
count * { u32 key_len,   key_utf16le,
          u32 value_len, value_utf16le }
```

Keys are prefixed with `\@`. Observed keys:

| key | meaning / observed values |
|-----|----------------------------|
| `\@color` | browser colour index, `"0"` |
| `\@devicetypeflags` | `"1"`, `"0"` |
| `\@soundtype` | sound category id, `"0"`… |
| `\@tempo` | `"0"` |
| `\@verl` | file format version written by FM8, e.g. `"1.7.8"`, `"1.7.14"` |
| `\@verm` | "major" version, same value |
| `\@visib` | visibility flag, `"0"` / `"1"` |

Note: `NewSound.nfm8` (the init patch shipped with FM8) has **no property map**
(count field `2` instead), so the map is optional.

## 5. The synthesis parameter blob

Located in the `DSIN` type-116 ('t') subtree.  A type-116 node appears twice
(an outer holder whose payload is an `hsin`, and an inner leaf), so **do not**
locate the blob by tree walking — identify it by its header signature instead.
This signature matched **438/438** files in the reference library:

```
+0x00  u32   table_size      per patch; 3291..3771 in the library, 4029 in
                             chunks emitted by the installed 1.7.14 plugin
+0x04  u32   stream_len
+0x08  u8    2               codec / layout version
+0x09  3 bytes               == table_size & 0xFFFFFF  (echo of the first field)
+0x0c  7 bytes               60 00 00 01 20 05 03
+0x13  <stream>              stream_len bytes; begins with the ASCII marker
                             'hsin', then 32 bytes (40 07 40 00 10 …), then values
```

The stream is `[blob+0x13, blob+0x13+stream_len)` and is followed by a few
trailing bytes that `stream_len` does not count (exactly 2 bytes in 426 of the
438 files, 6–205 bytes in the rest).  It never runs past EOF.

The 14 bytes of structure (`3`-byte echo + `7`-byte constant + `hsin`) make this
practically unambiguous; `nfm8.py` matches on exactly that.

Open questions about this header (do not treat as settled):

* `table_size` is **per patch**, not a version constant.  23 distinct values
  occur across the 438 files (3291 … 3771, e.g. 3375 for most of the DX7
  conversions, 3423 for several 1.7.8 files), while the installed 1.7.14 plugin
  emitted `4029` for the states that were dumped.  It is presumably a size hint
  for a decoded parameter table, but the relation to the byte stream is not
  established (it is *not* `4 x 1094` and not the stream length).
* `stream_len` is the encoded length measured from `blob+0x13`.  Adding it to
  `blob+0x13` never exceeds EOF across the library, which makes the signature
  above safely checkable.
* File-format versions seen in `\@verl`: `1.7.3` (419 files), `1.7.8` (18),
  absent (the shipped init patch).

### 5.1 Value encoding — partial, framing unresolved

Values are `float32` little-endian, written with only their significant bytes
(the low-order zero bytes of the mantissa are dropped).  Confirmed at parameter
45, `SQ LowFrequency`, by sweeping it one step at a time with the oracle
(raw table: `data/p45-sweep.csv`):

| VST | engine | float32 (LE) | bytes visible for the value |
|-----|--------|--------------|-----------------------------|
| 0.0625 | 0.0625 | `00 00 80 3d` | `80 3d` |
| 0.2812 | 0.2812 | `00 00 90 3e` | `90 3e` |
| 0.5000 | 2.0    | `00 00 00 40` | `40` (or `00 40`) |
| 0.5312 | 2.125  | `00 00 08 40` | `08 40` |
| 0.7500 | 3.0    | `00 00 40 40` | `40 40` |
| 1.0000 | 4.0    | `00 00 80 40` | `80 40` |

Also established:

* Above the midpoint FM8 stores `4 x VST value` for this parameter, below it the
  raw value.  The blob therefore holds an *engine* value with a
  per-parameter mapping, not the GUI value.
* Untruncated 4-byte floats occur too: `cd cc cc 3e` = 0.4f, `9a 99 19 40` =
  2.4f, `00 00 48 42` = 50.0f.
* Changing one parameter does **not** produce a local diff.  A diff typically
  runs from that parameter's token to the end of the stream, then re-syncs.

**Open question: are tokens byte-aligned?**

Two candidate models, and the evidence is currently contradictory — do not build
on either yet:

* *Byte-aligned tokens, each = length byte + payload.*  Supporting this: for the
  pair (p45 = 0.0625, p45 = 0.28125) the two chunks agree for ~98 % of the
  remaining bits once shifted by exactly 8 bits, i.e. the token grew by exactly
  one byte.  Sequences like `40 47 · 40 03 · c0 00 · 40 33 · e0 27 4b · c0 3f`
  also parse as `tag + 0/1/2 byte payload` over long runs.
* *Bit-packed values with a variable bit-length header.*  Supporting this: the
  bytes immediately after the value alternate between `00 20 13 02 …` and
  `20 0c 03 3f …` depending on whether the mantissa byte is a multiple of `0x20`
  or only of `0x10` — which looks like a bit count, not a byte count.  And the
  pair (p45 = 0.4, p45 = 0.75) matches at **no** byte or bit shift (best 37 %
  mismatch over the tail).

A third possibility that explains both observations: byte-aligned tokens, but
changing one parameter also rewrites one or more *derived* tokens (a filter
frequency plausibly carries a precomputed coefficient), so two or three tokens
change and any single-shift model fits only some pairs.

**The experiment that settles it** (next step, see `docs/PLAN.md`): pick a
parameter whose token length is provably the only thing that differs — e.g. find
two values whose float mantissas differ only in the number of trailing zero
bytes — and check whether the two chunks differ by a whole number of bytes over
the entire remaining stream.  If yes, tokens are byte-aligned and a length byte
exists.  If no, the stream is bit-packed and `nfm8` needs a bit-level parser.

### 5.2 Measured coupling: one parameter perturbs the whole stream

Before writing an encoder it is worth knowing how tightly the stream is coupled.
Measurement: for each of the 1094 parameters, set it to 0.25 / 0.5 / 0.75 on a
clean baseline, dump the state, and diff the parameter stream (3261
measurements; raw summary `data/param_stream_effects.json`).

* **0 of 3261** changes produced a localized diff (defined as span <= 12 bytes).
* **3149 of 3261** diffs ran all the way to the end of the stream.
* median diff span: **602 bytes** in a stream of ~755 bytes.

Consequence: there is no "byte offset per parameter" table to be found.  Any
change moves the tail of the stream, so a reader must parse the stream
sequentially with an exact framing model and cannot skip or resynchronise.  One
wrong length anywhere invalidates everything after it.  This is why the framing
question in §5.1 is the gate for the whole project, and why a partial decoder
(one that guesses lengths) is not worth shipping.

### 5.2 What a DLL-free tool needs

A tool that reads and writes patches without `FM8.dll` must know, per parameter:

1. its **position** in the stream (serialisation order),
2. its **framing** (byte- or bit-packed, and how the next token starts),
3. its **value mapping** (VST 0..1 <-> engine value, plus quantisation for
   switches/selectors).

None of that is known yet.  The acceptance test for "fully mapped" is strict and
worth stating plainly: **re-encode a parameter vector and get a chunk that is
byte-identical to FM8's output** (GUIDs aside).  Until that passes, the format is
only partially decoded and `nfm8.py` can safely edit metadata but not synth
parameters.

### 5.3 Breakpoint envelopes (solved, separate from the parameter stream)

The operator/filter/pitch envelopes are **not** VST parameters and are not part
of the packed parameter fields above.  They are a separate, header-relative
section of the `FM8E` payload and are fully decoded; see
[`ENVELOPES.md`](ENVELOPES.md) for the full specification and validation.

Summary:

* `header_end` is the end of the four length-prefixed metadata strings.
* 27 flag bytes at `header_end + 1118` (`P.Env`, `Env Loop`, `Env Rel` for
  A–F, X, Z, Pitch).
* 27 breakpoint records at `header_end + 1505`; each record is
  `u8 nseg` + `nseg+1` little-endian `float32` values (full 4-byte floats, no
  compacting).
* Three runs of nine records: times, levels, slopes.  Destinations in order:
  `A, B, C, D, E, F, X, Z, Pitch`.
* Times are milliseconds: BP1 absolute, then per-segment deltas.  FM8 displays
  seconds.
* The same grammar and anchors work for the 1.7.4, 1.7.8 and 1.7.14 factory
  files tested (1155/1155).  X/Z envelopes can contain uninitialised sentinel
  time values when unused.

Decode with `python fm8env.py patch.nfm8` or
`fm8param.Patch(path).envelopes()`.

## 6. Behaviour worth knowing

* `FM8.dll` **re-encodes** the parameter blob when saving (`effGetChunk`), so a
  saved file is usually *not* byte-identical to the file that was loaded:
  * all 16-byte GUIDs are regenerated;
  * name/author/`\@verl`/`\@verm` are rewritten from the live plugin state
    (loading a file via `effSetChunk` does **not** import the embedded
    name/author — they come from the preset slot);
  * the parameter blob is rewritten in the current version's canonical form
    (1.7.14 output is ~55 bytes shorter than the 1.7.8-written originals).
* The loaded *sound* is nonetheless identical, which is how the writer below was
  validated: patch + edit + load gives a state differing from the original only
  in the GUIDs.
* Metadata is therefore useful for **tooling / browser / archival** (category,
  colour, author, comments) but is regenerated by FM8 itself.

## 7. Tools in this folder

### `nfm8.py` — pure-Python reader/writer (no plugin needed)

```
python nfm8.py info <file...>              # name, author, comment, property map
python nfm8.py nodes <file>                # dump the node tree with offsets
python nfm8.py blob <file> <out.bin>       # extract the raw parameter blob
python nfm8.py set-name   <file> <text>    # edit, with automatic size fix-ups
python nfm8.py set-author <file> <text>
python nfm8.py set-comment <file> <text>
python nfm8.py set-meta   <file> color 3   # any \@ key
```

Editing a string changes the file length, so **every enclosing length field must
be patched**: the file-size u32 at +0 and the payload length of each `hsin`/`DSIN`
ancestor. `nfm8.py` does this by locating all nodes whose payload contains the
edited offset.

Verified: after `set-name`/`set-author`/`set-meta` (file grew 1869 → 1905 bytes)
FM8 loads the patch and the resulting state is byte-identical to loading the
original, apart from regenerated GUIDs.

Tested on all **438** `.nfm8` files in this folder (top level, `ConvertedSysEx/`,
`DX7/`): all parse and all names/authors are recovered.

### `vsthost/` — minimal VST2 host used as an oracle

`vsthost/vsthost.c` (build: `gcc -O2 -std=c11 -o vsthost.exe vsthost.c -s`).

```
vsthost <FM8.dll> params [n]                 # list VST params + current values
vsthost <FM8.dll> chunk out.bin              # dump state chunk
vsthost <FM8.dll> setchunk in.nfm8 out.bin   # load patch, dump re-encoded state
vsthost <FM8.dll> param <i> <v> out.bin      # set one parameter
vsthost <FM8.dll> sweep <dir> <i> <v0> <v1> <steps> [fixIdx fixVal]
vsthost <FM8.dll> dispsweep <dir> <i> <v0> <v1> <steps>   # + human-readable display
vsthost <FM8.dll> probe <dir> <byteStart> <byteEnd>       # flip bits, re-read chunk
```

The plugin must be "run" (one 512-sample block) after `setParameter` before the
change shows up in the chunk — `sweep`/`dispsweep` do that automatically.

## 8. Suggested next steps

1. Build the **position → parameter** table automatically: for each of the 1094
   VST parameters, sweep values, record the first/last differing bit of the
   blob, then cluster parameters that share a token. `sweep` + `probe` already
   produce the raw material.
2. Fully decode the tag byte (`0x40`/`0xc0`/`0xe0`/`0x03`/`0x04` …) — most likely
   a length+type pair; `0x03`-family tokens appear to hold an extra low-order
   byte for values whose mantissa does not fit 2 bytes.
3. Optionally map the `\@soundtype` ids to FM8's browser categories (the DX7
   conversion folders are a good corpus: they all share `\@verl` `1.7.14`).
