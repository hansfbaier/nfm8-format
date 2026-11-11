# FM8 SVG visualizer

`fm8svg.py` renders a `.nfm8` patch as a self-contained SVG datasheet.  It uses
only the standard library and the repository's own decoders; FM8 does not need
to be installed.

```bash
python fm8svg.py patch.nfm8 -o patch.svg
python fm8svg.py patch.nfm8 --title "My patch"
```

## What the sheet contains

| Area | What is shown | Why it matters |
|---|---|---|
| Header | name, author, comment, version, file, route/point counts | identifies the patch and how much structure it has |
| Operators | waveform, ratio, offset, output, P/S/R/TS flags | the raw oscillator settings that the matrix modulates |
| FM matrix | FM8-style colored diagonal labels (A–F, X, Z, IN), a self-modulation cell above each label, 9×9 cells, rows = destinations, columns = sources, one BL value per cell, direction arrows, hover for all TL/TR/BL/BR values, OUT row, optional PAN strip | the actual modulation topology, including output and pan |
| Envelopes | all nine destinations, absolute time, levels, slopes, flags, K/V amounts | timing and shape of every operator/filter/pitch envelope |
| Key scaling | pitch, level and slope curves per destination | how the envelope changes across the keyboard |
| Voice | filter, noise, saturation, master/input/transpose, unison, portamento | the post-matrix timbre and play mode |
| LFO / ARP | both LFOs, arpeggiator settings | movement and note pattern |
| Effects | effect on/off and mix/amount | the final processing chain |

## Why these extra panels are important

A matrix alone says *who modulates whom*, but not *how fast* or *how strong*
the modulation is.  An envelope panel alone says *how an envelope moves*, but
not *where it is routed*.  The datasheet puts the two next to each other and
adds the settings that are neither matrix nor envelope:

* **four values per matrix cell** — the FM8 matrix stores four values per
  source/destination pair, but its display shows the BL value.  The chart
  matches that view and keeps all four values in the hover text.
* **destination rows, source columns** — a source feeding a later operator is
  below the diagonal; a source feeding an earlier operator is above it.  This
  is the orientation FM8 uses.
* **self cells and arrows** — every operator label has a self-modulation cell
  above it, and each active route has an arrow pointing toward the destination
  operator, matching FM8's matrix cues.
* **OUT and PAN** — output level is a matrix destination, and the pan law is a
  separate bipolar destination.  OUT is drawn as FM8's bottom strip; PAN is
  shown as a slim extra strip only when the patch uses it.
* **absolute time and release marker** — the stored times are a first absolute
  time followed by deltas.  The last breakpoint is the sustain/release end
  point; without an absolute axis the envelope is ambiguous.
* **P/S/R/TS and K/V** — `P.Env`, `Env Loop` (`Sus`), `Env Rel`, tempo sync, key
  scaling and velocity scaling are separate fields; they are not encoded in the
  curve itself.
* **key scaling** — this is a second curve set, not part of the breakpoint
  records.  It is what makes an envelope key-dependent.
* **voice, LFO, ARP, effects** — filter mode/cutoff/resonance, saturation,
  unison/portamento, LFO shapes, arpeggiator and effect mix can dominate the
  sound even when the operator matrix is unchanged.

## Accuracy and limitations

* The matrix uses FM8's diagonal-label layout rather than row/column headers.
  Rows are destinations and columns are sources; off-diagonal cells show the BL
  value, diagonal cells carry the colored operator labels, and the small cell
  above each label is its self-modulation route.
* Matrix fields are decoded from the stored bytes: standard cells are shown as
  signed values, `OUT` as 0..100, and `PAN` as a bipolar value.
* The operator table shows the **BL** quadrant as the primary value.  An
  asterisk (`*`) means the four quadrants differ; the hover text lists all four.
* Envelope curve shape is an approximation derived from the stored slope.  The
  breakpoint times, levels, slopes and flags are exact and are available in the
  tooltip and through `fm8env.py`.
* Keyscale cells with out-of-range/sentinel data are marked `!` instead of
  drawing a misleading curve.
* Phaser, Flanger and Tremolo on/off fields are not in the measured parameter
  schema, so their effect rows show `?`.  Their mix/amount is still displayed.
* Some global parameters that are absent from the schema (for example Master
  Tune and Unison Voices) are omitted rather than guessed.

## Inspecting the SVG

The output is ordinary SVG 1.1 with embedded styles and gradients.  It can be
opened in a browser, Inkscape, Illustrator, or converted to PNG by any SVG
renderer.  Hover tooltips contain the exact matrix and breakpoint values.

## Validation

The renderer was exercised on all 1,155 `.nfm8` files in the FM8 factory
library; every output parsed as XML with no exceptions or malformed documents.
This covers the 1.7.4, 1.7.8 and 1.7.14 factory layouts.
