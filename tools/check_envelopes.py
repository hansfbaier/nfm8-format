#!/usr/bin/env python3
"""Validate the FM8 envelope decoder.

Three independent checks:

1. **Corpus check** - decode every `.nfm8` in a directory and require the
   structural invariants to hold (breakpoint counts agree across the time,
   level and slope runs; flags are bytes).
2. **GUI-oracle check** - reproduce the values read from FM8's Expert -> Env
   page for `tmp/Whitty_Lead_.nfm8`, including the corrected clean-file values
   for operator A.
3. **Writer oracle** - edit a 1.7.14 chunk with the in-place writer, load it
   through `FM8.dll`, and require FM8 to return the edited values and flag.

Usage::

    python tools/check_envelopes.py
    python tools/check_envelopes.py --dir "/path/to/Sounds"
    python tools/check_envelopes.py --whitty tmp/Whitty_Lead_.nfm8
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fm8env  # noqa: E402
import fm8param  # noqa: E402

DEFAULT_CORPUS = (
    r"C:\Program Files (x86)\Common Files\Native Instruments\FM8\Sounds"
)

# Values read from the FM8 1.7.14 GUI on the clean file.  A BP2 was previously
# reported as 0.113 / 0.112 / 0.653 after an exploratory drag; the clean
# stored data is 114 ms / 113 ms delta / 0.649 / 0.528.
ORACLE = {
    "A": {
        "abs_ms": [1.0, 114.0, 214.0, 254.0],
        "rel_ms": [1.0, 113.0, 100.0, 40.0],
        "levels": [1.0, 0.649, 0.649, 0.0],
        "slopes": [0.247, 0.528, 0.5, 0.917],
    },
    "B": {
        "abs_ms": [1.0, 101.0, 141.0],
        "rel_ms": [1.0, 100.0, 40.0],
        "levels": [1.0, 1.0, 0.0],
        "slopes": [0.247, 0.5, 0.917],
    },
    "D": {
        "abs_ms": [1.0, 101.0, 141.0],
        "rel_ms": [1.0, 100.0, 40.0],
        "levels": [1.0, 1.0, 0.0],
        "slopes": [0.247, 0.5, 0.917],
    },
    "F": {
        # FM8's separate 4 ms release.
        "abs_ms": [1.0, 101.0, 105.0],
        "rel_ms": [1.0, 100.0, 4.0],
        "levels": [1.0, 1.0, 0.0],
        "slopes": [0.247, 0.5, 0.917],
    },
    "Pitch": {
        "abs_ms": [1.0, 2.0, 102.0, 103.0],
        "rel_ms": [1.0, 1.0, 100.0, 1.0],
        "levels": [0.0, 0.0, 0.0, 0.0],
        "slopes": [0.5, 0.5, 0.5, 0.5],
    },
}


def close(a, b, tol=1e-3):
    return abs(float(a) - float(b)) <= tol


def check_whitty(path: str, verbose: bool = True) -> int:
    patch = fm8param.Patch(path)
    section = fm8env.read_section(patch)
    errors = []
    for dest, expected in ORACLE.items():
        env = section.destination_envelopes[dest]
        got = {
            "abs_ms": env.abs_times_ms,
            "rel_ms": env.rel_times_ms,
            "levels": env.levels,
            "slopes": env.slopes,
        }
        for key, want in expected.items():
            have = got[key]
            if len(have) != len(want) or not all(close(a, b) for a, b in zip(have, want)):
                errors.append(
                    "%s %s: expected %s, got %s"
                    % (dest, key, [round(x, 4) for x in want],
                       [round(x, 4) for x in have])
                )
    # All 27 flags are 1 in this patch.
    for dest in fm8env.DESTINATIONS:
        env = section.destination_envelopes[dest]
        for group in fm8env.FLAG_GROUPS:
            if not env.flags[group]:
                errors.append("%s flag %s is not set" % (dest, group))

    # The variable envelope block shifts fields after it.  These are the
    # handoff's matrix/output-level spot checks, decoded with layout=True.
    for index, want in {45: 0.4, 178: 0.34, 190: 0.21, 374: 0.27,
                        696: 0.79, 712: 0.64, 1093: 0.5}.items():
        got = patch.get(index, layout=True)
        if not close(got, want, 0.006):
            errors.append("param %d layout=True: expected %.3f, got %.3f"
                          % (index, want, got))

    if verbose:
        if errors:
            print("GUI oracle: FAIL")
            for e in errors:
                print("  " + e)
        else:
            print("GUI oracle: OK (A/B/D/F/Pitch and flags)")
    return 1 if errors else 0


def check_writer_oracle(verbose: bool = True) -> int:
    """Round-trip an in-place edit through FM8 itself (1.7.14 template)."""
    dll = os.environ.get(
        "FM8_DLL", r"C:\Program Files\Native Instruments\VSTPlugins 64 bit\FM8.dll")
    vsthost = os.path.join(ROOT, "vsthost", "vsthost.exe")
    template = os.path.join(ROOT, "tmp", "base.bin")
    if not (os.path.exists(dll) and os.path.exists(vsthost)
            and os.path.exists(template)):
        if verbose:
            print("writer oracle: skipped (needs FM8.dll, vsthost.exe and tmp/base.bin)")
        return 0

    work = tempfile.mkdtemp(prefix="fm8-env-write-")
    edited = os.path.join(work, "edited.nfm8")
    state = os.path.join(work, "state.bin")
    errors = []
    try:
        patch = fm8param.Patch(template)
        envs = patch.envelopes()
        a = envs["A"]
        a.time_values_ms[0] = 3.0
        a.levels[1] = 0.8
        a.slopes[2] = 0.65
        envs["F"].flags["pitch_env"] = False
        fm8env.write_envelopes(patch, {"A": a, "F": envs["F"]})
        patch.write(edited)

        run = subprocess.run([vsthost, dll, "setchunk", edited, state],
                             capture_output=True, text=True)
        if run.returncode != 0 or not os.path.exists(state):
            errors.append("vsthost failed: %s%s" % (run.stdout, run.stderr))
        else:
            out = fm8param.Patch(state)
            got = out.envelopes()["A"]
            if not close(got.abs_times_ms[0], 3.0):
                errors.append("A BP1 time not preserved: %r" % got.abs_times_ms[0])
            if not close(got.levels[1], 0.8):
                errors.append("A BP2 level not preserved: %r" % got.levels[1])
            if not close(got.slopes[2], 0.65):
                errors.append("A BP3 slope not preserved: %r" % got.slopes[2])
            if out.envelopes()["F"].flags["pitch_env"]:
                errors.append("F P.Env flag not preserved")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    if verbose:
        if errors:
            print("writer oracle: FAIL")
            for e in errors:
                print("  " + e)
        else:
            print("writer oracle: OK (A times/level/slope and F flag survive FM8)")
    return 1 if errors else 0


def check_resize_structure(verbose: bool = True) -> int:
    """Check that the experimental resize path keeps the decoder consistent."""
    template = os.path.join(ROOT, "tmp", "base.bin")
    if not os.path.exists(template):
        if verbose:
            print("resize structure: skipped (missing tmp/base.bin)")
        return 0

    work = tempfile.mkdtemp(prefix="fm8-env-resize-")
    errors = []
    try:
        # grow A from 3 to 4 breakpoints
        patch = fm8param.Patch(template)
        a = patch.envelopes()["A"]
        grown = {
            "time_values_ms": list(a.time_values_ms) + [100.0],
            "levels": list(a.levels) + [0.0],
            "slopes": list(a.slopes) + [0.5],
        }
        fm8env.write_envelopes(patch, {"A": grown}, resize=True)
        grown_path = os.path.join(work, "grown.nfm8")
        patch.write(grown_path)
        out = fm8param.Patch(grown_path)
        got = out.envelopes()["A"]
        if len(got.breakpoints) != 4:
            errors.append("grow: expected 4 BPs, got %d" % len(got.breakpoints))
        if out.envelope_section().parameter_shift != 12:
            errors.append("grow: parameter_shift is %d, expected 12"
                          % out.envelope_section().parameter_shift)

        # shrink A from 3 to 2 breakpoints
        patch = fm8param.Patch(template)
        shrunk = {
            "time_values_ms": [1.0, 500.0],
            "levels": [1.0, 1.0],
            "slopes": [0.5, 0.5],
        }
        fm8env.write_envelopes(patch, {"A": shrunk}, resize=True)
        shrunk_path = os.path.join(work, "shrunk.nfm8")
        patch.write(shrunk_path)
        out = fm8param.Patch(shrunk_path)
        if len(out.envelopes()["A"].breakpoints) != 2:
            errors.append("shrink: expected 2 BPs, got %d"
                          % len(out.envelopes()["A"].breakpoints))
        if out.envelope_section().parameter_shift != -12:
            errors.append("shrink: parameter_shift is %d, expected -12"
                          % out.envelope_section().parameter_shift)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    if verbose:
        if errors:
            print("resize structure: FAIL")
            for e in errors:
                print("  " + e)
        else:
            print("resize structure: OK (grow/shrink offsets; FM8 acceptance is separate)")
    return 1 if errors else 0


def check_corpus(directory: str, verbose: bool = True) -> int:
    files = sorted(glob.glob(os.path.join(directory, "**", "*.nfm8"),
                             recursive=True))
    if not files:
        print("no .nfm8 files under %s" % directory)
        return 1
    ok = strict_ok = flags_ok = aligned = 0
    failed = []
    for path in files:
        try:
            patch = fm8param.Patch(path)
            section = fm8env.read_section(patch, strict=False)
            # structural check, independent of the locator's fast path
            run = fm8env._valid_breakpoint_run(
                patch.inner, fm8env.header_end(patch) + fm8env.BREAKPOINT_OFFSET)
            if run is None:
                raise ValueError("breakpoint invariants failed")
            ok += 1
            # The variable envelope section must end exactly where the
            # layout-corrected parameter 178 begins (reference offset 2212).
            if (section.end_offset - patch.payload
                    == fm8env.schema_offset(patch, 2212)):
                aligned += 1
            else:
                raise ValueError("envelope end does not align with param 178")
            if not any(env.has_sentinel for env in
                       section.destination_envelopes.values()):
                strict_ok += 1
            raw = fm8env.read_raw_flags(patch)
            if all(v in (0, 1, 128, 255) for d in raw.values() for v in d.values()):
                flags_ok += 1
        except Exception as exc:  # noqa: BLE001 - report the first failure
            failed.append((path, repr(exc)))
    if verbose:
        print("corpus: %d files" % len(files))
        print("  breakpoint invariants : %d/%d" % (ok, len(files)))
        print("  envelope end aligned  : %d/%d" % (aligned, len(files)))
        print("  no sentinel times      : %d/%d" % (strict_ok, len(files)))
        print("  flag arrays 0/1/128/255: %d/%d" % (flags_ok, len(files)))
        for path, exc in failed[:10]:
            print("  FAIL %s: %s" % (os.path.basename(path), exc))
    if failed or ok != len(files) or aligned != len(files) or flags_ok != len(files):
        return 1
    # The three files with sentinel X/Z times are expected, not a failure.
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=DEFAULT_CORPUS,
                    help="directory of .nfm8 files to scan")
    ap.add_argument("--whitty", default=os.path.join(ROOT, "tmp",
                                                     "Whitty_Lead_.nfm8"),
                    help="patch used for the GUI oracle check")
    ap.add_argument("--no-corpus", action="store_true")
    ap.add_argument("--no-writer-oracle", action="store_true")
    args = ap.parse_args(argv)

    rc = 0
    if os.path.exists(args.whitty):
        rc |= check_whitty(args.whitty)
    else:
        print("GUI oracle: skipped (missing %s)" % args.whitty)
    if not args.no_corpus:
        if os.path.isdir(args.dir):
            rc |= check_corpus(args.dir)
        else:
            print("corpus: skipped (missing %s)" % args.dir)
    if not args.no_writer_oracle:
        rc |= check_writer_oracle()
    rc |= check_resize_structure()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
