#!/usr/bin/env python
"""Refuse to truncate a feature class when the staged replacement fails a plausibility check.

The nightly pattern this guards is everywhere in arcpy ETL:

    arcpy.TruncateTable_management(target)      # production is now empty
    arcpy.Append_management(staged, target, "NO_TEST")

Between those two lines the good copy is gone. A partial download, a vendor feed
that came back half empty, a join that silently nulled out, and you have already
destroyed production and appended the damage. Append's schema_type only checks
field shape, and only if you ask it to.

This runs the check first and refuses the delete when the replacement is not
believable. It compares the staged row count against what is live right now, so
a 288,000 row layer arriving with 180,000 rows is caught: that clears any sane
absolute floor and is still a 37 percent loss.

A row count is a proxy, though, and one failure defeats it completely: the join
key format changes, JoinField runs clean, every row arrives and every attribute
is NULL. --null-fields gates that, because the count is perfect and the data is
worthless.

--scan finds the pattern in a tree of scripts, so you can learn which of them
need this before you have to wrap any of them. --plan decides a whole batch of
pairs before performing any of them.

    python safe_republish.py --self-test
    python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels
    python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels --apply
    python safe_republish.py --source s.gdb/Parcels --target p.sde/Parcels --null-fields PARCELID
    python safe_republish.py --scan C:/etl/scripts
    python safe_republish.py --plan nightly.txt --apply

Exit codes: 0 allowed, 1 refused, 2 the replace failed part way, 64 usage error.
"""

from __future__ import print_function

import argparse
import collections
import io
import os
import re
import sys
import tokenize

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Fraction the staged count may differ from the live count before the run is
# refused. 0.15 means a 15 percent swing in either direction aborts.
DEFAULT_MAX_SWING = 0.15

# Absolute floor. A staged table below this is refused whatever the swing says,
# which is what stops a zero row feed from ever reaching production.
DEFAULT_MIN_ROWS = 1

# Verify the row count again after appending, and treat a mismatch as a failure.
VERIFY_AFTER_APPEND = True

# Largest fraction of a checked field that may be NULL or blank in the staged
# data before the run is refused. Only applies to fields named by --null-fields;
# naming none leaves this tool a row-count gate, which is what it was.
DEFAULT_MAX_NULL_PCT = 0.10

# How many lines may separate a clear call from the Append that refills it before
# --scan stops reading them as one truncate-then-append pair.
SCAN_WINDOW = 12

# Field-shape checking handed to Append. TEST refuses a schema mismatch, NO_TEST
# lets Append map what it can. TEST is the safer default; this tool is about not
# destroying data, and a silent field drop is a way to do that.
APPEND_SCHEMA_TYPE = "TEST"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

PRO_PYTHON = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"

# Decision codes.
ALLOW = "ALLOW"
REFUSE = "REFUSE"


class Decision(object):
    """The verdict, plus every reason behind it, with no arcpy involved."""

    def __init__(self, verdict, reasons, staged, live, swing):
        self.verdict = verdict
        self.reasons = reasons
        self.staged = staged
        self.live = live
        self.swing = swing

    @property
    def allowed(self):
        return self.verdict == ALLOW

    def __repr__(self):
        return "Decision(%s, staged=%r, live=%r, swing=%r)" % (
            self.verdict, self.staged, self.live, self.swing)


def _import_arcpy():
    """Import arcpy only when a real geodatabase is about to be touched."""
    try:
        import arcpy
    except ModuleNotFoundError:
        sys.exit(
            "arcpy was not found. Run this with the Python that ships with "
            "ArcGIS Pro:\n"
            '  "%s" safe_republish.py\n'
            "or the propy.bat in ...\\Pro\\bin\\Python\\Scripts\\.\n"
            "Only --self-test runs without arcpy." % PRO_PYTHON
        )
    return arcpy


# ----------------------------------------------------------------- pure core

def swing_fraction(staged, live):
    """Relative difference between the staged and live counts.

    Undefined against an empty target, which is exactly why an empty target is
    a separate decision rather than a division this function has to survive.
    """
    if live == 0:
        return None
    return abs(staged - live) / float(live)


