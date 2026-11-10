#!/usr/bin/env python3
"""Decode the breakpoint envelopes stored in an FM8 `.nfm8` patch.

The parameter schema in :mod:`fm8param` covers the automatable VST parameters,
but FM8's breakpoint envelopes are *not* VST parameters.  They live in a
separate, header-relative section of the `FM8E` payload:

    payload + header_end + 1118   27 flag bytes (P.Env / Loop / Rel)
    payload + header_end + 1505   27 breakpoint records

`header_end` is the end of the four length-prefixed strings that follow the
`FM8E` / `0xcf` / `0x23bc` preamble.  The offsets are independent of the file
format version (1.7.4, 1.7.8 and 1.7.14 all use the same anchors); only the
header length changes.

A breakpoint record is::

    u8   nseg                 # number of segments = breakpoints - 1
    f32  value[0..nseg]       # nseg + 1 little-endian float32 values

The first 27 records form three parallel runs of nine envelopes each:

    records  0.. 8   time values (milliseconds)
    records  9..17   levels (0..1)
    records 18..26   slopes (0..1)

Envelope order in every run is::

    A, B, C, D, E, F, X, Z, Pitch

Times are `BP1 absolute time` followed by per-segment deltas:

    AbsT[0] = value[0]
    AbsT[i] = AbsT[i-1] + value[i]       (i >= 1)
    RelT[0] = value[0]
    RelT[i] = value[i]                    (i >= 1)

FM8 displays those values in seconds, i.e. milliseconds / 1000.

Both the old 1.7.4 factory files and the newer 1.7.8/1.7.14 files follow this
layout.  The X and Z envelopes are frequently unused; their time array can then
contain uninitialised/sentinel floats (values in the 1e7..1e15 ms range).  Such
envelopes are marked with :attr:`Envelope.has_sentinel`; do not treat their
cumulative times as musical time.

The envelope section is variable length and is embedded in the `FM8E` payload.
Every schema field after it shifts by the difference in envelope size; use
:func:`schema_offset` to map a `data/param_schema.json` offset onto a particular
patch (the famous `+49` for `Whitty_Lead_` is `header +25` plus `24` extra bytes
of breakpoint records).

Measurements only; no Native Instruments code or data is involved.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

DESTINATIONS = ("A", "B", "C", "D", "E", "F", "X", "Z", "Pitch")

# Header-relative offsets.  `header_end` is the end of the four strings.
BREAKPOINT_OFFSET = 1505
FLAG_OFFSET = 1118
FLAG_BYTES = 27

#: Stored time values above this are treated as sentinel/uninitialised.  A real
#: FM8 envelope can be long, but not 1000 seconds per segment.
SENTINEL_MS = 1_000_000.0

#: Maximum segment count observed across the 1155-patch factory library (30,
#: i.e. 31 breakpoints).  Kept generous so future patches do not fail.
MAX_SEGMENTS = 96

#: The parameter schema offsets in data/param_schema.json were measured on the
#: 1.7.14 default chunk (tmp/base.bin): 56-byte header and a 577-byte envelope
#: section.  A patch with a different header length or breakpoint counts shifts
#: every schema field after the envelope section; see schema_offset().
REFERENCE_HEADER_LENGTH = 56
REFERENCE_ENVELOPE_START = 1561          # 56 + BREAKPOINT_OFFSET
REFERENCE_ENVELOPE_SIZE = 577
REFERENCE_ENVELOPE_END = REFERENCE_ENVELOPE_START + REFERENCE_ENVELOPE_SIZE

#: The three flag groups, nine destinations each, in the order they are stored.
FLAG_GROUPS = ("pitch_env", "loop", "release")
FLAG_LABELS = {
    "pitch_env": "P.Env",
    "loop": "Env Loop",
    "release": "Env Rel",
}


# ---------------------------------------------------------------------------
# payload navigation
# ---------------------------------------------------------------------------

def header_end(patch: Any) -> int:
    """Return the first payload byte after the four metadata strings.

    `patch` is anything with `payload` and `inner` attributes, normally
    :class:`fm8param.Patch`.
    """
    buf = patch.inner
    pos = patch.payload + 12  # 'FM8E' + 0xcf + 0x23bc
    for _ in range(4):
        if pos + 4 > len(buf):
            raise ValueError("truncated FM8E header")
        n = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        if n > 4096 or pos + n > len(buf):
            raise ValueError("implausible FM8E metadata string length %d" % n)
        pos += n
    return pos


def _read_record(buf: bytes, pos: int) -> Optional[tuple]:
    if pos >= len(buf):
        return None
    n = buf[pos]
    if n > MAX_SEGMENTS or pos + 1 + 4 * (n + 1) > len(buf):
        return None
    values = list(struct.unpack_from("<%df" % (n + 1), buf, pos + 1))
    if any(not math.isfinite(v) for v in values):
        return None
    return pos, n, values


def _read_run(buf: bytes, pos: int, count: int) -> Optional[List[tuple]]:
    out = []
    for _ in range(count):
        rec = _read_record(buf, pos)
        if rec is None:
            return None
        out.append(rec)
        pos += 1 + 4 * (rec[1] + 1)
    return out


def _valid_breakpoint_run(buf: bytes, pos: int) -> Optional[List[tuple]]:
    """Validate the 27-record breakpoint run at `pos`.

    The strongest structural invariant is that envelope *i* has the same
    breakpoint count in the time, level and slope runs.
    """
    recs = _read_run(buf, pos, 27)
    if recs is None:
        return None
    times, levels, slopes = recs[:9], recs[9:18], recs[18:27]
    if not all(times[i][1] == levels[i][1] == slopes[i][1] for i in range(9)):
        return None
    if not any(rec[1] > 0 for rec in times):
        return None
    if not all(all(v >= 0.0 for v in rec[2]) for rec in times):
        return None
    # Level/slope are nominal 0..1.  Allow a little slack for stored rounding
    # and for the occasional uninitialised value.
    if not all(all(-2.0 <= v <= 2.0 for v in rec[2]) for rec in levels + slopes):
        return None
    return recs


def locate_breakpoints(patch: Any) -> int:
    """Return the absolute offset of the first breakpoint record."""
    he = header_end(patch)
    buf = patch.inner
    pos = he + BREAKPOINT_OFFSET
    if _valid_breakpoint_run(buf, pos) is not None:
        return pos

    # Safety net for a future layout change: scan a window and prefer the first
    # structurally valid run.  The corpus never needs this, but a silent
    # misdecode is worse than a slower path.
    best = None
    for cand in range(max(he + 200, 0), min(len(buf) - 1, he + 5000)):
        recs = _valid_breakpoint_run(buf, cand)
        if recs is None:
            continue
        # Prefer a run whose following nine records look like key/velocity
        # scaling (finite, values in the 0..127 / -200..200 ranges).
        score = 0
        if _read_run(buf, cand + _run_size(recs), 9) is not None:
            score = 1
        key = (score, -abs(cand - pos))
        if best is None or key > best[0]:
            best = (key, cand)
    if best is None:
        raise ValueError("no FM8 envelope breakpoint run found")
    return best[1]


def _run_size(recs: Sequence[tuple]) -> int:
    return sum(1 + 4 * (rec[1] + 1) for rec in recs)


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------

@dataclass
class Breakpoint:
    """One envelope breakpoint in FM8's GUI units."""

    index: int          # 1-based, as shown by FM8 ("BP # n/N")
    abs_time_ms: float
    rel_time_ms: float
    level: float
    slope: float

    @property
    def abs_time_s(self) -> float:
        return self.abs_time_ms / 1000.0

    @property
    def rel_time_s(self) -> float:
        return self.rel_time_ms / 1000.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "index": self.index,
            "abs_time_s": self.abs_time_s,
            "rel_time_s": self.rel_time_s,
            "level": self.level,
            "slope": self.slope,
        }


