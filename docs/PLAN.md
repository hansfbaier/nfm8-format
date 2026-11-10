# Plan: a fully self-contained FM8 patch tool

Goal: `nfm8` must read **and write** patches with every synth parameter mapped,
with **no FM8 installation required at runtime**.  `FM8.dll` is only ever used
as a development-time oracle.

## Acceptance test

A tool is "done" when, for an arbitrary parameter vector, it can produce a chunk
that FM8 accepts and that is **byte-identical to FM8's own encoding** (GUIDs
aside).  Everything else is a step towards that test.

```
# oracle check (development only)
vsthost.exe FM8.dll setchunk authored.nfm8 from_fm8.bin
python tools/compare_states.py authored_params.json from_fm8.bin   # must be 0 diffs
```

## What is already known

* the container, node tree, metadata and all length fields (`docs/FORMAT.md` §2–4);
* the parameter blob is located by a self-checking header (§5) and matched
  438/438 reference files;
* values are LE `float32`, bit-packed with a variable significant-bit count
  (§5.1) — the exact framing is still open;
* 1094 VST parameters with names; the parameter names are stable text, so the
  schema can be keyed by name rather than by index.

## Blocker (measured, not assumed)

Collection pass done: 1094 params x 3 values, 3261 usable measurements
(`data/param_stream_effects.json`).  Result: **every** single-parameter change
perturbs the rest of the stream — 0/3261 diffs were localized (<= 12 bytes),
3149/3261 ran to end-of-stream, median span 602 bytes of a ~755-byte stream.

So the schema cannot be recovered as "parameter -> byte offset".  It requires an
exact sequential framing model first.  That model is the blocker; everything in
the pipeline below is cheap once it exists.

Two ways to attack the framing, both with real costs:

* **A. Black-box, longer campaign.** Needs a solver over candidate grammars
  constrained by hundreds of baseline streams plus single-parameter pairs
  (a valid grammar must parse every stream exactly and change exactly one token
  per single-parameter edit).  Open-ended: hours to days, no guarantee, because
  the stream also contains forward references (see note below) which make simple
  greedy parsers invalid.
* **B. Static analysis of `FM8.exe` / `FM8.dll`.** Fast and definitive, but it is
  disassembly of a proprietary binary — the opposite of the black-box approach
  used so far, and a decision the project owner must make, not me.
  **Status: approved by the project owner and in progress** — see
  `analysis/NOTES.md`. Already recovered the complete `DSIN` tag vocabulary and
  the parameter name pool (5 strings per parameter, module-grouped).

Note that pushes towards (B) or at least complicates (A): the stream contains
reversed ASCII (`knab`, `tsrpE8MF`, `ctniL`, `siug`) next to normal ASCII
(`default`), and length-like fields that change when the stream grows
(`e0 4b` -> `e0 45` for a +6 byte change).  Fields that encode sizes of things
that come *later* mean the serializer writes forward references, i.e. it may be
a two-pass format where sizes are patched in.  A decoder has to model that.

## Open problems, hardest first

1. **Token framing.** Determine exactly how a value plus its bit-length header
   is packed, and where the next token begins.  Then a sequential parser works
   and every later step becomes easy.
2. **Ordering.** Which parameter sits at which stream position.  Not the same
   order as the VST parameter indices (parameter 26's token sits before 27's,
   but the effects block order differs from the index order).
3. **Value mapping.** Per-parameter `VST value -> engine value` (parameter 45
   showed `4x` above the midpoint), plus discrete quantisation for switches.
4. **Derived parameters.** Some VST parameters are macros (`Easy*`, morph
   amounts) whose change makes FM8 rewrite *other* stored values.  These have to
   be modelled, or marked read-only.

## Discovery pipeline

All steps use the oracle and produce machine-readable data that is committed, so
the work is resumable and reviewable.

```
tools/collect.py      # for each of 1094 params x N probe values: dump chunk -> data/
tools/align.py        # find, per parameter, the bit offsets that respond to it
tools/derive_schema.py# turn alignments into a candidate schema (order + framing + mapping)
tools/encode.py       # pure-python encoder; must reproduce FM8 bytes exactly
```

### Step 1 — collect

`vsthost.exe ... scan <dir> <value> <first> <last>` already resets to a baseline
chunk, sets one parameter, commits two audio blocks and dumps the state.  It must
be run several times per parameter (e.g. values 0.25, 0.5, 0.75) because a
parameter may be stored differently depending on magnitude.

Cost: 1094 params x 3 values x ~60 ms ≈ 3 minutes.  Cheap; do it early and
commit the summaries (not the chunks — they are large).

### Step 2 — align

For each parameter, diff its chunk against the baseline and record the first and
last differing **bit** (not byte).  Group parameters that share the same first
bit: those are candidates for the same token, and the group ordering gives the
stream order.  Parameters whose diff covers the whole tail are derived/macro
parameters — flag them separately.

### Step 3 — derive framing

With the ordering known, walk the stream token by token and fit the bit-length
header.  Sanity constraint that makes this tractable: adjacent parameters that
did not change must re-encode to the *same bits*.  So for a given parameter, the
bits after its token must equal the baseline bits after the corresponding token
— that pins the token's bit length exactly.  Solve for the length as a function
of the value, over the sweep, and you have the framing rule.

### Step 4 — validate

Encode the baseline, then every sweep state, and compare byte-for-byte with the
oracle output.  Any mismatch localises the next unknown.

## Practical notes

* `effSetParameter` only reaches the state chunk after the plugin has actually
  rendered audio **while active** (`commitParams()` in `vsthost.c`); skipping
  this silently shifts every measurement by one step.  This bit me once.
* FM8 regenerates GUIDs and rewrites `\@verl`/`\@verm`/name/author on save, so
  comparisons must mask GUID slots (see `tools/fm8_oracle.py`).
* Chunks saved by 1.7.3, 1.7.8 and 1.7.14 differ in *length* even for identical
  sounds, so the encoder should target one version (1.7.14) and the decoder
  accept all.
* Keep `data/` text-only and small; never commit NI binaries or factory presets.
