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

Scanning `write_Instance` yields only four indices (`3, 6, 12, 11`); the bulk of
the data is written by a loop over a child container at `[this+0x5568]`. So the
ordering has to be harvested per writer function, not from one list:

* `write_Instance`  -> `3, 6, 12, 11`, then loops over the child container
* `FM8Program::write` -> `'FM8E'`, `0xcf`, `0x23bc`, 4 strings, then a
  straight 138-float run from `this+0x50` (stride 4, skipping indices 11, 12),
  then further blocks at other offsets

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


---

# Container layer, recovered from code (macOS 1.4.6 symbols)

The `hsin`/`DSIN` fourccs are the `NI::SOUND::Item` tree. Writers give exact
field order, so the container no longer needs guessing.

## Item header (writer: `NI::SOUND::ItemHeaderWriter`, 0x2b2be0)

```
u64  size            ; placeholder 0, patched later by StreamSizePatcher
u32  1
u32  'hsin'          ; 0x6e697368
u32  1
u32  0
u8[16] ItemUuid      ; new random uuid per save (ni::commons::fastRandomUuid)
```
`ItemFactory::createItem(1, 'DSIN')` allocates the root and calls
`fastRandomUuid`, which is why every save regenerates GUIDs.

## Item node (writer: `NI::SOUND::Item::writeItem`, 0x2b4040)

```
u64  size            ; patched afterwards
u32  'DSIN'          ; 0x4e495344, and Item::getDomainID returns exactly this
u32  itemID          ; one ASCII char: 1, 'e'(101), 'j'(106), 'l'(108),
                     ;                  's'(115), 't'(116), 'v'(118), 'y'(121)
```
`AppSpecific::writeItem` (0x2c81a0) is the model for a leaf:

```
u64 0
u32 'DSIN'
u32 0x75                 ; itemID 117
u32 1
    SubtreeItem::writeItem(...)          ; children
u32 1
u32 [this+0x98]
wstring(toString(VersionNumber at this+0x9c))   ; the @verl value
```

The `\@verl 1.7.x` metadata is therefore a **wide string written by the item
itself**, which is why the chunk length changes between FM8 versions.

## The `[u32][u32][u8]` header is real, and it is a size pair

Measured on a live chunk (`effGetChunk`), last `hsin` at 0x421:

| variant | file len | field A | field B | field C |
|---|---|---|---|---|
| default patch | 1812 | 4029 | 753 | 2 |
| param 0 = 0.25 | 1818 | 4029 | 759 | 2 |
| param 0 = 0.5 | 1812 | 4029 | 753 | 2 |
| param 0 = 1.0 | 1816 | 4029 | 757 | 2 |
| 6 factory patches | 1811-1812 | 4029 | ~753 | 2 |

`A = 4029` is constant across patches, `B` tracks the encoded payload length
(`len(file) - 0x421 - 2`), `C = 2` is a format version. So the preset body is a
**default snapshot plus a sparse override list**: A is the size of the full
default program, B the size of the overrides actually stored.

Field B grows by 6 when param 0 goes to 0.25 and by 4 at 1.0, so single
parameters add whole records; the encoding is variable-length.

## What it is *not*

* Not zlib. `NI::GP::ZLibStorage` exists, but scanning every offset 0x300-0x700
  of a real chunk with wbits -15/15/31 yields nothing > 200 bytes.
* Not `NI::GP::CompressedStorage` either: `setCompressionMethod` has exactly two
  callers, both in `NI::SND::Instrument::doExport/doImportProgramList`, and
  `CompressedStream::initStorage` has one caller in
  `CompressedMemoryStream::init`, which nothing calls. The preset path never
  builds a compressed stream.
* Not `FM8Stream` (0x86470): that reader wants a 5-byte magic `3a 01 01 00 00`
  or `3a 02 02 00 00`, which no `.nfm8` contains. It is the FM7/SysEx reader.
* Not `FM8Program::write` output: that one starts `'FM8E'`, `0xcf`, `0x23bc`
  and emits plain 4-byte floats, and no `'FM8E'` appears in a `.nfm8` chunk.

## Byte-level diff caveat

Diffs between two saves are dominated by 16-byte UUID regions, not by parameter
data. Every `40 23 e0 03 53 10`-style marker is followed by a fresh uuid, so the
markers are item boundaries, not value prefixes. Any diff-based analysis has to
mask these first — the earlier "first-diff offset" table is therefore UUID noise.

## Remaining unknown

The override record encoding inside the last `hsin` (B bytes). Everything else in
the file is now accounted for. Next: walk that block as the sparse table it must
be (param index + packed value), using the default snapshot as the base.


---

# Live tracing with gdb (this is the way in)

`gdb` from Strawberry Perl debugs the mingw-built host fine, and `vsthost` now
exports `void *g_fm8_base` so a script can compute plugin addresses at runtime:

```
break vsthost.c:210              # after LoadLibraryA, before the command runs
run "<FM8.dll>" chunk tmp/g.bin
break *((char*)g_fm8_base + RVA) # RVA from the PE section table
continue
```

PE mapping used throughout: `.text` VA 0x1000, raw 0x400, so
`RVA = file_offset + 0xc00` for code.

## Verified: the plaintext the preset writer emits

`FM8Program::write` is at **RVA 0xeaa86** (file 0xe9e86). It is the function
that an `effGetChunk` call reaches. Its body, straight from the disassembly:

```
writeU32('FM8E')                 ; RVA 0x8a92b0
writeU32(0xcf)
writeU32(0x23bc)
writeString(this+0x2900)         ; RVA 0x8a9150
writeString(this+0x2920)
writeString(this+0x2940)
writeString(this+0x2960)
for i in 0..0x8a:  if (i-0xb) > 1: writeFloat(this + i*4 + 0x68)
for i in 0..4: for j in 0..0x13:   writeFloat(this + (i*4+j)*4 + 0x290)
...
```