@dataclass
class Envelope:
    """A decoded breakpoint envelope for one destination."""

    destination: str
    time_values_ms: List[float]
    levels: List[float]
    slopes: List[float]
    flags: Dict[str, bool]
    raw_flag_bytes: Dict[str, int]
    time_offset: int = 0
    level_offset: int = 0
    slope_offset: int = 0

    @property
    def segment_count(self) -> int:
        return len(self.time_values_ms) - 1

    @property
    def abs_times_ms(self) -> List[float]:
        out: List[float] = []
        total = 0.0
        for i, value in enumerate(self.time_values_ms):
            total = value if i == 0 else total + value
            out.append(total)
        return out

    @property
    def rel_times_ms(self) -> List[float]:
        return list(self.time_values_ms)

    @property
    def has_sentinel(self) -> bool:
        return any(
            not math.isfinite(v) or v < 0.0 or v > SENTINEL_MS
            for v in self.time_values_ms
        ) or any(
            not math.isfinite(v) or v < 0.0 or v > SENTINEL_MS
            for v in self.abs_times_ms
        )

    @property
    def breakpoints(self) -> List[Breakpoint]:
        abs_ms = self.abs_times_ms
        return [
            Breakpoint(
                index=i + 1,
                abs_time_ms=abs_ms[i],
                rel_time_ms=self.time_values_ms[i],
                level=self.levels[i],
                slope=self.slopes[i],
            )
            for i in range(len(self.time_values_ms))
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "destination": self.destination,
            "flags": dict(self.flags),
            "raw_flag_bytes": dict(self.raw_flag_bytes),
            "offsets": {
                "times": self.time_offset,
                "levels": self.level_offset,
                "slopes": self.slope_offset,
            },
            "has_sentinel": self.has_sentinel,
            "breakpoints": [bp.to_dict() for bp in self.breakpoints],
            "raw": {
                "time_values_ms": list(self.time_values_ms),
                "levels": list(self.levels),
                "slopes": list(self.slopes),
            },
        }


