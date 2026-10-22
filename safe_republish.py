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

--cover reads both field lists before the truncate. Append null-fills every
target field the staged data does not feed, drops every staged field the target
does not have, and under TEST rejects the mismatch only after the truncate has
already emptied the target. --transactional runs the clear and the append in one
edit session, so a failure rolls the target back. It is off by default, and it
has not been run against an enterprise geodatabase (see the README).

--scan finds the pattern in a tree of scripts, so you can learn which of them
need this before you have to wrap any of them. --plan decides a whole batch of
pairs before performing any of them.

    python safe_republish.py --self-test
    python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels
    python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels --apply
    python safe_republish.py --source s.gdb/Parcels --target p.sde/Parcels --null-fields PARCELID
    python safe_republish.py --source s.gdb/Parcels --target p.sde/Parcels --cover
    python safe_republish.py --source s.gdb/Parcels --target p.sde/Parcels --transactional --apply
    python safe_republish.py --scan C:/etl/scripts
    python safe_republish.py --plan nightly.txt --apply

Exit codes: 0 allowed, 1 refused, 2 the replace failed (with --transactional the
target was rolled back, and the message says whether that was confirmed),
64 usage error.
"""

from __future__ import print_function

import argparse
import collections
import io
import os
import re
import sys
import tempfile
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

# Field roles for --cover. "system" fields (object id, geometry, shape length and
# area) are skipped. "regenerated" fields (global id, editor tracking) are filled
# by the geodatabase on every append, so a staged value never lands in them.
ROLE_DATA = "data"
ROLE_SYSTEM = "system"
ROLE_REGENERATED = "regenerated"

# Lossless widenings only. A staged field of the key type may feed a target field
# of any listed type. BigInteger to Double is absent because a double cannot hold
# every 64 bit integer. Only SmallInteger to Integer was run on Pro (README).
WIDENING = {
    "SmallInteger": ("Integer", "BigInteger", "Single", "Double"),
    "Integer": ("BigInteger", "Double"),
    "Single": ("Double",),
}

COVER_OK = "OK"
COVER_NULLED = "NULLED"
COVER_TYPE = "TYPE"
COVER_LENGTH = "LENGTH"
COVER_REGENERATED = "REGENERATED"
COVER_DROPPED = "DROPPED"

Fld = collections.namedtuple("Fld", "name type length role")
Cover = collections.namedtuple("Cover", "status field detail refuses")


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
           max_null_pct=DEFAULT_MAX_NULL_PCT, cover=None):
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

    cover is the list cover_fields() returns, or None when --cover was not
    asked for. Every entry that refuses refuses the run, and every other entry
    that is not a plain match is reported.
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

    if cover is not None:
        matched = [c for c in cover if c.status == COVER_OK]
        reasons.append("field coverage: %d target field(s) are filled from the "
                       "staged data" % len(matched))
        for entry in cover:
            if entry.status == COVER_OK:
                continue
            if entry.refuses:
                verdict = REFUSE
            reasons.append("field %s %s: %s"
                           % (entry.field, entry.status, entry.detail))

    return Decision(verdict, reasons, staged, live, swing)


# --------------------------------------------------------------- field coverage

def _type_fits(staged_type, target_type):
    return staged_type == target_type or target_type in WIDENING.get(staged_type, ())


def cover_fields(target, staged, mapping=None, allow=(), strict=False):
    """Say, before the truncate, what the append will do to every target field.

    target and staged are lists of Fld. mapping is None to match fields by name,
    or {TARGET NAME: [staged field names]} read from a field map. Names compare
    without case, because Append does (README, experiment 1).

    Append null-fills a target field that nothing feeds. It drops a staged field
    the target lacks, converts text into a number without a word when it cannot
    (the value becomes 0), and skips without an error any row whose text is
    longer than the target field. All four were run on Pro. The truncate has
    already happened by the time any of them shows.

    strict is True when Append will run under TEST. TEST rejects a missing
    field, an extra field, a type mismatch, and a tracking or global id field
    the staged data lacks, so those refuse here and no allow list waives them:
    the append would fail anyway, after the truncate. TEST does not reject a
    short text field, so LENGTH stays waivable.

    allow names target fields whose finding the operator accepts. It cannot
    turn a refusal TEST would repeat into an allow.

    Returns a list of Cover(status, field, detail, refuses).
    """
    waive = set(name.strip().upper() for name in allow)
    by_name = dict((f.name.upper(), f) for f in staged)
    used = set()
    found = []

    def add(status, name, detail, hard=False):
        waived = name.upper() in waive and not hard
        if waived:
            detail += " (accepted by --cover-allow)"
        found.append(Cover(status, name, detail, not waived))

    for field in target:
        if field.role == ROLE_SYSTEM:
            continue
        if mapping is None:
            inputs = [field.name] if field.name.upper() in by_name else []
        else:
            inputs = mapping.get(field.name.upper(), [])
        used.update(name.upper() for name in inputs)
        feeder = by_name.get(inputs[0].upper()) if inputs else None

        if feeder is None and field.role == ROLE_REGENERATED:
            if strict:
                add(COVER_REGENERATED, field.name,
                    "is a tracking or global id field the staged data lacks, "
                    "and TEST rejects that after the truncate", hard=True)
            else:
                found.append(Cover(
                    COVER_REGENERATED, field.name,
                    "is refilled by the geodatabase, so its current values are "
                    "lost", False))
        elif feeder is None:
            add(COVER_NULLED, field.name,
                "is fed by nothing in the staged data, so every row will be "
                "NULL after the truncate" if not inputs else
                "is mapped from %s, which the staged data does not have"
                % inputs[0], hard=strict)
        elif not _type_fits(feeder.type, field.type):
            add(COVER_TYPE, field.name,
                "is %s in the target and %s in the staged data. Append cannot "
                "convert some values and writes 0 or NULL without an error"
                % (field.type, feeder.type), hard=strict)
        elif field.type == "String" and feeder.length > field.length:
            add(COVER_LENGTH, field.name,
                "holds text up to %d in the target and %d in the staged data. "
                "Append skips any row with longer text, with no error"
                % (field.length, feeder.length))
        else:
            found.append(Cover(COVER_OK, field.name, "", False))

    for field in staged:
        if field.role == ROLE_SYSTEM or field.name.upper() in used:
            continue
        found.append(Cover(
            COVER_DROPPED, field.name,
            "is in the staged data and not in the target, so Append drops it"
            + (" (TEST rejects this after the truncate)" if strict else ""),
            strict))
    return found


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


def describe_fields(path, arcpy):
    """The Fld list for a table, with each field's role tagged for --cover.

    The shape length and area fields and the tracking and global id fields are
    named by Describe, not by ListFields, so a name is looked up there. A
    Describe that lacks an attribute (a plain table has no shape length) just
    contributes nothing.
    """
    described = arcpy.Describe(path)
    roles = {}
    for attr, role in (("lengthFieldName", ROLE_SYSTEM),
                       ("areaFieldName", ROLE_SYSTEM),
                       ("globalIDFieldName", ROLE_REGENERATED),
                       ("creatorFieldName", ROLE_REGENERATED),
                       ("createdAtFieldName", ROLE_REGENERATED),
                       ("editorFieldName", ROLE_REGENERATED),
                       ("editedAtFieldName", ROLE_REGENERATED)):
        name = getattr(described, attr, "")
        if name:
            roles[name.upper()] = role
    fields = []
    for field in arcpy.ListFields(path):
        if field.type in ("OID", "Geometry"):
            role = ROLE_SYSTEM
        else:
            role = roles.get(field.name.upper(), ROLE_DATA)
        fields.append(Fld(field.name, field.type, field.length, role))
    return fields


def _same_table(left, right):
    return (os.path.normcase(os.path.normpath(left))
            == os.path.normcase(os.path.normpath(right)))


def read_mapping(text, source, arcpy):
    """Read an arcpy field map string into {TARGET NAME: [staged field names]}.

    arcpy parses the string, so the format is arcpy's and not guessed here. Only
    inputs that come from the source table count. The usual recipe adds the
    target table to the map first, and Append never reads from it, so a field
    that only the target table feeds is fed by nothing. The source has to be
    spelled as the map spells it, case and slashes aside; a mismatch feeds
    nothing and refuses, which is the safe way to be wrong.

    loadFromString does not complain about garbage on Pro 3.6. It reads the
    whole text as one output field with no input, so every real target field is
    then unfed and --cover refuses. A blank string raises ERROR 999999.
    """
    maps = arcpy.FieldMappings()
    maps.loadFromString(text)
    mapping = {}
    for index in range(maps.fieldCount):
        one = maps.getFieldMap(index)
        mapping[one.outputField.name.upper()] = [
            one.getInputFieldName(n) for n in range(one.inputFieldCount)
            if _same_table(one.getInputTableName(n), source)]
    return mapping


def workspace_of(path, arcpy):
    """The geodatabase that holds path. A feature class in a feature dataset is
    one level deeper than a feature class at the top."""
    parent = arcpy.Describe(path).path
    if getattr(arcpy.Describe(parent), "dataType", "") == "FeatureDataset":
        parent = arcpy.Describe(parent).path
    return parent


def clear_target(path, arcpy):
    """Empty the target, choosing the operation its versioning allows."""
    if is_versioned(path, arcpy):
        arcpy.management.DeleteRows(path)
        return "DeleteRows (target is versioned)"
    arcpy.management.TruncateTable(path)
    return "TruncateTable"


def _schema_for(schema_type, field_mapping):
    """Append ignores a field map under TEST, so a mapped call is sent NO_TEST.
    That is local to the call and leaves the configured default alone."""
    return "NO_TEST" if field_mapping else schema_type


def replace(source, target, arcpy, schema_type=APPEND_SCHEMA_TYPE,
            field_mapping=None, transactional=False):
    """Clear the target and append the source. Returns the final row count.

    field_mapping is the opaque arcpy field-map string, passed through so the
    tool can be dropped into the call sites that need one. transactional runs
    the whole thing in one edit session instead (replace_in_session).
    """
    if transactional:
        return replace_in_session(source, target, arcpy, schema_type,
                                  field_mapping)
    staged = count_rows(source, arcpy)
    how = clear_target(target, arcpy)
    print("  cleared target via %s" % how)
    schema_type = _schema_for(schema_type, field_mapping)
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


def replace_in_session(source, target, arcpy, schema_type=APPEND_SCHEMA_TYPE,
                       field_mapping=None):
    """Clear and refill the target inside one edit session.

    DeleteRows replaces TruncateTable here, because Pro refuses a truncate while
    editing (ERROR 160592). A failed Append, or a row count that is not the
    staged count, aborts the operation and discards the session, so the target
    keeps its rows. Run on a file and a mobile geodatabase only. An enterprise
    geodatabase has not been run, and the README says so.
    """
    staged = count_rows(source, arcpy)
    before = count_rows(target, arcpy)
    versioned = is_versioned(target, arcpy)
    editor = arcpy.da.Editor(workspace_of(target, arcpy))
    try:
        editor.startEditing(False, versioned)
    except Exception as exc:
        raise RuntimeError(
            "could not open an edit session on %s, so nothing was emptied: %s"
            % (target, exc))
    try:
        editor.startOperation()
        arcpy.management.DeleteRows(target)
        print("  emptied target via DeleteRows inside an edit session")
        arcpy.management.Append(source, target,
                                _schema_for(schema_type, field_mapping),
                                field_mapping)
        print("  appended %d row(s) from the staged source" % staged)
        if VERIFY_AFTER_APPEND:
            after = count_rows(target, arcpy)
            if after != staged:
                raise RuntimeError(
                    "ROW COUNT MISMATCH after append: staged %d, target now %d"
                    % (staged, after))
            print("  verified: target now holds %d row(s)" % after)
        editor.stopOperation()
        editor.stopEditing(True)
    except Exception as exc:
        # shortcut: a failed abort is swallowed, because the recount below is
        # the evidence and it is reported either way.
        for undo in (editor.abortOperation, lambda: editor.stopEditing(False)):
            try:
                undo()
            except Exception:
                pass
        restored = count_rows(target, arcpy)
        if restored == before:
            state = ("The edit session was rolled back and the target holds "
                     "its original %d row(s)." % before)
        else:
            state = ("The rollback could NOT be confirmed: the target holds "
                     "%d row(s) and held %d. Inspect it before re-running."
                     % (restored, before))
        raise RuntimeError("Replacing %s FAILED inside an edit session. %s "
                           "Cause: %s" % (target, state, exc))
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


class _FakeEditor(object):
    """arcpy.da.Editor, with a rollback that really restores the row counts."""

    def __init__(self, arcpy, workspace):
        self.arcpy = arcpy
        self.workspace = workspace
        self.log = []
        self.snapshot = None

    def startEditing(self, with_undo, multiuser):
        if self.arcpy.edit_error:
            raise RuntimeError(self.arcpy.edit_error)
        self.log.append(("startEditing", with_undo, multiuser))

    def startOperation(self):
        self.snapshot = dict(self.arcpy.counts)
        self.log.append(("startOperation",))

    def stopOperation(self):
        self.log.append(("stopOperation",))
        if self.arcpy.commit_error:
            raise RuntimeError(self.arcpy.commit_error)

    def abortOperation(self):
        self.log.append(("abortOperation",))
        if self.arcpy.abort_error:
            raise RuntimeError(self.arcpy.abort_error)
        if not self.arcpy.rollback_broken:
            self.arcpy.counts = dict(self.snapshot)

    def stopEditing(self, save):
        self.log.append(("stopEditing", save))
        if not save and not self.arcpy.rollback_broken:
            self.arcpy.counts = dict(self.snapshot)


class _FakeFieldMap(object):
    def __init__(self, out_name, inputs):
        self.outputField = type("_Out", (object,), {"name": out_name})()
        self.inputs = inputs
        self.inputFieldCount = len(inputs)

    def getInputFieldName(self, index):
        return self.inputs[index][1]

    def getInputTableName(self, index):
        return self.inputs[index][0]


class _FakeFieldMappings(object):
    """FieldMappings over a toy string: OUT=IN1|t:IN2;OUT2=  (an empty side is
    no input, and a "t:" prefix names the table the input comes from, "s" when
    absent). Like arcpy 3.6 it reads a part with no "=" as an output field of that
    name with no input. The real parse is arcpy's, and the README shows it run
    on Pro."""

    def __init__(self):
        self.maps = []
        self.fieldCount = 0

    def loadFromString(self, text):
        for part in text.split(";"):
            out, _eq, ins = part.partition("=")
            inputs = []
            for token in ins.split("|"):
                if token:
                    table, colon, name = token.rpartition(":")
                    inputs.append((table if colon else "s", name))
            self.maps.append(_FakeFieldMap(out, inputs))
        self.fieldCount = len(self.maps)

    def getFieldMap(self, index):
        return self.maps[index]


class _FakeArcpy(object):
    """The smallest arcpy the geodatabase layer can be driven against.

    Every function in this file already takes arcpy as an argument, so one stub
    serves all of them and the self-test still touches no geodatabase. It is
    also its own .management and .da, because nothing here needs those to be
    separate objects.

    describe_error is the shape that matters: a Describe that raises used to be
    read as "not versioned" and the target was truncated anyway.

    specs maps a path to its (name, type, length) fields for ListFields, and
    attrs maps a path to extra Describe attributes. append_skips is how many
    rows Append silently fails to write, which is what Pro does with text longer
    than the target field.
    """

    def __init__(self, counts=None, versioned=False, describe_error=None,
                 append_error=None, fields=(), rows=(), specs=None, attrs=None,
                 append_skips=0, edit_error=None, commit_error=None,
                 abort_error=None, rollback_broken=False):
        self.counts = dict(counts or {})
        self.versioned = versioned
        self.describe_error = describe_error
        self.append_error = append_error
        self.fields = list(fields)
        self.rows = list(rows)
        self.specs = dict(specs or {})
        self.attrs = dict(attrs or {})
        self.append_skips = append_skips
        self.edit_error = edit_error
        self.commit_error = commit_error
        self.abort_error = abort_error
        self.rollback_broken = rollback_broken
        self.calls = []
        self.editors = []
        self.management = self
        self.da = self

    def Exists(self, path):
        return path in self.counts

    def GetCount(self, path):
        return [str(self.counts.get(path, 0))]

    def Describe(self, path):
        if self.describe_error:
            raise RuntimeError(self.describe_error)
        attrs = {"isVersioned": self.versioned, "path": ""}
        attrs.update(self.attrs.get(path, {}))
        return type("_Described", (object,), attrs)()

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
        self.counts[target] = self.counts.get(source, 0) - self.append_skips

    def ListFields(self, path):
        if path in self.specs:
            return [type("_Field", (object,),
                         {"name": n, "type": t, "length": ln})()
                    for n, t, ln in self.specs[path]]
        return [type("_Field", (object,), {"name": name})()
                for name in self.fields]

    def SearchCursor(self, path, field_names):
        columns = [self.fields.index(name) for name in field_names]
        return _FakeCursor([tuple(row[i] for i in columns)
                            for row in self.rows])

    def FieldMappings(self):
        return _FakeFieldMappings()

    def Editor(self, workspace):
        editor = _FakeEditor(self, workspace)
        self.editors.append(editor)
        return editor


class _Harness(object):
    """The check()/raises() recorder, a class so the self-test can test it."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label):
        try:
            fn()
        except ValueError:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def footer(self):
        """Print the closing line and return the process exit status."""
        print("-" * 68)
        total = self.passed + len(self.failed)
        if self.failed:
            print("%d assertions, %d failed" % (total, len(self.failed)))
            for label in self.failed:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0