def decide(staged, live, min_rows=DEFAULT_MIN_ROWS, max_swing=DEFAULT_MAX_SWING,
           allow_empty_target=False, null_counts=None,
           max_null_pct=DEFAULT_MAX_NULL_PCT):
    """Decide whether the staged data may replace the live data.

    An empty target REFUSES unless allow_empty_target is passed. This is the
    behaviour worth being deliberate about. An empty target usually means a
    previous run died part way through, which is precisely when the operator
    most needs to look before another truncate lands. Skipping the check there,
    because the relative comparison happens to be undefined, fails open at the
    one moment failing open is worst.

    null_counts maps each field named by --null-fields to the number of NULL or
    blank values in the staged data, or to None when the staged data has no
    field of that name. A missing field REFUSES. The hand-written guard this
    replaces filtered its missing fields out of its own check list instead, so
    renaming a field disabled the guard that watched it and said nothing. Counts
    arrive here as integers, exactly as row counts do, which keeps the whole
    decision free of arcpy.
    """
    if staged is None or live is None:
        raise ValueError("counts must be integers, got staged=%r live=%r"
                         % (staged, live))
    if staged < 0 or live < 0:
        raise ValueError("counts cannot be negative, got staged=%r live=%r"
                         % (staged, live))
    if min_rows < 0:
        raise ValueError("--min-rows cannot be negative")
    if max_swing < 0:
        raise ValueError("--max-swing cannot be negative")
    if max_null_pct < 0:
        raise ValueError("--max-null-pct cannot be negative")
    for field, nulls in sorted((null_counts or {}).items()):
        if nulls is not None and (nulls < 0 or nulls > staged):
            raise ValueError(
                "null count for %s must be between 0 and the staged count %d, "
                "got %r" % (field, staged, nulls))

    reasons = []
    verdict = ALLOW

    if staged < min_rows:
        verdict = REFUSE
        reasons.append(
            "staged has %d row(s), below the --min-rows floor of %d"
            % (staged, min_rows))

    swing = swing_fraction(staged, live)

    if live == 0:
        if allow_empty_target:
            reasons.append(
                "target is empty, so the swing check does not apply. "
                "Allowed because --allow-empty-target was passed.")
        else:
            verdict = REFUSE
            reasons.append(
                "target is EMPTY. A previous run may have died part way "
                "through. Verify production, then pass --allow-empty-target "
                "to proceed.")
    elif swing > max_swing:
        verdict = REFUSE
        reasons.append(
            "staged count %d swings %.1f%% from the live count %d, over the "
            "--max-swing limit of %.1f%%"
            % (staged, swing * 100.0, live, max_swing * 100.0))
    else:
        reasons.append(
            "staged count %d is within %.1f%% of the live count %d "
            "(limit %.1f%%)"
            % (staged, swing * 100.0, live, max_swing * 100.0))

    for field, nulls in sorted((null_counts or {}).items()):
        if nulls is None:
            verdict = REFUSE
            reasons.append(
                "field %s is named by --null-fields but the staged data has no "
                "such field. A renamed field must not silently disable the "
                "guard that watched it." % field)
            continue
        fraction = nulls / float(staged) if staged else 0.0
        if fraction > max_null_pct:
            verdict = REFUSE
            reasons.append(
                "field %s is %.1f%% NULL or blank in the staged data (%d of "
                "%d), over the --max-null-pct limit of %.1f%%"
                % (field, fraction * 100.0, nulls, staged,
                   max_null_pct * 100.0))
        else:
            reasons.append(
                "field %s is %.1f%% NULL or blank (limit %.1f%%)"
                % (field, fraction * 100.0, max_null_pct * 100.0))

    return Decision(verdict, reasons, staged, live, swing)


# Underscore is a word character, so "TruncateTable\b" never matches
# "TruncateTable_management(", which is the spelling nearly every legacy script
# uses. That one boundary took an earlier scan of the corpus behind this tool
# from 179 sites to 1, so the suffix is optional here and the boundary is not.
CLEAR_CALL = re.compile(
    r"\b(TruncateTable|DeleteRows|DeleteFeatures|Delete)(?:_management)?\s*\(")
APPEND_CALL = re.compile(r"\bAppend(?:_management)?\s*\(")
COUNT_CALL = re.compile(r"\bGetCount(?:_management)?\s*\(")
SCHEMA_ARG = re.compile(r"""['"](NO_TEST|TEST)['"]""")

Finding = collections.namedtuple(
    "Finding", "clear_line clear_call append_line schema counts_rows")


def mask_comments(text):
    """Blank every comment tail, leaving all other columns where they were.

    Line and column positions have to survive, because a finding reports them.
    String literals are kept, because the schema_type this looks for is one.
    A file that will not tokenize is scanned raw rather than skipped: of the
    743 legacy files this scanner was measured against, 5 fail to tokenize and
    every one of those is a broken file, not a Python 2 file.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:
        return text
    lines = text.splitlines()
    for token in tokens:
        if token[0] != tokenize.COMMENT:
            continue
        row, start = token[2]
        end = token[3][1]
        line = lines[row - 1]
        lines[row - 1] = line[:start] + " " * (end - start) + line[end:]
    return "\n".join(lines)


def scan_source(text, window=SCAN_WINDOW):
    """Find the truncate-then-append pairs in one source file.

    counts_rows is per file rather than per pair, because that is the question
    the measurement asked: does this script count anything at all before it
    empties a table. A commented-out clear call is not a pair, which matters:
    a live clear in one copy of a script and a commented one in its sibling is
    the difference that decides whether the source data survives the night.
    """
    lines = mask_comments(text).splitlines()
    counts_rows = any(COUNT_CALL.search(line) for line in lines)
    appends = [n for n, line in enumerate(lines, 1) if APPEND_CALL.search(line)]
    found = []
    for number, line in enumerate(lines, 1):
        clear = CLEAR_CALL.search(line)
        if not clear:
            continue
        for append_line in appends:
            if 0 < append_line - number <= window:
                near = "\n".join(lines[append_line - 1:append_line + 4])
                schema = SCHEMA_ARG.search(near)
                found.append(Finding(
                    number, clear.group(1), append_line,
                    schema.group(1) if schema else None, counts_rows))
                break
    return found


def scan_summary(results):
    """Totals for a scan: (files, sites, blind files, blind sites).

    Blind means the file calls GetCount nowhere, so nothing in it could have
    refused the truncate whatever the staged data looked like.
    """
    sites = sum(len(f) for _path, f in results)
    blind = [f for _path, f in results if not f[0].counts_rows]
    return len(results), sites, len(blind), sum(len(f) for f in blind)


def parse_plan(text):
    """Read a plan: one "source,target" pair per line, # comments ignored."""
    pairs = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.count(",") != 1:
            raise ValueError(
                "plan line %d is not source,target: %s" % (number, line))
        source, target = [part.strip() for part in line.split(",")]
        pairs.append((source, target))
    return pairs