@dataclass
class ScalingRun:
    """One of the two post-slope scaling record runs.

    The record grammar is the same as for breakpoints, but the semantics of the
    values are only partly established (see docs/ENVELOPES.md).  They are
    exposed raw rather than interpreted as breakpoints.
    """

    records: List[tuple]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "offsets": [rec[0] for rec in self.records],
            "segments": [rec[1] for rec in self.records],
            "values": [list(rec[2]) for rec in self.records],
        }


@dataclass
class EnvelopeSection:
    destination_envelopes: Dict[str, Envelope]
    scaling_a: Optional[ScalingRun]
    scaling_b: Optional[ScalingRun]
    breakpoint_offset: int
    end_offset: int
    flag_offset: int
    header_end: int
    header_length: int

    @property
    def envelope_size(self) -> int:
        """Bytes occupied by the breakpoint and scaling records."""
        return self.end_offset - self.breakpoint_offset

    @property
    def parameter_shift(self) -> int:
        """Shift to add to schema offsets that follow the envelope section."""
        return ((self.header_length - REFERENCE_HEADER_LENGTH)
                + (self.envelope_size - REFERENCE_ENVELOPE_SIZE))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "header_end": self.header_end,
            "header_length": self.header_length,
            "breakpoint_offset": self.breakpoint_offset,
            "end_offset": self.end_offset,
            "envelope_size": self.envelope_size,
            "parameter_shift": self.parameter_shift,
            "flag_offset": self.flag_offset,
            "envelopes": {
                k: v.to_dict() for k, v in self.destination_envelopes.items()
            },
            "scaling_a": self.scaling_a.to_dict() if self.scaling_a else None,
            "scaling_b": self.scaling_b.to_dict() if self.scaling_b else None,
        }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def read_flags(patch: Any) -> Dict[str, Dict[str, bool]]:
    """Return the P.Env / Loop / Rel flags for each destination."""
    he = header_end(patch)
    start = he + FLAG_OFFSET
    raw = list(patch.inner[start:start + FLAG_BYTES])
    if len(raw) != FLAG_BYTES:
        raise ValueError("truncated FM8 envelope flag array")
    out: Dict[str, Dict[str, bool]] = {}
    for group_index, group in enumerate(FLAG_GROUPS):
        for dest_index, dest in enumerate(DESTINATIONS):
            value = raw[group_index * 9 + dest_index]
            out.setdefault(dest, {})[group] = value != 0
    return out


def read_raw_flags(patch: Any) -> Dict[str, Dict[str, int]]:
    """Like :func:`read_flags` but returns the stored byte values."""
    he = header_end(patch)
    start = he + FLAG_OFFSET
    raw = list(patch.inner[start:start + FLAG_BYTES])
    if len(raw) != FLAG_BYTES:
        raise ValueError("truncated FM8 envelope flag array")
    out: Dict[str, Dict[str, int]] = {}
    for group_index, group in enumerate(FLAG_GROUPS):
        for dest_index, dest in enumerate(DESTINATIONS):
            out.setdefault(dest, {})[group] = raw[group_index * 9 + dest_index]
    return out


