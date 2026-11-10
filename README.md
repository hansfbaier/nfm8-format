# FM8 `.nfm8` patch format

Reverse-engineered documentation and tooling for Native Instruments **FM8** patch
files (`.nfm8`), plus a tiny VST2 host that uses `FM8.dll` itself as a
verification oracle.

> **Project goal:** a tool that reads *and writes* every synth parameter with **no
> FM8 installed** — `FM8.dll` used only as a development oracle.
>
> **Status:** the format is decoded end to end and the tool writes parameters
> without the plugin. The container, the encryption item and the payload codec
> are all solved, and the codec is bidirectional: every FastLZ block in a library
> decompresses and re-compresses back to the identical stored bytes
> (`python tools/check_codec.py <dir>`, 1099/1099 streams so far).
> `fm8param.Patch` reads and writes parameters directly: a file written by this
> tool is accepted by FM8, and the parameter payload FM8 returns is byte-identical
> (only the 16-byte UUIDs FM8 regenerates on every save differ).
>
> Coverage: all 1030 locatable parameters have a measured offset and transfer
> function in [`data/param_schema.json`](data/param_schema.json); 976 of them
> reproduce an unseen value exactly. The remaining 44 are integer-valued floats
> where FM8's rounding and ours disagree by one step, plus 10 discrete fields with
> no closed form. See [docs/PLAN.md](docs/PLAN.md).  The offsets are relative to
> the 1.7.14 reference layout; pass `layout=True` to `Patch.get`/`set` for a
> patch whose variable envelope block shifts the later fields (see
> [docs/ENVELOPES.md](docs/ENVELOPES.md) §6.1).
>
> **Envelopes:** the operator/filter/pitch **breakpoint envelopes** are decoded
> separately from the VST parameter schema. `fm8param.Patch.envelopes()` and
> `fm8env.py` read the breakpoint times, levels and slopes for A–F, X, Z and
> Pitch, plus the `P.Env` / `Env Loop` / `Env Rel` flags. The layout is
> header-relative and the same grammar works across the 1.7.4, 1.7.8 and 1.7.14
> factory files tested (1155/1155). See [`docs/ENVELOPES.md`](docs/ENVELOPES.md)
> for the specification, worked example and validation; the former open handoff
> is now [`docs/ENVELOPES_HANDOFF.md`](docs/ENVELOPES_HANDOFF.md).

## Why this exists

