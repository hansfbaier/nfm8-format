# Static analysis notes

Goal: decode the parameter stream framing, which black-box probing could not
crack (see `docs/PLAN.md`, "Blocker").

Scope discipline: analysis only runs on your own installed copy of FM8; nothing
from the binary is redistributed. These notes record *findings* (offsets, type
ids, structure), not code listings.

## Tooling

* `analysis/pe.py` — small PE helper (pefile + capstone) with file-offset ← →
  RVA mapping, a disassembler and a pattern scanner.
  `pip install pefile capstone`
* Binutils 2.25 `objdump` cannot disassemble this PE; use the helper instead.
* `7z` (19.00) on this machine can read DMG / HFS+ / Mach-O, so a macOS build
  would be extractable if one is available locally.

Binary: `FM8.dll` (x64, PE32+, image base `0x180000000`, 20.5 MB, 9 sections).

## Findings

### 1. The `DSIN` type field is an ASCII tag

The writer emits nodes through one helper (RVA `0x40a180`), called as
`(fourcc, type)`. Scanning the instruction stream for `mov r8d, 'DSIN'`
immediates gives the complete tag vocabulary actually written by FM8:

| tag | dec | tag | dec |
|-----|-----|-----|-----|
| `1`   | 1   | `m` | 109 |
| `d`   | 100 | `p` | 112 |
| `e`   | 101 | `r` | 114 |
| `g`   | 103 | `t` | 116 |
| `h`   | 104 | `v` | 118 |
| `j`   | 106 | `y` | 121 |
| `l`   | 108 | `z` | 122 |

So `DSIN`/`hsin` are little-endian u32 fourccs, and the "type id" is a *character*
— the containers are typed by letter (`v` = variant, `y` = ?, `t` = parameter
tree, `l`, `s`, `x`, …). This matches the tags observed in real files.

`hsin` is not written with an immediate in the same way, so its writer takes the
fourcc differently — worth locating next.

### 2. Parameter name table in `.rdata`

Contiguous string pool of **five** NUL-terminated strings per parameter:

```
<long description> <display name> <VST parameter name> <short name> <abbrev>
```

Example (VST index 0/1):

```
"Easy Edit Effect Strength" "Easy Edit FX Strength" "EasyFX Strength" "EyFXStre" "EFXS"
"Easy Edit Timbre Brightness" "Easy Edit Brightness" "Easy Brightness" "EyBrigth" "EBri"
```

Two important consequences:

* The pool order is **not** VST index order — it is grouped by module
  (LFO 1/2, Unison, Pitchbend, Morph, Microtune, Overdrive, …, Easy). That
  grouping is a strong candidate for the **serialisation order** the parameter
  stream uses.
* Only 177 of the 1094 VST names appear verbatim in the pool. Names with a
  corner suffix (`"Effects Amnt BL"`, `"Op. A On/Off TL"`, `"A WaveformBR"`) are
  built at runtime from a base name plus `" BL"/" BR"/" TR"/" TL"`. Any name
  matching must model that templating.

### 3. Regenerated 16-byte ids

Three sites in a chunk carry a regenerated id (`docs/FORMAT.md` §3): after
`hsin`+12, after `hsin`+9, and after the marker `40 23 e0 03 53 10`+6.

### 4. Negative results that narrow the framing question

Worth recording because they rule out the model I had favoured:

* The node-header writer (`RVA 0x40a180`) has only **2 direct call sites**, both
  in one function, so most node writes go through other entry points or are
  inlined — a single-hook approach will not find the serializer.
* No vtable/pointer reference to that writer exists in the image (the only
  RVA32 hit is `.pdata` unwind data), so it is not a virtual method.
* **The candidate tag bytes are not byte-dispatched.** Scanning `.text` for
  `cmp al, imm8` / `cmp r8, imm8` with the observed tags (`0x40, 0xc0, 0xe0,
  0x03, 0x04, 0x0b`) finds 560 comparisons but **no 96-byte window containing
  three distinct tag comparisons** — i.e. there is no `switch` over these bytes.

That last point is evidence *against* "tag byte + payload" and *for* a
bit-packed encoding, where `0x40`/`0xc0`/`0xe0` are merely the top bits of
packed values. It should be settled before any encoder is written.

## Next steps (in order)

1. **Enumerate the writer API.** `0x40a180` is the node-header writer; find its
   siblings (the writer object's vtable) — one of them encodes a scalar with a
   variable byte count. That is the token framing.
2. **Confirm serialisation order** by matching the stream against the pool order
   (with corner templating) for a patch whose parameters are known.
3. **Locate the numeric schema** (min/max/default/scale per parameter). The name
   pool has no numbers interleaved, so the structs live elsewhere and reference
   the pool by index — search for a table of ~1094 records with small integers.

## Blocker: a macOS build would be much cheaper

NI's download host is not reachable from this machine (`download.native-
instruments.com` returns no response; the product page rate-limits), so the
macOS build could not be fetched. NI's macOS binaries are usually far less
stripped and ship full symbol tables, which would collapse steps 1–3 into
symbol-name reading. `7z` here can unwrap a `.dmg`.

## Reproducing

```bash
pip install pefile capstone
python analysis/pe.py
```