def write_flags(patch: Any, flags: Dict[str, Dict[str, bool]]) -> Any:
    """Write the 27 ``P.Env`` / ``Loop`` / ``Rel`` flag bytes in place.

    `flags` is a ``{destination: {group: bool}}`` mapping.  Missing entries are
    left unchanged, so a partial mapping is safe.
    """
    he = header_end(patch)
    buf = patch.inner
    for group_index, group in enumerate(FLAG_GROUPS):
        for dest_index, dest in enumerate(DESTINATIONS):
            value = flags.get(dest, {}).get(group)
            if value is None:
                continue
            buf[he + FLAG_OFFSET + group_index * 9 + dest_index] = 1 if value else 0
    return patch


def _coerce_envelope_arrays(value: Any) -> tuple:
    """Accept an Envelope, a JSON dict, or a plain three-list tuple."""
    if isinstance(value, Envelope):
        return (list(value.time_values_ms), list(value.levels),
                list(value.slopes), dict(value.flags))
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return list(value[0]), list(value[1]), list(value[2]), None
    if not isinstance(value, dict):
        raise TypeError("envelope value must be Envelope, dict or 3-list")
    if "raw" in value and isinstance(value["raw"], dict):
        raw = value["raw"]
        return (list(raw["time_values_ms"]), list(raw["levels"]),
                list(raw["slopes"]), value.get("flags"))
    if "time_values_ms" in value and "levels" in value and "slopes" in value:
        return (list(value["time_values_ms"]), list(value["levels"]),
                list(value["slopes"]), value.get("flags"))
    if "breakpoints" in value:
        bps = value["breakpoints"]
        if not bps:
            raise ValueError("envelope has no breakpoints")
        times = [float(bps[0]["abs_time_s"]) * 1000.0]
        times += [float(bp["rel_time_s"]) * 1000.0 for bp in bps[1:]]
        return (times, [float(bp["level"]) for bp in bps],
                [float(bp["slope"]) for bp in bps], value.get("flags"))
    raise ValueError("cannot find time/level/slope arrays in envelope dict")


def _encode_record(values: Sequence[float]) -> bytes:
    n = len(values) - 1
    if n < 0 or n > 255:
        raise ValueError("breakpoint count out of range")
    return bytes([n]) + struct.pack("<%df" % (n + 1),
                                    *[float(v) for v in values])


def _inner_ancestors(patch: Any, pos: int) -> List[tuple]:
    """Return (tag, offset, size) for inner-tree nodes enclosing `pos`."""
    import nfm8  # local import: only the resizing writer needs the tree walk
    tree = nfm8.Patch(data=bytes(patch.inner)).tree()
    out = []
    for node, _parent, _depth in tree:
        start = node.off + (36 if node.tag == "hsin" else 20)
        if node.off <= pos < max(start, node.end):
            out.append((node.tag, node.off, node.size))
    return out


def _fix_lengths_after_splice(patch: Any, insert_at: int, old_len: int,
                              delta: int, ancestors: Sequence[tuple]) -> None:
    """Grow/shrink the inner item-tree length fields by `delta`."""
    buf = patch.inner
    # The inner tree's own total-size word.
    if len(buf) >= 4 and struct.unpack_from("<I", buf, 0)[0] == old_len:
        struct.pack_into("<I", buf, 0, old_len + delta)

    hsin_size = None
    for tag, off, size in ancestors:
        if tag == "hsin":
            field = off + 28
            hsin_size = size
        else:
            field = off + 8
        if field + 4 <= len(buf) and struct.unpack_from("<I", buf, field)[0] == size:
            struct.pack_into("<I", buf, field, size + delta)

    # The 1.7.x payload immediately before the FM8E block is the enclosing
    # item's payload length; it is 72 bytes smaller than the hsin size.
    if hsin_size is not None:
        field = None
        for tag, off, size in ancestors:
            if tag == "hsin" and size == hsin_size:
                field = off + 0x54
                break
        if field is not None and field + 4 <= len(buf):
            old = struct.unpack_from("<I", buf, field)[0]
            if old == hsin_size - 72:
                struct.pack_into("<I", buf, field, old + delta)

    # The 1.7.14 tsrp chunk length sits 12 bytes before the FM8E marker.
    tsrp = patch.payload - 12
    if tsrp >= 0 and bytes(buf[tsrp:tsrp + 4]) == b"tsrp":
        field = tsrp - 8
        if field >= 0:
            old = struct.unpack_from("<I", buf, field)[0]
            struct.pack_into("<I", buf, field, old + delta)


