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

## macOS build: unlocked

With `FM8_146_Mac.dmg` available locally, `7z` unwraps DMG -> HFS+ -> `.pkg`
(Xar) -> `Payload` (gzip) -> `Payload~` (cpio) -> universal Mach-O:

```
mac/FM8.component/Contents/MacOS/FM8     40 MB, x86_64 + arm64
```

`analysis/macho.py` parses fat Mach-O, segments/sections, `LC_SYMTAB`, and can
disassemble by virtual address. The x86_64 slice has **57,577 symbols** (arm64:
56,111) — the binary is not stripped at all.

### The class model (from symbols)

```
FM8Program::write(NI::GP::Stream&)         0x06ccd0   serialiser
FM8Program::read (NI::GP::Stream&)         0x06e0a0   parser
FM8EditBuffer::write / read                0x06cc90 / 0x06dfd0
FM8::write_Instance / read_Instance        0x09cfb0 / 0x09d7e0
FM8::writeChunkOfType / readChunkOfType    0x09cee0 / 0x09d140
FM8Stream::readHeader / readSnapBlock      0x086470 / 0x0864f0
NI::GP::Stream::writeF32 / writeU32 / writeString
NI::GP::CompressedStorage                  setCompressionMethod, beginBlock,
                                           finishBlock, readHeader(DataHeaderV2)
NI::SND::DBSoundFile                       chunk reader/writer, uses the above
```

Per-module tag enums exist as distinct types, which is the parameter taxonomy:
`ParameterTags`, `ParameterTagsMorphed`, `OperatorTags`, `OperatorTagsMorphed`,
`EnvelopeTags`, `KeyScaleTags`, `MatrixTags`, `EasyEditTags`, `RealtimeTags`,
`eMicrotune`, plus `FM8ParameterInfo` and
`FM8ParameterIndexTransformation::TagDescription` (the tag <-> index mapping),
and `FM8Program::GetParameterProperties(long, FM8ParameterInfo&)`.

### Serialisation order is an explicit index list

`FM8::write_Instance` writes `'FM8E'` + `0x2711` + one byte, then repeatedly:

```
mov rdi,[this+0x5558]        ; parameter accessor object
mov rax,[rdi]                ; vtable
mov esi, <index>             ; e.g. 3, 6, 12, 11, ...
call [rax+0x60]              ; float getParameter(int)
call NI::GP::Stream::writeF32
```

So the stream is an ordered sequence of plain **4-byte LE floats**, and the
order is the literal list of `esi` immediates in that function — i.e. the
schema order is recoverable by scanning the immediates, not by guessing.

`FM8Program::write` additionally shows a 138-float run read straight from the
program object (`this+0x50`, stride 4, skipping indices 11 and 12), preceded by
`'FM8E'`, `0xcf`, `0x23bc` and four strings.

### Consequence for the framing question

`NI::GP::Stream::writeF32` writes a plain 4-byte float (byte-swapped only on a
big-endian host). Therefore the variable-length "tokens" seen at file level are
**not** a float encoding: the numeric payload is fixed-width, and the apparent
tags belong to the storage/compression layer (`NI::GP::CompressedStorage`, whose
own header magic is `-NI-` / `data`, and which exposes
`setCompressionMethod(eCompressionMethod)` plus per-block begin/finish).

zlib is the only general-purpose compressor linked, but the preset payload does
**not** decompress as zlib/raw-deflate/gzip at any offset near its header, and
the binary contains no LZ4/ZSTD/LZO. Also, the payload visibly contains
`hsin`/`DSIN` fourccs, which incompressible text would not survive. The working
hypothesis is therefore that `codec == 2` is a **stored/uncompressed** mode and
the `[u32][u32][u8]` prefix is a size + method header, with the *first* u32 not
being an uncompressed length.

### Next steps

1. Scan `FM8::write_Instance` (and `FM8Program::write`) for the full `mov esi,
   imm` list -> complete ordered schema.
2. Read `FM8ParameterInfo` / `GetParameterProperties` to get per-parameter
   min/max/default/scale, which gives the VST 0..1 <-> engine value mapping.
3. Disassemble `CompressedStorage::readHeader`/`attachCompressionStorage`
   (`DataHeaderV2`) to settle the `[size][size][codec]` prefix.

## Reproducing

```bash
pip install pefile capstone
python analysis/pe.py
```