def self_test():
    """Assertions over the decision core. No arcpy, no geodatabase, no network."""
    harness = _Harness()
    check = harness.check
    raises = harness.raises

    KEEP = object()
    MISSING = object()

    def set_arcpy(value):
        """Put value in sys.modules as arcpy (MISSING removes it); return the old one."""
        old = sys.modules.get("arcpy", MISSING)
        if value is MISSING:
            sys.modules.pop("arcpy", None)
        else:
            sys.modules["arcpy"] = value
        return old

    class _Quiet(object):
        """Capture stdout and stderr, and put a stub arcpy in sys.modules."""

        def __init__(self, stub):
            self.stub = stub

        def __enter__(self):
            self.saved = (sys.stdout, sys.stderr)
            sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
            self.old = KEEP if self.stub is None else set_arcpy(self.stub)
            return self

        def __exit__(self, *exc_info):
            self.out, self.err = sys.stdout.getvalue(), sys.stderr.getvalue()
            sys.stdout, sys.stderr = self.saved
            if self.old is not KEEP:
                set_arcpy(self.old)
            return False

    def error_of(fn, kind=RuntimeError):
        """The message of the error fn raises, or "" when it raises none."""
        try:
            fn()
        except kind as exc:
            return str(exc) or kind.__name__
        return ""

    def exits(fn):
        """True when fn ends in SystemExit, which is how argparse refuses."""
        with _Quiet(None):
            try:
                fn()
            except SystemExit:
                return True
        return False

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
    prefix_refused = exits(lambda: _parse(["--source", "s", "--target", "t", "--ap"]))
    check(prefix_refused,
          "a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect")
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
    message = error_of(lambda: clear_target("prod.sde/Parcels", fake))
    check("prod.sde/Parcels" in message,
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
    message = error_of(lambda: replace("s", "t", fake))
    check("t" in message, "a failed Append names the target it cleared")
    check("EMPTY" in message,
          "a failed Append says the target is empty, not only that it failed")
    check("TruncateTable" in message,
          "a failed Append says how the target was cleared")
    fake = _FakeArcpy(counts={"s": 100, "t": 100}, append_skips=1)
    check("MISMATCH" in error_of(lambda: replace("s", "t", fake)),
          "a plain replace whose append skipped a row reports the count mismatch")
    check(repr(decide(100, 100)) == "Decision(ALLOW, staged=100, live=100, swing=0.0)",
          "a decision has a readable repr")

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

    # ---- --cover: what the append will do to each field, decided before the truncate
    def F(name, kind="String", length=50, role=ROLE_DATA):
        return Fld(name, kind, length, role)

    def statuses(found):
        return dict((c.field, c.status) for c in found)

    sys_fields = [F("OBJECTID", "OID", 4, ROLE_SYSTEM),
                  F("Shape", "Geometry", 0, ROLE_SYSTEM)]
    TARGET = sys_fields + [F("NAME"), F("VALUE", "Integer", 4),
                           F("NOTES", length=20)]
    STAGED = sys_fields + [F("NAME"), F("VALUE", "Integer", 4)]

    found = cover_fields(TARGET, STAGED)
    check(statuses(found)["NOTES"] == COVER_NULLED,
          "a target field nothing feeds is NULLED")
    check([c for c in found if c.field == "NOTES"][0].refuses,
          "a NULLED field refuses the run  <-- pinned defect")
    check("OBJECTID" not in statuses(found) and "Shape" not in statuses(found),
          "object id and geometry are system fields and are not judged")
    check(statuses(found)["NAME"] == COVER_OK and statuses(found)["VALUE"] == COVER_OK,
          "fields the staged data feeds are OK")
    found = cover_fields(TARGET, [F("name"), F("value", "Integer", 4), F("notes", length=20)])
    check(set(statuses(found).values()) == set([COVER_OK]),
          "field names match without regard to case, as Append matches them")
    check(not any(c.refuses for c in found), "a full match refuses nothing")

    found = cover_fields(TARGET, [F("NAME"), F("VALUE", "String", 50), F("NOTES", length=20)])
    check(statuses(found)["VALUE"] == COVER_TYPE and [c for c in found if c.field == "VALUE"][0].refuses,
          "text feeding an integer field is a TYPE refusal  <-- pinned defect")
    one = [F("A", "Integer", 4)]
    check(cover_fields(one, [F("A", "SmallInteger", 2)])[0].status == COVER_OK,
          "a SmallInteger may feed an Integer field")
    check(cover_fields(one, [F("A", "BigInteger", 8)])[0].status == COVER_TYPE,
          "a BigInteger may not feed an Integer field")
    check(cover_fields([F("A", "SmallInteger", 2)], [F("A", "Integer", 4)])[0].status == COVER_TYPE,
          "an Integer may not feed a SmallInteger field")
    check(cover_fields([F("A", "Double", 8)], [F("A", "Single", 4)])[0].status == COVER_OK,
          "a Single may feed a Double field")
    check(cover_fields([F("A", "Double", 8)], [F("A", "BigInteger", 8)])[0].status == COVER_TYPE,
          "a BigInteger may not feed a Double field, a double cannot hold every one")
    check(cover_fields([F("A", "Double", 8)], [F("A", "Double", 8)])[0].status == COVER_OK,
          "a Double feeds a Double and its length is not compared")

    found = cover_fields([F("A", length=20)], [F("A", length=50)])
    check(found[0].status == COVER_LENGTH and found[0].refuses,
          "text declared longer than the target field is a LENGTH refusal  <-- pinned defect")
    check(cover_fields([F("A", length=50)], [F("A", length=50)])[0].status == COVER_OK,
          "equal text lengths are OK, the limit is inclusive")
    check(cover_fields([F("A", length=50)], [F("A", length=10)])[0].status == COVER_OK,
          "shorter staged text is OK")

    found = cover_fields(TARGET, STAGED, allow=["notes"])
    notes = [c for c in found if c.field == "NOTES"][0]
    check(not notes.refuses and "accepted by --cover-allow" in notes.detail,
          "--cover-allow accepts a NULLED field and says so")
    check(not any(c.refuses for c in cover_fields(TARGET, STAGED, allow=[" Notes "])),
          "--cover-allow ignores case and spaces")
    check(cover_fields([F("A", length=20)], [F("A", length=50)], allow=["A"])[0].refuses is False,
          "--cover-allow accepts a LENGTH finding")

    found = cover_fields(TARGET, STAGED + [F("EXTRA")])
    extra = [c for c in found if c.status == COVER_DROPPED][0]
    check(extra.field == "EXTRA" and not extra.refuses,
          "a staged field the target lacks is DROPPED, reported, and not a refusal under NO_TEST")
    check(cover_fields([F("A")], [F("A"), F("X", "OID", 4, ROLE_SYSTEM)]) ==
          [Cover(COVER_OK, "A", "", False)],
          "a staged system field is not reported as dropped")

    found = cover_fields(TARGET, STAGED, strict=True)
    check([c for c in found if c.field == "NOTES"][0].refuses,
          "under TEST a NULLED field refuses")
    check([c for c in cover_fields(TARGET, STAGED, allow=["NOTES"], strict=True)
           if c.field == "NOTES"][0].refuses,
          "--cover-allow cannot waive a NULLED field under TEST, the append fails after the truncate  <-- pinned defect")
    check([c for c in cover_fields(TARGET, STAGED + [F("EXTRA")], strict=True)
           if c.status == COVER_DROPPED][0].refuses,
          "under TEST a DROPPED field refuses, TEST rejects an extra staged field  <-- pinned defect")
    check(cover_fields([F("A", "Integer", 4)], [F("A")], allow=["A"], strict=True)[0].refuses,
          "--cover-allow cannot waive a TYPE finding under TEST")
    check(cover_fields([F("A", length=20)], [F("A", length=50)], allow=["A"], strict=True)[0].refuses is False,
          "--cover-allow can waive a LENGTH finding even under TEST, TEST does not reject it")

    TRACK = [F("CREATED_BY", "String", 255, ROLE_REGENERATED),
             F("GlobalID", "GlobalID", 38, ROLE_REGENERATED)]
    found = cover_fields([F("A")] + TRACK, [F("A")])
    check([c.status for c in found if c.field == "CREATED_BY"] == [COVER_REGENERATED]
          and not any(c.refuses for c in found),
          "tracking and global id fields are REGENERATED and reported, not refused, under NO_TEST")
    found = cover_fields([F("A")] + TRACK, [F("A")], strict=True)
    check(all(c.refuses for c in found if c.status == COVER_REGENERATED),
          "tracking and global id fields the staged data lacks refuse under TEST  <-- pinned defect")
    check(all(c.refuses for c in cover_fields([F("A")] + TRACK, [F("A")], allow=["CREATED_BY"], strict=True)
              if c.status == COVER_REGENERATED),
          "--cover-allow cannot waive a REGENERATED field under TEST")
    check(not any(c.refuses for c in cover_fields([F("A")] + TRACK, [F("A")] + TRACK, strict=True)),
          "tracking fields present in the staged data are matched like any other")

    # a field map decides the feeders, not the names
    maps = {"NAME": ["FULLNAME"], "VALUE": ["VALUE"], "NOTES": []}
    found = cover_fields(TARGET, sys_fields + [F("FULLNAME"), F("VALUE", "Integer", 4), F("NAME")], maps)
    check([c.status for c in found if c.field == "NAME" and c.status != COVER_DROPPED] == [COVER_OK],
          "a mapped target field is fed by the field it names")
    check(statuses(found)["NOTES"] == COVER_NULLED and "nothing" in
          [c for c in found if c.field == "NOTES"][0].detail,
          "a mapped output with no input is NULLED")
    check([c.field for c in found if c.status == COVER_DROPPED] == ["NAME"],
          "a staged field the map never reads is DROPPED, even with the same name as a target field")
    found = cover_fields(TARGET, STAGED, {"NAME": ["NAME"], "VALUE": ["VALUE"]})
    check(statuses(found)["NOTES"] == COVER_NULLED,
          "a target field the map does not mention is NULLED")
    found = cover_fields(TARGET, STAGED, {"NAME": ["GONE"], "VALUE": ["VALUE"], "NOTES": ["NAME"]})
    gone = [c for c in found if c.field == "NAME"][0]
    check(gone.status == COVER_NULLED and "GONE" in gone.detail and gone.refuses,
          "a map reading a field the staged data lacks is NULLED and names the missing field")
    check(all(c.status == COVER_NULLED for c in cover_fields(TARGET, STAGED, {})
              if c.status != COVER_DROPPED),
          "an empty field map feeds nothing")

    # the verdict
    found = cover_fields(TARGET, STAGED)
    d = decide(100, 100, cover=found)
    check(not d.allowed and any("NOTES NULLED" in r for r in d.reasons),
          "decide refuses when --cover refuses and names the field")
    check(any(r.startswith("field coverage: 2") for r in d.reasons),
          "decide reports how many target fields are filled")
    check(decide(100, 100, cover=cover_fields(TARGET, STAGED, allow=["NOTES"])).allowed,
          "decide allows once the finding is accepted")
    d = decide(100, 100, cover=cover_fields(TARGET, STAGED + [F("EXTRA")], allow=["NOTES"]))
    check(d.allowed and any("EXTRA DROPPED" in r for r in d.reasons),
          "decide reports a dropped staged field and still allows it under NO_TEST")
    check(decide(100, 100, cover=[]).allowed, "an empty coverage list allows")
    check(not decide(10, 100, cover=cover_fields(TARGET, STAGED, allow=["NOTES"])).allowed,
          "coverage never overrides the swing check")

    # ---- reading fields and field maps out of arcpy
    specs = {"t": [("OBJECTID", "OID", 4), ("Shape", "Geometry", 0),
                   ("NAME", "String", 50), ("GlobalID", "GlobalID", 38),
                   ("CREATED_BY", "String", 255), ("Shape_Length", "Double", 8)],
             "plain": [("A", "String", 5)]}
    attrs = {"t": {"lengthFieldName": "Shape_Length", "globalIDFieldName": "GlobalID",
                   "creatorFieldName": "CREATED_BY", "areaFieldName": ""}}
    fake = _FakeArcpy(specs=specs, attrs=attrs)
    roles = dict((f.name, f.role) for f in describe_fields("t", fake))
    check(roles == {"OBJECTID": ROLE_SYSTEM, "Shape": ROLE_SYSTEM,
                    "NAME": ROLE_DATA, "GlobalID": ROLE_REGENERATED,
                    "CREATED_BY": ROLE_REGENERATED, "Shape_Length": ROLE_SYSTEM},
          "fields are tagged system, regenerated or data from Describe")
    check(describe_fields("plain", fake) == [Fld("A", "String", 5, ROLE_DATA)],
          "a table whose Describe names no special field has only data fields")
    fake = _FakeArcpy(describe_error="ERROR 000732")
    check(error_of(lambda: describe_fields("t", fake)) != "",
          "a Describe that fails while reading fields raises, it is not an empty list")
    check(read_mapping("NAME=FULLNAME;NOTES=;SUM=A|B", "s", _FakeArcpy()) ==
          {"NAME": ["FULLNAME"], "NOTES": [], "SUM": ["A", "B"]},
          "a field map string is read into target name and staged field names")
    check(read_mapping("NAME=t:NAME|s:FULLNAME;NOTES=t:NOTES", "s", _FakeArcpy()) ==
          {"NAME": ["FULLNAME"], "NOTES": []},
          "an input that comes from the target table feeds nothing  <-- pinned defect")
    check(_same_table("db.gdb//Parcels", "db.gdb/./Parcels")
          and not _same_table("db.gdb/Parcels", "db.gdb/Other"),
          "table paths compare after normalising their slashes")
    check(read_mapping("garbage", "s", _FakeArcpy()) == {"GARBAGE": []},
          "garbage is read as one output field with no input, so nothing real is fed  <-- pinned defect")
    fake = _FakeArcpy(attrs={"db.gdb/Parcels": {"path": "db.gdb"},
                             "db.gdb/fds/Parcels": {"path": "db.gdb/fds"},
                             "db.gdb/fds": {"path": "db.gdb", "dataType": "FeatureDataset"}})
    check(workspace_of("db.gdb/Parcels", fake) == "db.gdb",
          "the workspace of a top level feature class is its parent")
    check(workspace_of("db.gdb/fds/Parcels", fake) == "db.gdb",
          "the workspace of a feature class in a feature dataset is one level up")

    # ---- --transactional: the clear and the append in one edit session
    fake = _FakeArcpy(counts={"s": 100, "t": 80}, attrs={"t": {"path": "ws"}})
    check(replace("s", "t", fake, transactional=True) == 100,
          "a transactional replace returns the staged count")
    check([c[0] for c in fake.calls] == ["DeleteRows", "Append"],
          "a transactional replace uses DeleteRows and never TruncateTable, Pro refuses a truncate while editing")
    log = [e[0] for e in fake.editors[0].log]
    check(log == ["startEditing", "startOperation", "stopOperation", "stopEditing"]
          and fake.editors[0].log[-1] == ("stopEditing", True),
          "a successful session stops the operation and saves the edits")
    check(fake.editors[0].log[0] == ("startEditing", False, False) and fake.editors[0].workspace == "ws",
          "the session opens on the target's workspace, with no undo and no multiuser mode")
    check(fake.counts["t"] == 100, "the target holds the staged rows after a committed session")
    fake = _FakeArcpy(counts={"s": 100, "t": 80}, versioned=True)
    replace_in_session("s", "t", fake)
    check(fake.editors[0].log[0] == ("startEditing", False, True),
          "a versioned target opens the session in multiuser mode")
    fake = _FakeArcpy(counts={"s": 5, "t": 5})
    replace_in_session("s", "t", fake, field_mapping="NAME=NAME")
    check(fake.calls[-1][2] == "NO_TEST", "a field map sends the session's append NO_TEST too")

    fake = _FakeArcpy(counts={"s": 100, "t": 80}, append_error="ERROR 000466: schema does not match")
    message = error_of(lambda: replace("s", "t", fake, transactional=True))
    check("rolled back" in message and "original 80" in message,
          "a failed Append in an edit session is rolled back and the target keeps its 80 rows  <-- pinned defect")
    check("000466" in message, "the rollback message carries the cause")
    check(fake.counts["t"] == 80, "the target still holds its rows after the failed session")
    check(fake.editors[0].log[-2:] == [("abortOperation",), ("stopEditing", False)],
          "a failed session aborts the operation and discards the edits")
    check(("stopEditing", True) not in fake.editors[0].log, "a failed session never saves")
    fake = _FakeArcpy(counts={"s": 100, "t": 80}, append_error="boom")
    error_of(lambda: replace("s", "t", fake))
    check(fake.counts["t"] == 0,
          "the plain replace leaves the target EMPTY after the same failure, which is what --transactional is for")

    fake = _FakeArcpy(counts={"s": 100, "t": 80}, append_skips=1)
    message = error_of(lambda: replace_in_session("s", "t", fake))
    check("MISMATCH" in message and "rolled back" in message,
          "an append that silently skips a row is rolled back, not saved  <-- pinned defect")
    check(fake.counts["t"] == 80, "the target keeps its rows after the mismatch")
    fake = _FakeArcpy(counts={"s": 100, "t": 80}, commit_error="disk full")
    message = error_of(lambda: replace_in_session("s", "t", fake))
    check("rolled back" in message and "disk full" in message,
          "a commit that fails is rolled back")
    fake = _FakeArcpy(counts={"s": 100, "t": 80}, append_error="boom",
                      rollback_broken=True)
    message = error_of(lambda: replace_in_session("s", "t", fake))
    check("NOT be confirmed" in message and "holds 0" in message,
          "a rollback that cannot be confirmed says so and gives both counts  <-- pinned defect")
    fake = _FakeArcpy(counts={"s": 100, "t": 80}, append_error="boom",
                      abort_error="no operation")
    message = error_of(lambda: replace_in_session("s", "t", fake))
    check("rolled back" in message and "original 80" in message,
          "a failed abort is swallowed and the recount still reports the rolled back target")
    fake = _FakeArcpy(counts={"s": 100, "t": 80}, edit_error="workspace is locked")
    message = error_of(lambda: replace_in_session("s", "t", fake))
    check("nothing was emptied" in message and "locked" in message,
          "a session that will not open says nothing was emptied")
    check(fake.calls == [], "no row was touched when the session would not open")
    fake = _FakeArcpy(counts={"s": 100, "t": 80}, describe_error="ERROR 000732")
    message = error_of(lambda: replace_in_session("s", "t", fake))
    check("nothing will be emptied" in message,
          "a target nobody can describe is not edited transactionally either")
    check(fake.calls == [] and fake.editors == [], "and no session was opened for it")

    globals()["VERIFY_AFTER_APPEND"] = False
    try:
        fake = _FakeArcpy(counts={"s": 100, "t": 80}, append_skips=1)
        replace("s", "t", fake)
        replace_in_session("s", "t", fake)
        check(True, "with VERIFY_AFTER_APPEND off neither replace checks the count")
    finally:
        globals()["VERIFY_AFTER_APPEND"] = True

    # ---- the command line, run through main() against the stub arcpy
    def cli(argv, stub):
        with _Quiet(stub) as q:
            rc = main(argv)
        return rc, q.out, q.err

    def pair(**kwargs):
        return _FakeArcpy(counts={"s": 100, "t": 100}, **kwargs)

    BASE = ["--source", "s", "--target", "t"]
    rc, out, err = cli([], pair())
    check(rc == 64 and "both required" in err, "no source and target is a usage error")
    rc, out, err = cli(BASE + ["--min-rows", "-1"], pair())
    check(rc == 64 and "cannot be negative" in err, "a negative limit is a usage error")
    rc, out, err = cli(BASE + ["--cover-allow", "NOTES"], pair())
    check(rc == 64 and "needs --cover" in err, "--cover-allow without --cover is a usage error, never ignored")
    rc, out, err = cli(["--source", "nope", "--target", "t"], pair())
    check(rc == 64 and "source does not exist" in err, "a missing source is a usage error")
    fake = pair()
    rc, out, err = cli(BASE, fake)
    check(rc == 0 and "Check only" in out and fake.calls == [],
          "an allowed check without --apply touches nothing")
    fake = pair()
    fake.counts["s"] = 10
    rc, out, err = cli(BASE + ["--apply"], fake)
    check(rc == 1 and "Nothing was deleted" in out and fake.calls == [],
          "a refused run with --apply still deletes nothing")
    fake = pair()
    rc, out, err = cli(BASE + ["--apply"], fake)
    check(rc == 0 and [c[0] for c in fake.calls] == ["TruncateTable", "Append"] and "Replaced." in out,
          "--apply truncates and appends an allowed run")
    fake = pair(append_error="boom")
    rc, out, err = cli(BASE + ["--apply"], fake)
    check(rc == 2 and "EMPTY" in err, "a failed append exits 2 and says the target is empty")
    fake = pair(append_error="boom")
    rc, out, err = cli(BASE + ["--apply", "--transactional"], fake)
    check(rc == 2 and "rolled back" in err and fake.counts["t"] == 100,
          "a failed append under --transactional exits 2, says rolled back, and the target keeps its rows")
    fake = pair()
    rc, out, err = cli(BASE + ["--apply", "--transactional"], fake)
    check(rc == 0 and ("DeleteRows", "t") in fake.calls and ("TruncateTable", "t") not in fake.calls,
          "--transactional --apply clears with DeleteRows inside the session")
    fake = pair()
    rc, out, err = cli(BASE + ["--transactional"], fake)
    check(rc == 0 and fake.calls == [] and fake.editors == [],
          "--transactional without --apply opens no session and touches nothing")

    SPECS = {"s": [("OBJECTID", "OID", 4), ("NAME", "String", 50)],
             "t": [("OBJECTID", "OID", 4), ("NAME", "String", 50), ("NOTES", "String", 20)]}
    fake = pair(specs=SPECS)
    rc, out, err = cli(BASE + ["--cover", "--apply"], fake)
    check(rc == 1 and "NOTES NULLED" in out and fake.calls == [],
          "--cover refuses an unfed target field before the truncate and --apply deletes nothing  <-- pinned defect")
    fake = pair(specs=SPECS)
    rc, out, err = cli(BASE + ["--cover", "--cover-allow", "NOTES", "--apply"], fake)
    check(rc == 1 and fake.calls == [],
          "--cover-allow does not rescue a run TEST would fail, the default schema type is TEST")
    fake = pair(specs=SPECS)
    rc, out, err = cli(BASE + ["--cover", "--cover-allow", "NOTES", "--field-mapping", "NAME=NAME;NOTES="], fake)
    check(rc == 0 and "accepted by --cover-allow" in out,
          "with a field map the run is NO_TEST and --cover-allow can accept a NULLED field")
    fake = pair(specs=SPECS)
    rc, out, err = cli(BASE + ["--cover", "--field-mapping", "NAME=NAME;NOTES=", "--apply"], fake)
    check(rc == 1 and fake.calls == [], "the same map without --cover-allow refuses")
    fake = pair(specs=SPECS)
    rc, out, err = cli(BASE + ["--cover", "--field-mapping", "garbage"], fake)
    check(rc == 1 and "NOTES NULLED" in out and "NAME NULLED" in out and fake.calls == [],
          "a field map that feeds no target field refuses, it does not run unchecked")
    fake = pair(describe_error="ERROR 000732")
    rc, out, err = cli(BASE + ["--cover"], fake)
    check(rc == 1 and "could not read" in out, "--cover refuses when a field list cannot be read")
    fake = pair(specs={"s": SPECS["s"], "t": SPECS["t"][:2]})
    rc, out, err = cli(BASE + ["--cover", "--apply"], fake)
    check(rc == 0 and "coverage: 1" in out and fake.calls[0][0] == "TruncateTable",
          "--cover allows and --apply runs when every target field is fed")
    fake = pair(fields=["PARCELID"], rows=[("1",), ("2",)])
    rc, out, err = cli(BASE + ["--null-fields", "NOSUCH"], fake)
    check(rc == 1 and "NOSUCH" in out, "--null-fields on a missing field refuses through main")

    # ---- --plan through main(): the same gates for every pair
    def plan_cli(text, stub, extra=()):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.write(text)
        handle.close()
        try:
            return cli(["--plan", handle.name] + list(extra), stub)
        finally:
            os.remove(handle.name)

    def two(**kwargs):
        return _FakeArcpy(counts={"s1": 10, "t1": 10, "s2": 20, "t2": 20}, **kwargs)

    rc, out, err = cli(["--plan", os.path.join(tempfile.gettempdir(), "no-such-plan.txt")], two())
    check(rc == 64 and "error:" in err, "a plan file that does not exist is a usage error")
    rc, out, err = plan_cli("# only a comment\n", two())
    check(rc == 64 and "no pairs" in err, "a plan with no pairs is a usage error")
    rc, out, err = plan_cli("a b\n", two())
    check(rc == 64, "a malformed plan line is a usage error")
    rc, out, err = plan_cli("s1,t1\ns2,t1\n", two())
    check(rc == 1 and "named twice" in out, "a plan naming a target twice is refused before anything is read")
    rc, out, err = plan_cli("s1,t1\nsX,t2\n", two())
    check(rc == 64 and "source does not exist" in err, "a plan naming a missing path is a usage error")
    fake = two()
    rc, out, err = plan_cli("s1,t1\ns2,t2\n", fake)
    check(rc == 0 and "Check only. 2 target(s)" in out and fake.calls == [],
          "a plan check without --apply touches nothing")
    fake = two()
    fake.counts["s2"] = 1
    rc, out, err = plan_cli("s1,t1\ns2,t2\n", fake, ["--apply"])
    check(rc == 1 and "no step ran" in out and fake.calls == [],
          "one refused step stops the whole plan")
    fake = two()
    rc, out, err = plan_cli("s1,t1\ns2,t2\n", fake, ["--apply"])
    check(rc == 0 and "Replaced 2 target(s)" in out and len([c for c in fake.calls if c[0] == "Append"]) == 2,
          "a plan with --apply replaces every target")
    fake = two()
    rc, out, err = plan_cli("s1,t1\ns2,t2\n", fake, ["--apply", "--transactional"])
    check(rc == 0 and len(fake.editors) == 2 and ("TruncateTable", "t1") not in fake.calls,
          "a plan with --transactional gives each target its own session")
    fake = two(append_error="boom")
    rc, out, err = plan_cli("s1,t1\ns2,t2\n", fake, ["--apply"])
    check(rc == 2 and "FAILED" in err, "a plan whose replace fails exits 2")
    fake = two(fields=["PARCELID"], rows=[(None,), (None,)])
    rc, out, err = plan_cli("s1,t1\n", fake, ["--null-fields", "PARCELID"])
    check(rc == 1 and "NULL or blank" in out,
          "--null-fields applies to every plan pair, it used to be ignored  <-- pinned defect")
    fake = two(describe_error="ERROR 000732")
    rc, out, err = plan_cli("s1,t1\n", fake, ["--cover"])
    check(rc == 1 and "could not read" in out, "a plan pair whose fields cannot be read refuses the plan")

    # ---- --scan through main(), on real files in a temporary tree
    root = tempfile.mkdtemp()
    try:
        rc, out, err = cli(["--scan", os.path.join(root, "absent")], _FakeArcpy())
        check(rc == 64 and "needs a directory" in err, "--scan on a missing directory is a usage error")
        rc, out, err = cli(["--scan", root], _FakeArcpy())
        check(rc == 0 and "0 file(s)" in out, "--scan on an empty tree finds nothing and exits 0")
        with open(os.path.join(root, "counts.py"), "w") as handle:
            handle.write("n = arcpy.GetCount_management(s)\n"
                         "arcpy.TruncateTable_management(t)\n"
                         "arcpy.Append_management(s, t)\n")
        with open(os.path.join(root, "plain.py"), "w") as handle:
            handle.write("x = 1\n")
        with open(os.path.join(root, "notes.txt"), "w") as handle:
            handle.write("arcpy.TruncateTable_management(t)\n")
        rc, out, err = cli(["--scan", root], _FakeArcpy())
        check(rc == 0 and "1 file(s), 1 truncate-then-append" in out and "counts " in out,
              "--scan reads python files only and exits 0 when every pair counts rows")
        with open(os.path.join(root, "blind.py"), "w") as handle:
            handle.write("arcpy.DeleteRows_management(t)\n"
                         "arcpy.Append_management(s, t)\n")
        rc, out, err = cli(["--scan", root], _FakeArcpy())
        check(rc == 1 and "BLIND " in out and "1 file(s) and 1 site(s) count no rows" in out
              and "schema_type not on the call line" in out,
              "--scan exits 1 and names a file that counts no rows")

        def refusing_open(*args, **kwargs):
            raise IOError("denied")

        globals()["open"] = refusing_open
        try:
            check(scan_tree(root) == [], "a file that cannot be read is skipped, not fatal")
        finally:
            del globals()["open"]
    finally:
        for name in os.listdir(root):
            os.remove(os.path.join(root, name))
        os.rmdir(root)

    # ---- arcpy is imported only when it is needed
    held = set_arcpy(None)
    exited = error_of(_import_arcpy, SystemExit)
    stub = _FakeArcpy()
    set_arcpy(stub)
    returned = _import_arcpy()
    set_arcpy(MISSING)
    gone = "arcpy" not in sys.modules
    set_arcpy(held)
    check("arcpy was not found" in exited and "Only --self-test" in exited,
          "a missing arcpy exits with the Pro Python path")
    check(returned is stub, "an importable arcpy is returned")
    check(gone and sys.modules.get("arcpy", MISSING) is held,
          "the stub arcpy is removed and the real slot is put back after a test")

    # ---- the new flags on the parser
    a = _parse(BASE)
    check(a.cover is False and a.cover_allow == "" and a.transactional is False,
          "--cover, --cover-allow and --transactional default to off")
    a = _parse(BASE + ["--cover", "--cover-allow", "A,B", "--transactional"])
    check(a.cover and a.cover_allow == "A,B" and a.transactional, "the new flags are read")
    for flag in ("--trans", "--transaction", "--cov", "--cover-al"):
        check(exits(lambda: _parse(BASE + [flag])), "a prefix %s of a new flag is REFUSED by the parser  <-- pinned defect" % flag)

    # ---- the self-test helpers
    check(error_of(lambda: None) == "", "error_of reports no error as an empty message")
    check(exits(lambda: None) is False, "exits reports a call that does not exit as False")

    # ---- the recorder records a failure
    inner = _Harness()
    with _Quiet(None) as q:
        inner.check(False, "a failing check")
        inner.raises(lambda: None, "no error")
        inner.raises(lambda: 1 // 0, "wrong error")
        inner.raises(lambda: int("x"), "the right error")
        status = inner.footer()
    check(status == 1 and len(inner.failed) == 3 and inner.passed == 1,
          "check() and raises() really do record a failure  <-- pinned defect")
    check("4 assertions, 3 failed" in q.out and "FAILED: no error" in q.out,
          "the footer reports failures by count and by name, and exits 1  <-- pinned defect")
    with _Quiet(None) as q:
        status = _Harness().footer()
    check(status == 0 and "0 assertions, 0 failed" in q.out, "an empty run reports zero failures and exits 0")

    return harness.footer()


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="safe_republish.py",
        allow_abbrev=False,
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
    ap.add_argument("--cover", action="store_true",
                    help="before the truncate, compare the staged and target "
                         "field lists (or the --field-mapping) and refuse a "
                         "target field nothing feeds, a type Append cannot "
                         "convert, or text that would make Append skip rows.")
    ap.add_argument("--cover-allow", dest="cover_allow", default="",
                    help="comma separated target fields whose --cover finding "
                         "you accept, for example a field left NULL on "
                         "purpose. Needs --cover. It cannot waive what TEST "
                         "would reject after the truncate.")
    ap.add_argument("--transactional", action="store_true",
                    help="clear and refill the target in one edit session, so "
                         "a failure rolls it back. Off by default. Run on file "
                         "and mobile geodatabases only, not on enterprise.")
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


def _gather(source, target, args, arcpy):
    """Read both sides and decide. Returns (staged, live, decision).

    Shared by the single run and by --plan, so a gate asked for on the command
    line applies to every pair. Before this, --plan ignored --null-fields.
    """
    staged = count_rows(source, arcpy)
    live = count_rows(target, arcpy)
    fields = [f.strip() for f in args.null_fields.split(",") if f.strip()]
    null_counts = count_nulls(source, fields, arcpy) if fields else None
    cover = None
    if args.cover:
        mapping = (read_mapping(args.field_mapping, source, arcpy)
                   if args.field_mapping else None)
        cover = cover_fields(
            describe_fields(target, arcpy), describe_fields(source, arcpy),
            mapping, args.cover_allow.split(","),
            strict=APPEND_SCHEMA_TYPE == "TEST" and not args.field_mapping)
    decision = decide(staged, live, args.min_rows, args.max_swing,
                      args.allow_empty_target, null_counts,
                      args.max_null_pct, cover)
    return staged, live, decision


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
        try:
            _staged, _live, decision = _gather(source, target, args, arcpy)
        except Exception as exc:
            print("REFUSED: could not read %s -> %s: %s"
                  % (source, target, exc))
            print("Nothing was deleted.")
            return 1
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
            replace(source, target, arcpy, field_mapping=args.field_mapping,
                    transactional=args.transactional)
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
    if args.cover_allow and not args.cover:
        print("error: --cover-allow needs --cover.", file=sys.stderr)
        return 64

    arcpy = _import_arcpy()

    for label, path in (("source", args.source), ("target", args.target)):
        if not arcpy.Exists(path):
            print("error: %s does not exist: %s" % (label, path), file=sys.stderr)
            return 64

    try:
        staged, live, decision = _gather(args.source, args.target, args, arcpy)
    except Exception as exc:
        print("REFUSED: could not read the staged and target data: %s" % exc)
        print("\nNothing was deleted.")
        return 1
    print("staged source: %d row(s)" % staged)
    print("live target:   %d row(s)" % live)
    print("")
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
                field_mapping=args.field_mapping,
                transactional=args.transactional)
    except Exception as exc:
        print("  FAILED: %s" % exc, file=sys.stderr)
        return 2
    print("\nReplaced.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