def write_envelopes(patch: Any, envelopes: Dict[str, Any], *,
                    write_flag_values: bool = True, strict: bool = True,
                    resize: bool = False) -> Any:
    """Rewrite breakpoint values in place.

    With the default ``resize=False`` this is the safe writer: every destination
    must keep the same number of breakpoints, so no byte is inserted or removed
    and every following parameter offset stays valid.  This path is validated
    through FM8's own ``effSetChunk`` (values and flags survive).

    ``resize=True`` rebuilds the first 27 records and fixes the inner-tree
    length fields.  It round-trips through the decoder, but FM8 1.7.14's
    ``effSetChunk`` reverted resized envelopes to defaults in testing, so treat
    resizing as experimental and always verify with the real plugin.

    Values are in the file's units (times in milliseconds).  Use
    :func:`read_section` / ``to_dict`` to get the current arrays, edit them,
    and pass the result back.
    """
    section = read_section(patch, strict=strict)
    buf = patch.inner
    staged = []
    flag_updates: Dict[str, Dict[str, bool]] = {}
    arrays: Dict[str, tuple] = {}

    for dest in DESTINATIONS:
        current = section.destination_envelopes[dest]
        if dest in envelopes:
            times, levels, slopes, flag_values = _coerce_envelope_arrays(
                envelopes[dest])
        else:
            times = list(current.time_values_ms)
            levels = list(current.levels)
            slopes = list(current.slopes)
            flag_values = None
        if not (len(times) == len(levels) == len(slopes)):
            raise ValueError("%s: time/level/slope lengths differ" % dest)
        if len(times) != len(current.time_values_ms) and not resize:
            raise ValueError(
                "%s: changing breakpoint count (%d -> %d) requires resize=True"
                % (dest, len(current.time_values_ms), len(times)))
        for label, values, positive in (("time", times, True),
                                        ("level", levels, False),
                                        ("slope", slopes, False)):
            for v in values:
                if not math.isfinite(v):
                    raise ValueError("%s %s value is not finite: %r"
                                     % (dest, label, v))
                if positive and v < 0.0:
                    raise ValueError("%s time value is negative: %r" % (dest, v))
                if not positive and not (-2.0 <= v <= 2.0):
                    raise ValueError("%s %s value out of range: %r"
                                     % (dest, label, v))
        arrays[dest] = (times, levels, slopes)
        if dest in envelopes:
            staged.append((current.time_offset, times))
            staged.append((current.level_offset, levels))
            staged.append((current.slope_offset, slopes))
        if write_flag_values and flag_values:
            flag_updates[dest] = {g: bool(flag_values.get(g, current.flags[g]))
                                  for g in FLAG_GROUPS}

    sizes_differ = any(
        len(arrays[d][0]) != len(section.destination_envelopes[d].time_values_ms)
        for d in DESTINATIONS)
    if not sizes_differ:
        # Safe in-place path: no offsets move.
        for offset, values in staged:
            n = len(values) - 1
            if buf[offset] != n:
                raise ValueError("record at %#x changed size during write" % offset)
            struct.pack_into("<%df" % (n + 1), buf, offset + 1,
                             *[float(v) for v in values])
        if flag_updates:
            write_flags(patch, flag_updates)
        return patch

    # Resizing path: rebuild all 27 breakpoint records, keep the two scaling
    # runs byte-for-byte, splice, and fix the enclosing inner-tree lengths.
    ancestors = _inner_ancestors(patch, section.breakpoint_offset)
    bp_bytes = b"".join(
        _encode_record(arrays[d][run])
        for run in range(3)
        for d in DESTINATIONS
    )
    old_bp_size = _run_size(_read_run(buf, section.breakpoint_offset, 27))
    scaling_bytes = bytes(buf[section.breakpoint_offset + old_bp_size:
                              section.end_offset])
    new_section = bp_bytes + scaling_bytes
    old_len = len(buf)
    delta = len(new_section) - (section.end_offset - section.breakpoint_offset)
    buf[section.breakpoint_offset:section.end_offset] = new_section
    patch.plain_len += delta
    _fix_lengths_after_splice(patch, section.breakpoint_offset, old_len,
                              delta, ancestors)
    if flag_updates:
        write_flags(patch, flag_updates)
    return patch