def check_plan(pairs):
    """Refuse a whole plan before a single pair of it is decided.

    A one-target guard in a fourteen-target run stops the job halfway and leaves
    the database half old and half new. The case that no re-run recovers is the
    same target named twice, because the second truncate destroys what the first
    append wrote, so it is refused here, before any count is read. Returns the
    refusal reasons; an empty list means the plan may proceed.
    """
    reasons = []
    seen = {}
    for index, (source, target) in enumerate(pairs, 1):
        if not source or not target:
            reasons.append("step %d names an empty source or target" % index)
            continue
        if target in seen:
            reasons.append(
                "target %s is named twice, by step %d and step %d. The second "
                "truncate would destroy what the first append wrote."
                % (target, seen[target], index))
        else:
            seen[target] = index
    return reasons


def describe(decision):
    """Render a decision as the lines the CLI prints."""
    out = ["  %s: %s" % ("ok" if decision.verdict == ALLOW else "REFUSED", r)
           for r in decision.reasons]
    out.append("VERDICT: %s" % decision.verdict)
    return out


# ------------------------------------------------------------------ geodatabase

def count_rows(path, arcpy):
    return int(arcpy.management.GetCount(path)[0])


def count_nulls(path, fields, arcpy):
    """Count the NULL or blank values of each named field in the staged data.

    A field the staged data does not have maps to None rather than being
    dropped, because dropping it is the defect: the guard this replaces built
    its check list by filtering out the fields it could not find, so the day a
    field was renamed the guard that watched it stopped running and said
    nothing. decide() refuses a None.

    Blank counts with NULL. A join whose key format changed writes empty text
    as readily as it writes NULL, and neither one is data.
    """
    present = set(field.name for field in arcpy.ListFields(path))
    counts = dict((name, None) for name in fields if name not in present)
    readable = [name for name in fields if name in present]
    if not readable:
        return counts
    for name in readable:
        counts[name] = 0
    with arcpy.da.SearchCursor(path, readable) as rows:
        for row in rows:
            for name, value in zip(readable, row):
                if value is None or (isinstance(value, str) and not value.strip()):
                    counts[name] += 1
    return counts


def is_versioned(path, arcpy):
    """True when the target is a traditionally versioned feature class.

    TruncateTable refuses a versioned table, so the replace path has to use
    DeleteRows instead. Getting this wrong is a run that dies after the check
    passed, which is the worst time for it.

    A Describe that fails is not an answer, and it used to be read as one: the
    handler returned False, and clear_target sent that False straight on to
    TruncateTable. A target nobody could describe was emptied anyway, which is
    the outcome the paragraph above exists to prevent. Refuse instead.
    """
    try:
        described = arcpy.Describe(path)
    except Exception as exc:
        raise RuntimeError(
            "cannot describe the target %s, so whether it is versioned is "
            "unknown and nothing will be emptied: %s" % (path, exc))
    return bool(getattr(described, "isVersioned", False))


def clear_target(path, arcpy):
    """Empty the target, choosing the operation its versioning allows."""
    if is_versioned(path, arcpy):
        arcpy.management.DeleteRows(path)
        return "DeleteRows (target is versioned)"
    arcpy.management.TruncateTable(path)
    return "TruncateTable"


def replace(source, target, arcpy, schema_type=APPEND_SCHEMA_TYPE,
            field_mapping=None):
    """Clear the target and append the source. Returns the final row count.

    field_mapping is the opaque arcpy field-map string, passed through so the
    tool can be dropped into the call sites that need one. Append ignores a
    mapping under TEST, so a call that carries one is sent as NO_TEST. That is
    local to this call and leaves the configured default alone.
    """
    staged = count_rows(source, arcpy)
    how = clear_target(target, arcpy)
    print("  cleared target via %s" % how)
    if field_mapping:
        schema_type = "NO_TEST"
    try:
        arcpy.management.Append(source, target, schema_type, field_mapping)
    except Exception as exc:
        # The target is empty by this line, and the print above put that fact on
        # stdout only. Wherever the traceback is what gets mailed, stdout is not
        # there, so the exception has to carry the name of what was cleared.
        raise RuntimeError(
            "Append into %s FAILED after the target was cleared via %s. The "
            "target is EMPTY. Restore it before re-running: %s"
            % (target, how, exc))
    print("  appended %d row(s) from the staged source" % staged)

    if VERIFY_AFTER_APPEND:
        after = count_rows(target, arcpy)
        if after != staged:
            raise RuntimeError(
                "ROW COUNT MISMATCH after append: staged %d, target now %d. "
                "The target is in an unknown state, inspect it before "
                "re-running." % (staged, after))
        print("  verified: target now holds %d row(s)" % after)
    return staged


# ------------------------------------------------------------------ self-test