`.nfm8` has no public specification. I wanted to script patch metadata (names,
authors, categories, colours) across a large sound library, and to understand the
file well enough to check that edits are safe. Everything here was derived by
observation; see [Method](#method).

## What is confirmed so far

* **`.nfm8` is the FM8 VST state chunk**, byte for byte what `FM8.dll` returns
  from `effGetChunk` and what it accepts through `effSetChunk`.
* The plugin exposes **1094 automatable parameters** and **128 programs**.
* The container is a tagged node tree (`hsin` / `DSIN`), no magic at offset 0;
  the first `u32` equals the file size.
* Metadata is length-prefixed UTF-16LE: name, author, comment, then a
  `\@`-prefixed property map (`color`, `soundtype`, `tempo`, `visib`, `verl`,
  `verm`, `devicetypeflags`).
* Editing metadata with correct length-field fix-ups keeps the patch
  **bit-identical** in FM8: loading the edited file and re-dumping the state
  differs from the original only in the 7 regenerated GUIDs.
* FM8 **re-encodes** on save: GUIDs are regenerated and name/author/version
  properties are rewritten from the live plugin state, so a save is never
  byte-identical to the load.
* The parameter blob is located by a self-checking header signature that matched
  **438/438** reference patches (`docs/FORMAT.md` §5).

## Quick start

No dependencies beyond Python 3.6+.

```bash
python nfm8.py info MyPatch.nfm8
python nfm8.py nodes MyPatch.nfm8
python nfm8.py blob MyPatch.nfm8 params.bin
python nfm8.py tokens MyPatch.nfm8 --limit 20
```

```
== MyPatch.nfm8 (1869 bytes)
   name    : 'FM8Clap'
   author  : 'not set'
   comment : ''
   params  : offset 0x40e, table_size 4029, stream_len 752, codec 2
   \@color           : '0'
   \@devicetypeflags : '1'
   \@soundtype       : '0'
   \@tempo           : '0'
   \@verl            : '1.7.8'
   \@verm            : '1.7.8'
   \@visib           : '0'
```

Edit metadata (length changes are handled, `-o` avoids in-place writes):

```bash
python nfm8.py set-author MyPatch.nfm8 "Your Name" -o tagged.nfm8
python nfm8.py set-name   MyPatch.nfm8 "Deep Clap" -o tagged.nfm8
python nfm8.py set-meta   MyPatch.nfm8 color 3     -o tagged.nfm8
python nfm8.py set-meta   MyPatch.nfm8 soundtype 2 -o tagged.nfm8
```

Read the breakpoint envelopes:

```bash
python fm8env.py MyPatch.nfm8              # all nine envelopes
python fm8env.py MyPatch.nfm8 -o A --raw   # one envelope, stored arrays
python fm8env.py MyPatch.nfm8 --json       # machine-readable
python tools/check_envelopes.py            # oracle + factory-library validation

# edit a JSON dump and write the values back (same breakpoint counts)
python fm8env.py MyPatch.nfm8 --json > env.json
python fm8env.py MyPatch.nfm8 --apply env.json --output edited.nfm8
```

```python
import fm8param

p = fm8param.Patch('MyPatch.nfm8')
for bp in p.envelopes()['A'].breakpoints:
    print(bp.index, bp.abs_time_s, bp.rel_time_s, bp.level, bp.slope)
print(p.env_flags()['F'])       # P.Env / Loop / Rel
print(p.get(178, layout=True))  # schema field after the variable envelope block
```

As a library:

```python
from nfm8 import Patch

p = Patch('MyPatch.nfm8')
print(p.summary()['name'], p.read_meta())
p.set_strings(name='Deep Clap', author='me')
p.set_meta('color', '3')
p.save('tagged.nfm8')
```

## The oracle: `vsthost`

`vsthost/` is a ~400 line VST2 host for Windows that loads `FM8.dll`, dumps /
loads state chunks and sweeps parameters. It needs an **installed, licensed**
FM8 — nothing of NI's is redistributed here.

## Licensing and scope

Everything in this repository is original work: the parser, the codec, the host
and the measurements. No Native Instruments source, no disassembly listings, no
factory presets and no binaries are included or redistributed.

* **FastLZ** is a public format by Ariya Hidayat (MIT). `analysis/fastlz.py` is an
  independent implementation written from the published format and from the byte
  streams FM8 emits, so the tool stays binary-free.
* **`data/params.txt`** lists the 1094 automatable parameter names the plugin
  reports. They are interface identifiers, the minimum needed to talk to a patch
  in a way a user can recognise, in the same sense as an API surface.
* **`data/param_schema.json`** and the other `data/` files are measurements taken
  on the author's own licensed copy — offsets and coefficients, not vendor data.
* You still need your own licensed FM8 to make the audio, and NI's licence
  applies to patches you create with it.

```bash
cd vsthost && make            # or: gcc -O2 -std=c11 -o vsthost.exe vsthost.c -s

./vsthost.exe "C:/Program Files/Native Instruments/VSTPlugins 64 bit/FM8.dll" params 30
./vsthost.exe ".../FM8.dll" chunk state.bin
./vsthost.exe ".../FM8.dll" setchunk patch.nfm8 reencoded.bin
./vsthost.exe ".../FM8.dll" sweep outdir 45 0 1 32
./vsthost.exe ".../FM8.dll" dispsweep outdir 45 0 1 16
./vsthost.exe ".../FM8.dll" probe outdir 0x40e 0x420
```

`tools/fm8_oracle.py` wraps the round-trip check used during development:

```bash
python tools/fm8_oracle.py original.nfm8 edited.nfm8
# -> loads both, compares the resulting plugin states, reports non-GUID diffs
```

A clean result is "differences only in 16-byte GUID slots", which is what the
software produces for a metadata-only edit.

## Method

This is **black-box interoperability analysis**, not a formal two-team clean-room
process.

* I loaded `FM8.dll` through its public VST2 entry point and called documented
  VST2 opcodes (`effGetChunk`, `effSetChunk`, `effGetParamName`,
  `effGetParamDisplay`, …). I did **not** disassemble, decompile, unpack or
  symbolically inspect any NI binary, and no NI code or data is reproduced here.
* File-structure conclusions come from differential analysis of `.nfm8` files and
  from plugin behaviour: set one parameter, render a block, dump the state, diff.
  That is how the `float32` value encoding in §5.1 was established, e.g.
  `SQ LowFrequency` at 0.75 storing `40 40`, the upper half of `float32 0x40400000`
  = 3.0.
* Hypotheses were only written down after they survived a round-trip through the
  real plugin. Where a conclusion is still a guess it is labelled as such.

Strictly speaking a clean room would need a second, independent implementation
written only from this document; that has not happened. If you need that
guarantee (e.g. for a commercial interoperability project), treat
[docs/FORMAT.md](docs/FORMAT.md) as the neutral specification and have an
independent party implement from it, without access to FM8 or to `vsthost`.

Nothing here is legal advice. FM8 and Native Instruments are trademarks of
Native Instruments GmbH; this project is unaffiliated with them. You need your
own FM8 licence to use the oracle tooling, and you should check the EULA that
applies to you before redistributing patches made with it.

## Repository layout

```
nfm8.py              reader/writer + CLI (stdlib only)
fm8param.py          VST-parameter reader/writer (no plugin at runtime)
fm8env.py            breakpoint-envelope decoder + CLI
docs/FORMAT.md        the format write-up, with evidence
docs/ENVELOPES.md     envelope layout, worked example and validation
data/envelope_schema.json  machine-readable envelope layout + flag table
vsthost/vsthost.c     minimal VST2 host used as an oracle
vsthost/Makefile
tools/fm8_oracle.py   round-trip verification helper
tools/check_envelopes.py  envelope decoder validation
```

## Contributing

The blocker is the **token framing** of the parameter stream, then the
stream-position -> parameter-id table. `docs/PLAN.md` has the ordered plan and
the acceptance test (re-encode a parameter vector, get bytes identical to FM8).
`sweep` / `probe` already emit the raw measurements the table needs.

Please do not open PRs containing NI binaries, factory presets, or code derived
from decompilation.

## Licence

MIT — see [LICENSE](LICENSE).