def read_envelopes(patch: Any, strict: bool = True) -> Dict[str, Envelope]:
    """Decode the nine breakpoint envelopes of `patch`.

    `strict=False` tolerates a non-finite or out-of-range stored value and
    keeps it in the raw arrays.  The default validates the structural
    invariants and raises :class:`ValueError` on a corrupt/unknown layout.
    """
    return {
        dest: env
        for dest, env in read_section(patch, strict=strict).destination_envelopes.items()
    }


def read_section(patch: Any, strict: bool = True) -> EnvelopeSection:
    """Decode the breakpoint records and the two scaling runs."""
    he = header_end(patch)
    buf = patch.inner

    if strict:
        pos = locate_breakpoints(patch)
        recs = _valid_breakpoint_run(buf, pos)
        if recs is None:  # cannot happen after locate, but keep the contract
            raise ValueError("invalid FM8 envelope breakpoint run")
    else:
        pos = he + BREAKPOINT_OFFSET
        recs = _read_run(buf, pos, 27)
        if recs is None:
            raise ValueError("truncated FM8 envelope breakpoint run")

    times, levels, slopes = recs[:9], recs[9:18], recs[18:27]
    flags = read_flags(patch)
    raw_flags = read_raw_flags(patch)

    envelopes: Dict[str, Envelope] = {}
    for i, dest in enumerate(DESTINATIONS):
        envelopes[dest] = Envelope(
            destination=dest,
            time_values_ms=list(times[i][2]),
            levels=list(levels[i][2]),
            slopes=list(slopes[i][2]),
            flags=dict(flags[dest]),
            raw_flag_bytes=dict(raw_flags[dest]),
            time_offset=times[i][0],
            level_offset=levels[i][0],
            slope_offset=slopes[i][0],
        )

    # Two nine-record scaling runs follow the slopes.  They are the last
    # envelope-owned records; the next byte starts the following parameter
    # section (whose schema offsets are shifted by parameter_shift).
    cursor = pos + _run_size(recs)
    scaling_a = _read_run(buf, cursor, 9)
    if scaling_a is not None:
        cursor += _run_size(scaling_a)
    scaling_b = _read_run(buf, cursor, 9)
    if scaling_b is not None:
        cursor += _run_size(scaling_b)

    return EnvelopeSection(
        destination_envelopes=envelopes,
        scaling_a=ScalingRun(scaling_a) if scaling_a else None,
        scaling_b=ScalingRun(scaling_b) if scaling_b else None,
        breakpoint_offset=pos,
        end_offset=cursor,
        flag_offset=he + FLAG_OFFSET,
        header_end=he,
        header_length=he - patch.payload,
    )


def parameter_shift(patch: Any) -> int:
    """Return the shift for schema offsets **after** the envelope section.

    `data/param_schema.json` offsets were measured on the 1.7.14 default chunk,
    which has a 56-byte header and a 577-byte envelope section.  A patch with a
    different header length or different breakpoint counts shifts every field
    after the envelope section by::

        (header_length - 56) + (envelope_size - 577)

    For `Whitty_Lead_` this is `(81 - 56) + (601 - 577) = 49`, which is exactly
    the empirical `+49` from the original handoff.
    """
    he = header_end(patch)
    return ((he - patch.payload - REFERENCE_HEADER_LENGTH)
            + (_envelope_owned_size(patch) - REFERENCE_ENVELOPE_SIZE))


def schema_offset(patch: Any, offset: int) -> int:
    """Map a 1.7.14 `param_schema.json` payload offset to this patch.

    Fields before the envelope section shift with the header only; fields after
    it also shift with the envelope section's size.  Offsets that fall *inside*
    the envelope section are not valid schema fields and raise `ValueError`.
    """
    he = header_end(patch)
    header_shift = he - patch.payload - REFERENCE_HEADER_LENGTH
    if offset < REFERENCE_ENVELOPE_START:
        return offset + header_shift
    if offset >= REFERENCE_ENVELOPE_END:
        return (offset + header_shift
                + _envelope_owned_size(patch) - REFERENCE_ENVELOPE_SIZE)
    raise ValueError(
        "schema offset %d lies inside the envelope section" % offset)