class _FakeCursor(object):
    """The little that arcpy.da.SearchCursor has to be for count_nulls."""

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return iter(self.rows)

    def __exit__(self, *exc_info):
        return False


class _FakeArcpy(object):
    """The smallest arcpy the geodatabase layer can be driven against.

    Every function in this file already takes arcpy as an argument, so one stub
    serves all of them and the self-test still touches no geodatabase. It is
    also its own .management and .da, because nothing here needs those to be
    separate objects.

    describe_error is the shape that matters: a Describe that raises used to be
    read as "not versioned" and the target was truncated anyway.
    """

    def __init__(self, counts=None, versioned=False, describe_error=None,
                 append_error=None, fields=(), rows=()):
        self.counts = dict(counts or {})
        self.versioned = versioned
        self.describe_error = describe_error
        self.append_error = append_error
        self.fields = list(fields)
        self.rows = list(rows)
        self.calls = []
        self.management = self
        self.da = self

    def GetCount(self, path):
        return [str(self.counts.get(path, 0))]

    def Describe(self, path):
        if self.describe_error:
            raise RuntimeError(self.describe_error)
        return type("_Described", (object,), {"isVersioned": self.versioned})()

    def DeleteRows(self, path):
        self.calls.append(("DeleteRows", path))
        self.counts[path] = 0

    def TruncateTable(self, path):
        self.calls.append(("TruncateTable", path))
        self.counts[path] = 0

    def Append(self, source, target, schema_type, field_mapping=None):
        self.calls.append(("Append", target, schema_type, field_mapping))
        if self.append_error:
            raise RuntimeError(self.append_error)
        self.counts[target] = self.counts.get(source, 0)

    def ListFields(self, path):
        return [type("_Field", (object,), {"name": name})()
                for name in self.fields]

    def SearchCursor(self, path, field_names):
        columns = [self.fields.index(name) for name in field_names]
        return _FakeCursor([tuple(row[i] for i in columns)
                            for row in self.rows])


