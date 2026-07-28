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

    python safe_republish.py --self-test
    python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels
    python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels --apply

Exit codes: 0 allowed, 1 refused, 2 the replace failed part way, 64 usage error.
"""

from __future__ import print_function

import argparse
import os
import sys

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
           allow_empty_target=False):
    """Decide whether the staged data may replace the live data.

    An empty target REFUSES unless allow_empty_target is passed. This is the
    behaviour worth being deliberate about. An empty target usually means a
    previous run died part way through, which is precisely when the operator
    most needs to look before another truncate lands. Skipping the check there,
    because the relative comparison happens to be undefined, fails open at the
    one moment failing open is worst.
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

    return Decision(verdict, reasons, staged, live, swing)


def describe(decision):
    """Render a decision as the lines the CLI prints."""
    out = ["  %s: %s" % ("ok" if decision.verdict == ALLOW else "REFUSED", r)
           for r in decision.reasons]
    out.append("VERDICT: %s" % decision.verdict)
    return out


# ------------------------------------------------------------------ geodatabase

def count_rows(path, arcpy):
    return int(arcpy.management.GetCount(path)[0])


def is_versioned(path, arcpy):
    """True when the target is a traditionally versioned feature class.

    TruncateTable refuses a versioned table, so the replace path has to use
    DeleteRows instead. Getting this wrong is a run that dies after the check
    passed, which is the worst time for it.
    """
    try:
        return bool(getattr(arcpy.Describe(path), "isVersioned", False))
    except Exception:
        return False


def clear_target(path, arcpy):
    """Empty the target, choosing the operation its versioning allows."""
    if is_versioned(path, arcpy):
        arcpy.management.DeleteRows(path)
        return "DeleteRows (target is versioned)"
    arcpy.management.TruncateTable(path)
    return "TruncateTable"


def replace(source, target, arcpy, schema_type=APPEND_SCHEMA_TYPE):
    """Clear the target and append the source. Returns the final row count."""
    staged = count_rows(source, arcpy)
    how = clear_target(target, arcpy)
    print("  cleared target via %s" % how)
    arcpy.management.Append(source, target, schema_type)
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


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.source or not args.target:
        print("error: --source and --target are both required. Use --self-test "
              "to verify the tool without a geodatabase.", file=sys.stderr)
        return 64
    if args.min_rows < 0 or args.max_swing < 0:
        print("error: --min-rows and --max-swing cannot be negative.",
              file=sys.stderr)
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

    decision = decide(staged, live, args.min_rows, args.max_swing,
                      args.allow_empty_target)
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
        replace(args.source, args.target, arcpy)
    except Exception as exc:
        print("  FAILED: %s" % exc, file=sys.stderr)
        return 2
    print("\nReplaced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