Breaking after each call and dumping the stream's storage buffer
(`[stream+8] -> storage`, buffer at `storage+0x20`, capacity at `storage+0x28`)
gives the exact byte sequence:

```
45 38 4d 46  'FM8E' as a little-endian u32
cf 00 00 00  0xcf
bc 23 00 00  0x23bc
07 00 00 00  'default'          <- length-prefixed ASCII, u32 length
07 00 00 00  'default'
07 00 00 00  'default'
07 00 00 00  'default'
00 00 00 3f  0.5f  as BIG-endian
00 00 80 3f  1.0f
00 00 a0 41  20.0f
```

Two facts worth pinning down, both measured:

* **u32 fields are little-endian** (`'FM8E'` -> `45 38 4d 46`).
* **f32 fields are big-endian** (`0.5f` -> `00 00 00 3f`, `20.0f` -> `00 00 a0 41`).
  `Stream::writeF32` byte-swaps whenever the stream's byte-order flag is not 1,
  and this stream has it set; `writeU32` in this build does not swap.

The plaintext for one program is **3172 bytes**, buffer capacity 0x2000.

## The file is the compressed form of that plaintext

Same patch, plaintext vs file, aligned at the `prst` chunk:

```
plain  cf 00 00 00 | bc 23 00 00 | 07 00 00 00 | 'default' | 07 ...
file   cf 20 0b 04 | bc 23 00 00 | 07 20 07 06 | 'default' | e0 18 0a ...
```

So the container really does wrap a codec, and the codec is the only thing left.

## Where the codec is applied

Caller of `FM8Program::write` (return address RVA **0x2e4c4e**, function at file
0x2e3f60) shows the envelope:

```
if mode == 0:  writeChunkOfType('prst', stream)          ; vtable +0x128
if mode == 1:
    enumerateChunkTypes(&vec)                            ; vtable +0x120
    writeU32(stream, vec.size())
    for id in vec:
        CompressedStream::initStorage(name, 0x2000, storage)   ; RVA 0x89c9f0
        writeChunkOfType(id, &compressedStream)                 ; method = 3
        writeU32(stream, id)
        writeRaw(stream, compressedBytes, size)
```

`writeChunkOfType` is at **RVA 0xf8b70** (file 0xf7f70); its dispatch compares
the id against `'edit'`, `'guis'`, `'intc'`, `'prst'` (as immediates 0x65646974,
0x67756973, 0x696e7463, 0x70727374).

So: `CompressedStream` with **method 3** produces the 5:1 reduction. The Mac
symbols name the pieces (`NI::GP::CompressedMemoryStream::init`,
`CompressedStorage::setCompressionMethod`, `beginBlock`, `finishBlock`), and
`ZLibStorage` is *not* involved — no zlib call site is reachable from here.

## Next step (short and concrete)

Break on `CompressedStream::write` / `finishBlock` while the host is in this
path, and single-step the encoder with the 3172-byte plaintext as a known input.
Because the plaintext is now fully known, the codec can also be derived offline
by aligning the two byte streams — either way the remaining unknown is a single
function.


---

# The codec pair, captured live

Tracing `effSetChunk` with gdb gives the decompressed image of the chunk, which
is the missing half of the codec:

```
break *(g_fm8_base + 0xf8c40)      # readChunkOfType
# rdx = stream, [rdx+8] = storage, [storage+0x20] = buffer, [storage+0x28] = len
```

Decompressed block, 3825 bytes, for the default patch:

```
05 00 00 00           version
63 0c 00 00           3171 = the FM8Program::write payload length
00 00 00 00
74 73 72 70           'tsrp'  (reversed 'prst')
45 38 4d 46           'E8MF'  (reversed 'FM8E')
cf 00 00 00  bc 23 00 00  07 00 00 00  'default' ...
```

The 3171 byte count matches the `writeRaw` size measured in the writer path
exactly, so the decompressed image is the chunk container itself:

```
u32 version, u32 payloadLen, u32 0, 'tsrp', 'E8MF', <FM8Program::write payload>
```

The compressed form in the same file is `file[0x417 : 0x417+753]`; the header
just before it is `[4029][753][2]`, so `4029` is the uncompressed capacity and
`753` the stored length.

## Token observations from the alignment (753 -> 3825)

Using difflib on the two byte strings, the encoder emits literals verbatim and
intersperses tokens. Measured token -> expansion examples:

| token bytes in the compressed stream | expansion |
|---|---|
| `20 0b 04` | 3 zero bytes |
| `20 07 06` | 3 zero bytes |
| `20 0f` | 2 bytes |
| `e0 18 0a 20 2a` | 35 bytes |
| `e0 02 03 03` | 11 bytes |
| `20 07 e0 0b` | 21 bytes |

Ruled out by direct test against this pair: byte-aligned LZSS with an 8-bit flag
byte (all bit orders, 8/12/13-bit offsets, all biases, three start offsets),
and LZ4 block format (invalid at every start phase 0-7). The literal ASCII runs
(`default`, `cd cc cc 3e`, `3f 9a 99 19`) survive byte-for-byte, so literals are
byte-aligned; the mismatched token lengths mean the tokens themselves are
bit-packed.

Reproduce with:

```
tools/fm8_oracle.py  ->  gdb script: break at base+0xf8c40, dump [storage+0x20]
```

## Next

Set a hardware watchpoint on the decompression buffer found at `readChunkOfType`
and let the decoder write it, which drops the PC straight into the decoder; the
pair above then only has to confirm the reading.