def self_test():
    """Assertions over the decision core. No arcpy, no geodatabase, no network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("safe_republish self-test: no arcpy, no database, no network")
    print("-" * 68)

    # ---- the swing calculation
    check(swing_fraction(100, 100) == 0.0, "an identical count swings 0%")
    check(swing_fraction(85, 100) == 0.15, "a 15 point drop from 100 swings 15%")
    check(swing_fraction(115, 100) == 0.15, "a 15 point rise from 100 swings 15%")
    check(swing_fraction(50, 100) == 0.5, "half the rows swings 50%")
    check(swing_fraction(0, 100) == 1.0, "an empty source swings 100%")
    check(swing_fraction(200, 100) == 1.0, "double the rows swings 100%")
    check(swing_fraction(10, 0) is None, "the swing is undefined against an empty target")

    # ---- the ordinary allow
    d = decide(100, 100)
    check(d.allowed, "an identical count is allowed")
    check(d.verdict == ALLOW, "the verdict reads ALLOW")
    d = decide(98, 100)
    check(d.allowed, "a 2% drop is allowed")
    d = decide(286000, 288000)
    check(d.allowed, "a realistic parcel refresh is allowed")

    # ---- the swing refusal, the headline check
    d = decide(180000, 288000)
    check(not d.allowed, "a 37% loss is REFUSED even though the count is large")
    check(any("swings" in r for r in d.reasons), "the refusal explains the swing")
    check(decide(84, 100).allowed is False, "a 16% drop is refused at the default limit")
    check(decide(85, 100).allowed is True, "exactly 15% is allowed, the limit is inclusive")
    check(decide(116, 100).allowed is False, "a 16% GROWTH is refused too")
    check(decide(84, 100, max_swing=0.20).allowed is True,
          "a wider --max-swing allows what the default refused")
    check(decide(98, 100, max_swing=0.0).allowed is False,
          "--max-swing 0 refuses any change at all")
    check(decide(100, 100, max_swing=0.0).allowed is True,
          "--max-swing 0 still allows an identical count")

    # ---- THE PINNED DEFECT: an empty target must fail closed
    d = decide(100, 0)
    check(not d.allowed,
          "an EMPTY TARGET is REFUSED by default  <-- pinned defect")
    check(any("EMPTY" in r for r in d.reasons),
          "the refusal says the target is empty")
    check(any("--allow-empty-target" in r for r in d.reasons),
          "the refusal names the flag that would override it")
    d = decide(100, 0, allow_empty_target=True)
    check(d.allowed, "an empty target is allowed once --allow-empty-target is passed")
    check(decide(0, 0, allow_empty_target=True).allowed is False,
          "an empty source into an empty target is still refused by the floor")
    check(decide(100, 0).swing is None,
          "no swing is computed against an empty target")

    # ---- the absolute floor
    check(decide(0, 100).allowed is False, "a zero row source is refused")
    check(decide(0, 0).allowed is False, "a zero row source is refused against an empty target")
    d = decide(5, 100, min_rows=10)
    check(not d.allowed, "a source under --min-rows is refused")
    check(any("min-rows" in r for r in d.reasons), "the refusal names the floor")
    check(decide(10, 100, min_rows=10, max_swing=1.0).allowed is True,
          "exactly --min-rows is allowed, the floor is inclusive")
    check(decide(0, 100, min_rows=0, max_swing=1.0).allowed is True,
          "--min-rows 0 permits a deliberate empty publish")

    # ---- the floor and the swing are independent
    d = decide(5, 1000, min_rows=10)
    check(not d.allowed, "a source failing both floor and swing is refused")
    check(len([r for r in d.reasons if "REFUS" in r or "below" in r or "swings" in r]) >= 2,
          "both failures are reported, not just the first")

    # ---- input validation
    raises(lambda: decide(None, 100), "a null staged count raises")
    raises(lambda: decide(100, None), "a null live count raises")
    raises(lambda: decide(-1, 100), "a negative staged count raises")
    raises(lambda: decide(100, -1), "a negative live count raises")
    raises(lambda: decide(100, 100, min_rows=-1), "a negative --min-rows raises")
    raises(lambda: decide(100, 100, max_swing=-0.1), "a negative --max-swing raises")

    # ---- rendering
    lines = describe(decide(100, 100))
    check(lines[-1] == "VERDICT: ALLOW", "an allowed decision renders its verdict last")
    lines = describe(decide(10, 100))
    check(lines[-1] == "VERDICT: REFUSE", "a refused decision renders its verdict last")
    check(any(l.startswith("  REFUSED:") for l in lines),
          "a refusal reason is labelled REFUSED")

    # ---- argument handling
    a = _parse(["--source", "s", "--target", "t"])
    check(a.apply is False, "--apply defaults to OFF")
    check(a.allow_empty_target is False, "--allow-empty-target defaults to OFF")
    check(a.max_swing == DEFAULT_MAX_SWING, "--max-swing defaults to the configured value")
    check(a.min_rows == DEFAULT_MIN_ROWS, "--min-rows defaults to the configured value")
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(_parse(["--source", "s", "--target", "t",
                  "--max-swing", "0.5"]).max_swing == 0.5, "--max-swing is read")
    check(_parse(["--source", "s", "--target", "t",
                  "--min-rows", "7"]).min_rows == 7, "--min-rows is read")
    check(a.max_null_pct == DEFAULT_MAX_NULL_PCT,
          "--max-null-pct defaults to the configured value")
    check(a.null_fields == "", "--null-fields defaults to naming no field")
    check(a.field_mapping is None, "--field-mapping defaults to none")
    check(a.scan is None, "--scan is off unless a directory is given")
    check(a.plan is None, "--plan is off unless a file is given")
    check(_parse(["--source", "s", "--target", "t",
                  "--null-fields", "A,B"]).null_fields == "A,B",
          "--null-fields is read")
    check(_parse(["--source", "s", "--target", "t",
                  "--max-null-pct", "0.5"]).max_null_pct == 0.5,
          "--max-null-pct is read")
    check(_parse(["--scan", "somedir"]).scan == "somedir", "--scan is read")
    check(_parse(["--plan", "p.txt"]).plan == "p.txt", "--plan is read")

    # ---- the null gate. The row count is perfect and the data is worthless.
    d = decide(100, 100, null_counts={"PARCELID": 3})
    check(d.allowed, "a field 3% NULL is allowed at the default 10% limit")
    check(any("PARCELID" in r for r in d.reasons),
          "an allowed null check still reports the field it checked")
    d = decide(100, 100, null_counts={"PARCELID": 40})
    check(not d.allowed, "a field 40% NULL is REFUSED")
    check(any("PARCELID" in r for r in d.reasons),
          "the null refusal names the field")
    check(any("--max-null-pct" in r for r in d.reasons),
          "the null refusal names the flag that sets the limit")
    check(decide(100, 100, null_counts={"F": 10}).allowed is True,
          "exactly --max-null-pct is allowed, the limit is inclusive")
    check(decide(100, 100, null_counts={"F": 11}).allowed is False,
          "one point over --max-null-pct is refused")
    check(decide(100, 100, null_counts={"F": 1}, max_null_pct=0.0).allowed is False,
          "--max-null-pct 0 refuses a single null")
    check(decide(100, 100, null_counts={"F": 0}, max_null_pct=0.0).allowed is True,
          "--max-null-pct 0 allows a field with no nulls at all")
    check(decide(288000, 288000, null_counts={"OWNERNAME": 288000}).allowed is False,
          "a complete parcel refresh with every owner name NULL is refused")
    check(decide(100, 100).allowed is True,
          "naming no --null-fields leaves the decision a row count gate")
    d = decide(100, 100, null_counts={"F": 40}, max_null_pct=0.5)
    check(d.allowed, "a wider --max-null-pct allows what the default refused")

    # ---- THE PINNED DEFECT: a renamed field must not disable its own guard
    d = decide(100, 100, null_counts={"PARCEL_ID": None})
    check(not d.allowed,
          "a field the staged data does not have is REFUSED  <-- pinned defect")
    check(any("--null-fields" in r for r in d.reasons),
          "the missing field refusal names the flag that asked for it")
    check(any("PARCEL_ID" in r for r in d.reasons),
          "the missing field refusal names the field")
    check(decide(100, 100, null_counts={"A": 0, "B": None}).allowed is False,
          "one missing field refuses even when every other field is clean")
    d = decide(180000, 288000, null_counts={"F": 90000})
    check(len([r for r in d.reasons if "swings" in r or "NULL" in r]) >= 2,
          "a swing failure and a null failure are both reported")
    raises(lambda: decide(100, 100, null_counts={"F": 101}),
           "a null count above the staged count raises")
    raises(lambda: decide(100, 100, null_counts={"F": -1}),
           "a negative null count raises")
    raises(lambda: decide(100, 100, max_null_pct=-0.1),
           "a negative --max-null-pct raises")

    # ---- counting nulls out of a staged table
    fake = _FakeArcpy(fields=["PARCELID", "OWNERNAME"],
                      rows=[("1", "A"), ("2", None), ("3", "   ")])
    counts = count_nulls("staged", ["PARCELID", "OWNERNAME"], fake)
    check(counts["PARCELID"] == 0, "a fully populated field counts zero nulls")
    check(counts["OWNERNAME"] == 2,
          "a blank string counts with a NULL, because neither one is data")
    check(count_nulls("staged", ["NOSUCH"], fake) == {"NOSUCH": None},
          "a field the table does not have is reported, not filtered away")
    counts = count_nulls("staged", ["PARCELID", "NOSUCH"], fake)
    check(counts["PARCELID"] == 0 and counts["NOSUCH"] is None,
          "a missing field does not stop the fields that are there")

    # ---- THE PINNED DEFECT: a target nobody can describe is not truncated
    fake = _FakeArcpy(describe_error="ERROR 000732: the dataset does not exist")
    try:
        clear_target("prod.sde/Parcels", fake)
        check(False, "a target whose Describe fails is REFUSED  <-- pinned defect")
    except RuntimeError as exc:
        check("prod.sde/Parcels" in str(exc),
              "a target whose Describe fails is REFUSED  <-- pinned defect")
    check(fake.calls == [],
          "nothing was emptied when Describe could not answer")
    fake = _FakeArcpy(versioned=True)
    check(clear_target("t", fake) == "DeleteRows (target is versioned)",
          "a versioned target is emptied with DeleteRows")
    fake = _FakeArcpy(versioned=False)
    check(clear_target("t", fake) == "TruncateTable",
          "a non-versioned target is emptied with TruncateTable")

    # ---- the replace, and what its failure says
    fake = _FakeArcpy(counts={"s": 100, "t": 100})
    check(replace("s", "t", fake) == 100, "a replace returns the staged count")
    check(("Append", "t", "TEST", None) in fake.calls,
          "the configured schema type reaches Append")
    fake = _FakeArcpy(counts={"s": 100, "t": 100})
    replace("s", "t", fake, field_mapping="PARCELID 'Parcel' true true")
    check(fake.calls[-1][3] == "PARCELID 'Parcel' true true",
          "a field mapping reaches Append")
    check(fake.calls[-1][2] == "NO_TEST",
          "a field mapping sends that one call NO_TEST, because TEST ignores it")
    check(APPEND_SCHEMA_TYPE == "TEST",
          "the configured default is still TEST after a mapped call")
    fake = _FakeArcpy(counts={"s": 100, "t": 100},
                      append_error="ERROR 000466: schema does not match")
    try:
        replace("s", "t", fake)
        check(False, "a failed Append raises")
    except RuntimeError as exc:
        message = str(exc)
        check("t" in message, "a failed Append names the target it cleared")
        check("EMPTY" in message,
              "a failed Append says the target is empty, not only that it failed")
        check("TruncateTable" in message,
              "a failed Append says how the target was cleared")

    # ---- --scan: find the pattern before you have to wrap anything
    SRC = ('import arcpy\n'
           'arcpy.TruncateTable_management(target)\n'
           'arcpy.Append_management(staged, target, "NO_TEST")\n')
    f = scan_source(SRC)
    check(len(f) == 1, "a truncate-then-append pair is one finding")
    check(f[0].clear_line == 2 and f[0].append_line == 3,
          "the finding carries the line of each half of the pair")
    check(f[0].clear_call == "TruncateTable",
          "TruncateTable_management is found  <-- pinned defect")
    check(f[0].schema == "NO_TEST", "the finding reads the schema type")
    check(f[0].counts_rows is False,
          "a file that counts nothing is marked blind")
    check(len(scan_source('arcpy.management.TruncateTable(t)\n'
                          'arcpy.management.Append(s, t, "TEST")\n')) == 1,
          "the arcpy.management spelling is found too")
    check(scan_source('arcpy.management.TruncateTable(t)\n'
                      'arcpy.management.Append(s, t, "TEST")\n')[0].schema == "TEST",
          "a TEST append is reported as TEST, not as a blind one")
    check(len(scan_source('arcpy.DeleteRows_management(t)\n'
                          'arcpy.Append_management(s, t)\n')) == 1,
          "DeleteRows counts as clearing the target")
    check(len(scan_source('arcpy.DeleteFeatures_management(t)\n'
                          'arcpy.Append_management(s, t)\n')) == 1,
          "DeleteFeatures counts as clearing the target")
    check(scan_source('arcpy.DeleteRows_management(t)\n'
                      'arcpy.Append_management(s, t)\n')[0].schema is None,
          "an append with no schema argument reports None, not a guess")
    check(scan_source('# arcpy.TruncateTable_management(t)\n'
                      'arcpy.Append_management(s, t)\n') == [],
          "a commented out truncate is not a pair")
    check(len(scan_source('arcpy.TruncateTable_management(t)\n' +
                          '\n' * 20 + 'arcpy.Append_management(s, t)\n')) == 0,
          "an append past the window is not a pair")
    check(scan_source('arcpy.Append_management(s, t)\n'
                      'arcpy.TruncateTable_management(t)\n') == [],
          "an append BEFORE the truncate is not a pair")
    check(scan_source('n = arcpy.GetCount_management(s)\n'
                      'arcpy.TruncateTable_management(t)\n'
                      'arcpy.Append_management(s, t)\n')[0].counts_rows is True,
          "GetCount_management anywhere in the file clears the blind mark")
    check(scan_source('n = arcpy.management.GetCount(s)\n'
                      'arcpy.TruncateTable_management(t)\n'
                      'arcpy.Append_management(s, t)\n')[0].counts_rows is True,
          "the arcpy.management.GetCount spelling counts too")
    check(scan_source('print "hello"\n'
                      'arcpy.TruncateTable_management(t)\n'
                      'arcpy.Append_management(s, t)\n')[0].clear_line == 2,
          "a Python 2 print statement does not stop the scan")
    check(len(scan_source('arcpy.TruncateTable_management(t  # unbalanced\n'
                          'arcpy.Append_management(s, t)\n')) == 1,
          "a file that will not tokenize is scanned raw, never silently clean")
    check(scan_source("x = 1\n") == [], "a file with no pair reports nothing")
    check(mask_comments('a = 1  # note\nb = 2\n').splitlines()[1] == "b = 2",
          "masking a comment leaves the following lines where they were")
    check(mask_comments('s = "# not a comment"\n') == 's = "# not a comment"',
          "a hash inside a string literal is not a comment")
    results = [("a.py", scan_source(SRC)), ("b.py", scan_source(
        'n = arcpy.GetCount_management(s)\n'
        'arcpy.TruncateTable_management(t)\n'
        'arcpy.Append_management(s, t)\n'))]
    check(scan_summary(results) == (2, 2, 1, 1),
          "a scan summary separates the blind files from the counting ones")

    # ---- --plan: decide the whole batch before performing any of it
    check(check_plan([("s1", "t1"), ("s2", "t2")]) == [],
          "a plan naming distinct targets passes")
    problems = check_plan([("s1", "t1"), ("s2", "t1")])
    check(problems, "a plan naming the same target twice REFUSES  <-- pinned defect")
    check(any("step 1" in p and "step 2" in p for p in problems),
          "the plan refusal names both steps that collide")
    check(any("t1" in p for p in problems), "the plan refusal names the target")
    check(check_plan([("s", "")]), "a plan step with no target refuses")
    check(check_plan([("", "t")]), "a plan step with no source refuses")
    check(parse_plan("a,b\nc,d\n") == [("a", "b"), ("c", "d")],
          "a plan file reads one source,target pair per line")
    check(parse_plan("# nightly\n\n a , b \n") == [("a", "b")],
          "a plan file ignores comments, blank lines and spacing")
    raises(lambda: parse_plan("a b\n"), "a plan line with no comma raises")
    raises(lambda: parse_plan("a,b,c\n"), "a plan line with two commas raises")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="safe_republish.py",
        description="Refuse to truncate a feature class when the staged "
                    "replacement fails a plausibility check.",
        epilog="Config precedence: flag > environment > the CONFIGURATION "
               "block. Nothing is deleted without --apply.",
    )
    ap.add_argument("--source", help="staged replacement data")
    ap.add_argument("--target",
                    help="feature class that would be emptied and refilled")
    ap.add_argument("--min-rows", dest="min_rows", type=int,
                    default=int(os.environ.get("SAFE_REPUBLISH_MIN_ROWS",
                                               DEFAULT_MIN_ROWS)),
                    help="absolute row floor for the staged data "
                         "(default %d). Env: SAFE_REPUBLISH_MIN_ROWS"
                         % DEFAULT_MIN_ROWS)
    ap.add_argument("--max-swing", dest="max_swing", type=float,
                    default=float(os.environ.get("SAFE_REPUBLISH_MAX_SWING",
                                                 DEFAULT_MAX_SWING)),
                    help="largest allowed relative change against the live "
                         "count, as a fraction (default %.2f). "
                         "Env: SAFE_REPUBLISH_MAX_SWING" % DEFAULT_MAX_SWING)
    ap.add_argument("--null-fields", dest="null_fields", default="",
                    help="comma separated fields whose NULL or blank fraction "
                         "is checked in the staged data. A field named here "
                         "that the staged data does not have REFUSES.")
    ap.add_argument("--max-null-pct", dest="max_null_pct", type=float,
                    default=float(os.environ.get("SAFE_REPUBLISH_MAX_NULL_PCT",
                                                 DEFAULT_MAX_NULL_PCT)),
                    help="largest allowed NULL or blank fraction for each "
                         "--null-fields field (default %.2f). "
                         "Env: SAFE_REPUBLISH_MAX_NULL_PCT"
                         % DEFAULT_MAX_NULL_PCT)
    ap.add_argument("--field-mapping", dest="field_mapping", default=None,
                    help="arcpy field map string passed straight to Append. "
                         "Append ignores one under TEST, so supplying it sends "
                         "this one call as NO_TEST.")
    ap.add_argument("--scan", metavar="DIR",
                    help="report the truncate-then-append pairs under DIR and "
                         "which of them count no rows. Reads only.")
    ap.add_argument("--plan", metavar="FILE",
                    help="decide every source,target pair in FILE before "
                         "performing any of them.")
    ap.add_argument("--allow-empty-target", dest="allow_empty_target",
                    action="store_true",
                    help="proceed when the target is already empty. Off by "
                         "default because an empty target usually means a "
                         "previous run died part way through.")
    ap.add_argument("--apply", action="store_true",
                    help="perform the replace. Without this nothing is deleted.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def scan_tree(root, window=SCAN_WINDOW):
    """Walk a tree and return (path, findings) for each file holding a pair."""
    results = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if not name.lower().endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as handle:
                    text = handle.read().decode("utf-8", "replace")
            except IOError:
                continue
            findings = scan_source(text, window)
            if findings:
                results.append((path, findings))
    return results


def _run_scan(root):
    """Print the scan and exit non-zero when anything is blind."""
    if not os.path.isdir(root):
        print("error: --scan needs a directory: %s" % root, file=sys.stderr)
        return 64
    results = scan_tree(root)
    for path, findings in results:
        blind = not findings[0].counts_rows
        print("%s %s" % ("BLIND " if blind else "counts", path))
        for f in findings:
            print("    line %d %s -> line %d Append %s"
                  % (f.clear_line, f.clear_call, f.append_line,
                     f.schema or "schema_type not on the call line"))
    files, sites, blind_files, blind_sites = scan_summary(results)
    print("")
    print("%d file(s), %d truncate-then-append site(s)" % (files, sites))
    print("%d file(s) and %d site(s) count no rows anywhere, so nothing in "
          "them could refuse the truncate" % (blind_files, blind_sites))
    return 1 if blind_files else 0


def _run_plan(path, args):
    """Decide a whole batch, refusing the batch before any pair is read."""
    try:
        with open(path) as handle:
            pairs = parse_plan(handle.read())
    except (IOError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    if not pairs:
        print("error: the plan names no pairs.", file=sys.stderr)
        return 64

    problems = check_plan(pairs)
    if problems:
        for problem in problems:
            print("  REFUSED: %s" % problem)
        print("VERDICT: REFUSE")
        print("\nNothing was read and nothing was deleted.")
        return 1

    arcpy = _import_arcpy()
    decisions = []
    for source, target in pairs:
        for label, one in (("source", source), ("target", target)):
            if not arcpy.Exists(one):
                print("error: %s does not exist: %s" % (label, one),
                      file=sys.stderr)
                return 64
        decision = decide(count_rows(source, arcpy), count_rows(target, arcpy),
                          args.min_rows, args.max_swing,
                          args.allow_empty_target)
        decisions.append((source, target, decision))
        print("%s -> %s" % (source, target))
        for line in describe(decision):
            print(line)
        print("")

    if not all(d.allowed for _s, _t, d in decisions):
        print("At least one step was refused, so no step ran. "
              "Nothing was deleted.")
        return 1
    if not args.apply:
        print("Check only. %d target(s) were not touched." % len(decisions))
        return 0

    print("=== APPLY ===")
    for source, target, _decision in decisions:
        try:
            replace(source, target, arcpy, field_mapping=args.field_mapping)
        except Exception as exc:
            print("  FAILED: %s" % exc, file=sys.stderr)
            return 2
    print("\nReplaced %d target(s)." % len(decisions))
    return 0


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if args.scan:
        return _run_scan(args.scan)

    if args.plan:
        return _run_plan(args.plan, args)

    if not args.source or not args.target:
        print("error: --source and --target are both required. Use --self-test "
              "to verify the tool without a geodatabase.", file=sys.stderr)
        return 64
    if args.min_rows < 0 or args.max_swing < 0 or args.max_null_pct < 0:
        print("error: --min-rows, --max-swing and --max-null-pct cannot be "
              "negative.", file=sys.stderr)
        return 64

    arcpy = _import_arcpy()

    for label, path in (("source", args.source), ("target", args.target)):
        if not arcpy.Exists(path):
            print("error: %s does not exist: %s" % (label, path), file=sys.stderr)
            return 64

    staged = count_rows(args.source, arcpy)
    live = count_rows(args.target, arcpy)
    print("staged source: %d row(s)" % staged)
    print("live target:   %d row(s)" % live)
    print("")

    fields = [f.strip() for f in args.null_fields.split(",") if f.strip()]
    null_counts = count_nulls(args.source, fields, arcpy) if fields else None

    decision = decide(staged, live, args.min_rows, args.max_swing,
                      args.allow_empty_target, null_counts,
                      args.max_null_pct)
    for line in describe(decision):
        print(line)

    if not decision.allowed:
        print("\nNothing was deleted.")
        return 1

    if not args.apply:
        print("\nCheck only. The target was not touched.")
        print("Re-run with --apply to replace %d row(s) with %d." % (live, staged))
        return 0

    print("\n=== APPLY ===")
    try:
        replace(args.source, args.target, arcpy,
                field_mapping=args.field_mapping)
    except Exception as exc:
        print("  FAILED: %s" % exc, file=sys.stderr)
        return 2
    print("\nReplaced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