def _envelope_owned_size(patch: Any) -> int:
    """Bytes from the first breakpoint record through the second scaling run."""
    buf = patch.inner
    pos = header_end(patch) + BREAKPOINT_OFFSET
    recs = _read_run(buf, pos, 27)
    if recs is None:
        raise ValueError("truncated FM8 envelope breakpoint run")
    cursor = pos + _run_size(recs)
    scaling_a = _read_run(buf, cursor, 9)
    if scaling_a is not None:
        cursor += _run_size(scaling_a)
    scaling_b = _read_run(buf, cursor, 9)
    if scaling_b is not None:
        cursor += _run_size(scaling_b)
    return cursor - pos


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_seconds(v: float) -> str:
    if not math.isfinite(v):
        return "non-finite"
    return f"{v / 1000.0:.3f}"


def _print_human(section: EnvelopeSection, only: Optional[str] = None,
                 show_raw: bool = False) -> None:
    names = [only] if only else list(DESTINATIONS)
    if only and only not in section.destination_envelopes:
        raise SystemExit("unknown destination %r; choose from %s"
                         % (only, ", ".join(DESTINATIONS)))
    for dest in names:
        env = section.destination_envelopes[dest]
        flag_text = " ".join(
            "%s=%d" % (FLAG_LABELS[g], int(env.flags[g]))
            for g in FLAG_GROUPS
        )
        sentinel = "  ** sentinel/uninitialised time data **" if env.has_sentinel else ""
        print(f"{dest}: {len(env.breakpoints)} breakpoints  {flag_text}{sentinel}")
        print("  BP   AbsT(s)   RelT(s)   Level    Slope")
        for bp in env.breakpoints:
            print(f"  {bp.index:2d}  {_fmt_seconds(bp.abs_time_ms):>9} "
                  f"{_fmt_seconds(bp.rel_time_ms):>9}  {bp.level:7.3f}  {bp.slope:7.3f}")
        if show_raw:
            print("  raw time values (ms): "
                  + " ".join("%.6g" % v for v in env.time_values_ms))
            print("  raw levels:           "
                  + " ".join("%.6g" % v for v in env.levels))
            print("  raw slopes:           "
                  + " ".join("%.6g" % v for v in env.slopes))
        print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Decode FM8 breakpoint envelopes from an .nfm8 file.")
    ap.add_argument("patch", nargs="?", help=".nfm8 file")
    ap.add_argument("--operator", "-o", choices=DESTINATIONS,
                    help="only show one destination")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--raw", action="store_true", help="also show stored arrays")
    ap.add_argument("--schema", action="store_true",
                    help="print the machine-readable layout description")
    ap.add_argument("--apply", metavar="JSON",
                    help="rewrite envelope values from a JSON dump")
    ap.add_argument("--resize", action="store_true",
                    help="experimental: allow breakpoint-count changes with --apply")
    ap.add_argument("--output", "-w", metavar="FILE",
                    help="output file for --apply (default: <patch>.edited.nfm8)")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import fm8param  # local import keeps `--help` fast

    if args.schema:
        path = os.path.join(here, "data", "envelope_schema.json")
        with open(path, encoding="utf-8") as fh:
            print(json.dumps(json.load(fh), indent=2))
        return 0

    if not args.patch:
        ap.error("a patch file is required (or use --schema)")

    patch = fm8param.Patch(args.patch)

    if args.apply:
        with open(args.apply, encoding="utf-8") as fh:
            data = json.load(fh)
        envelopes = data.get("envelopes", data)
        write_envelopes(patch, envelopes, resize=args.resize)
        out = args.output or (os.path.splitext(args.patch)[0] + ".edited.nfm8")
        patch.write(out)
        print("wrote %s" % out)
        return 0

    section = read_section(patch)
    if args.json:
        print(json.dumps(section.to_dict(), indent=2))
    else:
        _print_human(section, only=args.operator, show_raw=args.raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
