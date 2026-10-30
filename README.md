# safe-republish

Refuse to truncate a feature class when the staged replacement fails a plausibility check. Check
the field lists first, so the append cannot fail after the target is empty. Optionally run the
clear and the append in one edit session, so a failure puts the rows back.

The nightly pattern this guards is everywhere in arcpy ETL:

```python
arcpy.TruncateTable_management(target)      # production is now empty
arcpy.Append_management(staged, target, "NO_TEST")
```

Between those two lines the good copy is gone. A partial download, a vendor feed that came back
half empty, a join that silently nulled out, and you have already destroyed production and
appended the damage. You find out the next morning when the map is blank.

The row count is the check people add, and five quieter failures get past it.

- **The field that nothing feeds.** The staged table has no `NOTES` column. `Append` fills every
  target field that nothing maps with NULL ([Append](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/append.html):
  "Fields in the target dataset that are not mapped to fields from the input datasets will contain
  null values"). The count is perfect. The notes that people typed in over the years are gone.
- **The append that fails after the truncate.** With `schema_type` left at `TEST`, a staged table
  that differs from the target by one field is rejected with `ERROR 000466`. The rejection comes
  after the truncate, so the target is empty when it arrives. Esri says "Truncate commands do not use
  database transactions and are unrecoverable"
  ([Truncate Table](https://pro.arcgis.com/en/pro-app/3.6/tool-reference/data-management/truncatetable.htm)).
  A long integer feeding a double field is rejected the same way, although it loses nothing.
- **The row that vanishes.** A staged text field declared 50 characters wide feeds a target field
  declared 5 wide. `Append` skips every row whose text is too long. It raises no error and it
  reports `Row Count = 2` for a source of three rows. The count check after the append finds the
  difference, and by then the target is already truncated.
- **The class that appends nothing.** A polygon class feeds a point target. `Append` raises no
  error and writes no rows, under `TEST` and under `NO_TEST`. The fields all match, the count is
  perfect, and the target is empty.
- **The table that feeds itself.** A typo names one table as both source and target. The truncate
  empties it, and `Append` then fails with `ERROR 000670` because the output cannot be the input.

```
$ python safe_republish.py --self-test
safe_republish self-test: no arcpy, no database, no network
--------------------------------------------------------------------
PASS  an identical count swings 0%
...
PASS  an EMPTY TARGET is REFUSED by default  <-- pinned defect
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  SmallInteger into Double passes NO_TEST and refuses under TEST, which rejects it after the truncate  <-- pinned defect
PASS  a polygon class feeding a point class refuses, Append writes no rows and no error  <-- pinned defect
...
PASS  a failed Append in an edit session is rolled back and the target keeps its 80 rows  <-- pinned defect
...
PASS  a source that is also the target is refused before any delete  <-- pinned defect
PASS  the same table spelled with other slashes and case is refused too  <-- pinned defect
...
PASS  --scan exits 3 and says the scan is incomplete when it could not read a file  <-- pinned defect
...
PASS  an argparse refusal of --ap exits 64, never 2  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
...
--------------------------------------------------------------------
311 assertions, 0 failed
```

The full run prints all 311 assertions. Each `...` line above is where this block is cut.

## What already exists

Esri already ships the parts. This tool adds a check before the delete, not a new way to load.

- **`Append` with `schema_type="TEST"`.** It rejects a field mismatch, and it is the safer default.
  It rejects after the truncate. This tool reads both field lists before the truncate and says
  what `Append` is about to do. `Append` with `NO_TEST` and a field map does what the map says and
  tells you nothing about the fields the map leaves out.
- **`arcpy.da.Editor`.** It is Esri's edit session. Its documentation says "if any exceptions occur,
  changes will not be saved" and "For nonversioned data, edits will be committed when
  stopEditing(True) or rolled back when stopEditing(False)"
  ([Editor](https://doc.esri.com/en/arcgis-pro/latest/arcpy/data-access/editor.html)). The page
  does not say whether geoprocessing tools such as `Append` take part in the session. Experiment 2
  below runs that on a file and a mobile geodatabase.
- **Delete Rows.** Pro refuses a truncate while editing (`ERROR 160592`), so a session needs a
  row delete, and Delete Rows is the geoprocessing tool for it. Its page suggests Truncate Table
  when you want every row gone. That is why the default path still truncates and `--transactional`
  does not.

On 2026-10-30, `gh search repos safe-republish` returned this repository and one unrelated
project, a Python message bus. The searches `arcpy append`, `arcpy truncate` and `arcpy safe
truncate` returned nothing. `append field map` returned one repository,
[geor_append_field_map](https://github.com/Geo-Ridge/geor_append_field_map). Its `metadata.txt`
describes a QGIS processing plugin that appends features with a field mapping. It is a loader, and
it does not check anything before a truncate. A search that finds nothing shows only that the
search found nothing, so this is not a claim that no other tool exists.

## Requirements

Python 3.9 or newer for everything. `arcpy` is needed for a real run, because reading and
replacing a feature class needs it. `--self-test` and `--scan` need none of that.

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" safe_republish.py --self-test
```

```
git clone https://github.com/uhsear/safe-republish.git
```

The self-test prints the same 311 assertions on every host listed here, and the outputs match line
for line.

| Host | Python | `arcpy` | Assertions |
|---|---|---|---|
| Windows | 3.13.2 | no | 311, 0 failed |
| Windows, the Pro 3.6 clone | 3.13.7 | yes | 311, 0 failed |
| Windows | 3.9.25 | no | 311, 0 failed |
| Ubuntu | 3.12.3 | no | 311, 0 failed |

The counts are equal because the self-test never imports `arcpy`. It drives every function
through a stub, and it swaps the stub in and out of `sys.modules`, so the same assertions run
whether or not the real module is installed. Linux has no `arcpy`, so the real runs below were
made on Windows only. The scan and the exit codes that need no `arcpy` were also run on Ubuntu.

`coverage run --branch safe_republish.py --self-test` reports 100 percent of statements and
branches, on Python 3.13.2. One line carries `# pragma: no cover`: the
`if __name__ == "__main__":` line, whose false branch cannot run when the file is the program.

## Quick start

```
python safe_republish.py --self-test
```

## Usage

Check first. Nothing is deleted without `--apply`.

```
python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels
python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels --cover
python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels --cover --apply
python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels --transactional --apply
python safe_republish.py --scan C:/etl/scripts
python safe_republish.py --plan nightly.txt --cover --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--source` | none | Staged replacement data. Required. |
| `--target` | none | Feature class that would be emptied and refilled. Required. |
| `--min-rows` | `1` | Absolute floor for the staged count. Env: `SAFE_REPUBLISH_MIN_ROWS` |
| `--max-swing` | `0.15` | Largest allowed relative change against the live count, as a fraction. Env: `SAFE_REPUBLISH_MAX_SWING` |
| `--null-fields` | none | Comma separated fields whose NULL or blank share is checked in the staged data. A field named here that the staged data lacks refuses. Names match without regard to case. |
| `--max-null-pct` | `0.10` | Largest allowed NULL or blank share for each `--null-fields` field. Env: `SAFE_REPUBLISH_MAX_NULL_PCT` |
| `--field-mapping` | none | An arcpy field map string, passed to `Append`. A mapped call is sent as `NO_TEST`. |
| `--cover` | off | Read both field lists and both geometry types before the truncate and refuse what `Append` would damage. |
| `--cover-allow` | none | Comma separated target fields whose `--cover` finding you accept. Needs `--cover`. |
| `--transactional` | off | Clear and refill the target in one edit session, so a failure rolls it back. |
| `--allow-empty-target` | off | Proceed when the target is already empty. |
| `--scan` | none | Report the truncate-then-append pairs under a directory. Reads only. |
| `--plan` | none | Decide every `source,target` pair in a file before performing any of them. |
| `--apply` | off | Perform the replace. Without it nothing is deleted. |
| `--self-test` | off | Run the offline assertions and exit. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
instead of being read as `--apply`, and `--trans` and `--cov` are refused instead of being read
as `--transactional` and `--cover`. Nothing else may write: no mode other than `--apply` changes a
geodatabase.

The three thresholds must be finite numbers. A value of `nan` or `inf`, on the command line or in
the environment, is a usage error, because a comparison with `nan` is always false and a check
built on it would allow every run.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Allowed, or the scan found nothing blind and read every path. |
| 1 | Refused, or the scan found a file with no row count that reaches a branch. |
| 2 | The replace failed after `--apply` began. The target may be empty. |
| 3 | The check could not run and nothing was touched: no `arcpy`, data that could not be read, or a scan that could not read every path. |
| 64 | Usage error. This includes every refusal by the argument parser. |

With `--transactional`, exit 2 means the session was rolled back, and the message says whether the
recount confirmed it. Python's `argparse` exits 2 for a bad flag. This tool overrides that, because
a scheduler that reads 2 as "the replace failed" would go looking for an empty target when nothing
had run.

## Configuration

Precedence is flag, then environment, then the `CONFIGURATION` block at the top of the file.
`--apply` is the only thing that authorises a delete, and no environment variable can turn it on.
An environment value that is not a plain number exits 64 and names the variable.

## Field coverage: `--cover`

`--cover` runs after the count checks and before anything is deleted. It lists the target fields,
lists the staged fields, and says what `Append` will do to each one. With `--field-mapping` it reads
the map instead of matching names. `OBJECTID`, the geometry and the shape length and area are
skipped as fields. Names match without regard to case, as `Append` matches them. It also compares
the geometry type of the two feature classes.

| Status | Meaning | Refuses? |
|---|---|---|
| `NULLED` | Nothing feeds this target field, so every row is NULL afterwards. | Yes |
| `TYPE` | The staged type cannot feed the target type. Text into a long integer writes `0`. | Yes |
| `LENGTH` | Staged text is declared wider than the target field. `Append` skips rows. | Yes |
| `GEOMETRY` | The staged feature class has a different geometry type. `Append` writes no rows. | Always |
| `DROPPED` | The staged table has a field the target lacks. `Append` drops it. | Under `TEST` only |
| `REGENERATED` | A global id or editor tracking field. The geodatabase refills it. | Under `TEST` only |

The type check depends on the schema type. Under `NO_TEST` (a field map) these widenings pass:
`SmallInteger` into `Integer`, `BigInteger`, `Single` or `Double`, `Integer` into `BigInteger` or
`Double`, and `Single` into `Double`. Under `TEST` only `SmallInteger` into `Integer` and `Single`
into `Double` pass, because `Append` under `TEST` rejects the other five with `ERROR 000466`
after the truncate. Anything else must match. Experiment 3 below is the measurement.

Two rules decide the refusals. When the run uses `TEST` (the default, and always without
`--field-mapping`), `Append` itself rejects a missing field, an extra field, a type mismatch, and
a global id or tracking field the staged data lacks. That rejection comes after the truncate. So
`--cover` refuses these before the truncate, and `--cover-allow` cannot waive them. A `LENGTH`
finding stays waivable, because `TEST` does not reject a short text field. When the run uses
`NO_TEST` (a field map), `--cover-allow NOTES` accepts the NULL fill for `NOTES`, and the output
says so. A `GEOMETRY` finding cannot be waived under either.

The first run below is the unfed `NOTES` field. The second is the same pair without `--cover`.
Both are real output from Pro 3.6 on a file geodatabase. The temporary folder in each path is cut.

```
$ python safe_republish.py --source scratch.gdb\s1 --target scratch.gdb\t1 --cover --apply
staged source: 5 row(s)
live target:   5 row(s)

  REFUSED: staged count 5 is within 0.0% of the live count 5 (limit 15.0%)
  REFUSED: field coverage: 2 target field(s) are filled from the staged data
  REFUSED: field NOTES NULLED: is fed by nothing in the staged data, so every row will be NULL after the truncate
VERDICT: REFUSE

Nothing was deleted.
[t1 now holds 5 row(s)]            (exit 1)

$ python safe_republish.py --source scratch.gdb\s1 --target scratch.gdb\t1 --apply
  FAILED: Append into scratch.gdb\t1 FAILED after the target was cleared via TruncateTable. The target is EMPTY. Restore it before re-running: ERROR 000466: scratch.gdb\s1 does not match the schema of target scratch.gdb\t1
[t1 now holds 0 row(s)]            (exit 2)
```

The tool prints a `REFUSED:` label before every reason of a refused run, including the lines that
passed. That is how `describe()` has always rendered a refusal.

A long integer feeding a double field is the type case that looks harmless. `TEST` rejects it
after the truncate. `--cover` refuses it first. A short integer feeding a long integer is accepted
by `TEST` and by `--cover`.

```
$ python safe_republish.py --source scratch.gdb\sl --target scratch.gdb\td --apply
  FAILED: Append into scratch.gdb\td FAILED after the target was cleared via TruncateTable. The target is EMPTY. ...
[td now holds 0 row(s)]            (exit 2)

$ python safe_republish.py --source scratch.gdb\sl --target scratch.gdb\td2 --cover --apply
  REFUSED: field V TYPE: is Double in the target and Integer in the staged data. Append cannot convert some values and writes 0 or NULL without an error
VERDICT: REFUSE

Nothing was deleted.
[td2 now holds 5 row(s)]           (exit 1)

$ python safe_republish.py --source scratch.gdb\ss --target scratch.gdb\tl --cover --apply
  ok: field coverage: 2 target field(s) are filled from the staged data
VERDICT: ALLOW
...
Replaced.
[tl now holds 5 row(s)]            (exit 0)
```

A polygon class feeding a point class is the geometry case:

```
$ python safe_republish.py --source scratch.gdb\pg --target scratch.gdb\pt --cover --apply
  REFUSED: field Shape GEOMETRY: is Polygon in the staged data and Point in the target. Append writes no rows and raises no error, so the target would be left empty
VERDICT: REFUSE

Nothing was deleted.
[pt now holds 5 row(s)]            (exit 1)
```

A field map is often built by adding the target table to a `FieldMappings` object first and the
source second. The exported string then lists the target table as an input of every field it has.
`Append` reads only the source, so inputs that come from the target table feed nothing. `--cover`
ignores them, and it matches the source by path with the slashes normalised. A source spelled differently
from the map feeds nothing, and the run refuses. That is the safe way to be wrong.

```
$ python safe_republish.py --source scratch.gdb\s1 --target scratch.gdb\t3 --cover --cover-allow NOTES --field-mapping "NAME ..." --apply
  ok: staged count 5 is within 0.0% of the live count 5 (limit 15.0%)
  ok: field coverage: 2 target field(s) are filled from the staged data
  ok: field NOTES NULLED: is fed by nothing in the staged data, so every row will be NULL after the truncate (accepted by --cover-allow)
VERDICT: ALLOW
...
Replaced.                          (exit 0)
rows: [('new0', 0, None), ('new1', 10, None)]
```

Arcpy reads a field map string of `garbage` as one output field named `garbage` with no input.
Every real target field is then unfed, and `--cover` refuses. A blank string raises `ERROR 999999`
in arcpy.

## Source and target

`--source` and `--target` must name different tables. The tool compares the two paths after it
flattens slashes, dot segments and case, and it exits 64 before it reads a count. A `--plan` step
that reads and writes one table is refused the same way. Both runs below are real.

```
$ python safe_republish.py --source scratch.gdb\same --target scratch.gdb\same --apply
error: --source and --target name the same table: scratch.gdb\same. The truncate would empty it before the append reads it.
[same now holds 5 row(s)]          (exit 64)

$ python safe_republish.py --source scratch.gdb\same --target C:/USERS/.../SCRATCH.GDB/SAME --apply
error: --source and --target name the same table: ...
[same now holds 5 row(s)]          (exit 64)
```

Before this check the same run gave `VERDICT: ALLOW` (a 0 percent swing), cleared the table, failed
in `Append` with `ERROR 000670`, and left it empty.

## Transactional replace: `--transactional`

Off by default. With it, `--apply` opens an edit session on the target's workspace, empties the
target with Delete Rows, appends, counts the rows, and saves only when the count equals the staged
count. A failed append, a count mismatch or a failed save aborts the operation and discards the
session. The tool then counts the target again and says whether the original rows came back.

The first run uses a staged table with one extra field, which `TEST` rejects after it clears the
target. The second adds `--transactional`.

```
$ python safe_republish.py --source scratch.gdb\s6 --target scratch.gdb\t6 --apply
  FAILED: Append into scratch.gdb\t6 FAILED after the target was cleared via TruncateTable. The target is EMPTY. ...
[t6 now holds 0 row(s)]            (exit 2)

$ python safe_republish.py --source scratch.gdb\s6b --target scratch.gdb\t6b --apply --transactional
  FAILED: Replacing scratch.gdb\t6b FAILED inside an edit session. The edit session was rolled back and the target holds its original 5 row(s). Cause: ERROR 000466: ...
[t6b now holds 5 row(s)]           (exit 2)
```

The same flag catches the skipped row. Here a staged text field 50 wide feeds a target field 5
wide.

```
$ python safe_republish.py --source scratch.gdb\s5 --target scratch.gdb\t8 --apply --transactional
  FAILED: Replacing scratch.gdb\t8 FAILED inside an edit session. The edit session was rolled back and the target holds its original 5 row(s). Cause: ROW COUNT MISMATCH after append: staged 5, target now 4
[t8 now holds 5 row(s)]            (exit 2)

$ python safe_republish.py --source scratch.gdb\s5 --target scratch.gdb\t8b --apply
  FAILED: ROW COUNT MISMATCH after append: staged 5, target now 4. The target is in an unknown state, inspect it before re-running.
[t8b now holds 4 row(s)]           (exit 2)
```

A plan with `--transactional` gives each target its own session. A plan is not atomic across
targets. `--plan` still decides every pair before it performs any. This run is real:

```
$ python safe_republish.py --plan plan.txt --transactional --apply
...
=== APPLY ===
  emptied target via DeleteRows inside an edit session
  appended 5 row(s) from the staged source
  verified: target now holds 5 row(s)
  emptied target via DeleteRows inside an edit session
  appended 5 row(s) from the staged source
  verified: target now holds 5 row(s)
Replaced 2 target(s).              (exit 0)
[pa now holds 5 row(s)]  [pb now holds 5 row(s)]
pa rows: ['new0', 'new1', 'new2', 'new3', 'new4']
```

## The experiments

Four experiments decided what ships. All ran on ArcGIS Pro 3.6 (Python 3.13.7) in scratch
geodatabases built by the experiment. Nothing here touched a real database. The experiments were
run from throwaway scripts that are not in this repository, so the tables below are the record. The
2026-10-30 rerun of experiments 1 and 2 matched the first run in every row.

### Experiment 1: what a truncate-then-append does to a target field the source lacks

Each case builds a target with `NAME`, `VALUE` and `NOTES` holding two rows, truncates it, and
appends a staged table. The cases and the observed result:

| Case | `schema_type` | Observed |
|---|---|---|
| Staged table lacks `NOTES` | `NO_TEST` | Appended. `NOTES` is `None` in every row. |
| Staged table lacks `NOTES` | `TEST` | `ERROR 000466`. The target holds 0 rows. |
| Staged table has an extra field | `NO_TEST` | Appended. The extra field is dropped. |
| Staged table has an extra field | `TEST` | `ERROR 000466`. The target holds 0 rows. |
| Staged `VALUE` is text, target is long | `NO_TEST` | Appended. `"10"` became `10`, `"zz"` became `0`. |
| Staged `VALUE` is text, target is long | `TEST` | `ERROR 000466`. |
| Field names differ only in case | `TEST` and `NO_TEST` | Matched. |
| Fields in a different order | `TEST` and `NO_TEST` | Matched. |
| Short integer feeds long integer | `TEST` and `NO_TEST` | Matched. |
| Target has editor tracking, staged does not | `NO_TEST` | Appended. Tracking fields hold the append time and user, not the old values. |
| Target has editor tracking, staged does not | `TEST` | `ERROR 000466`. |
| Target has a global id, staged does not | `TEST` | `ERROR 000466`. |
| Target has a global id, staged does not | `NO_TEST` | Appended. New global ids were generated. |
| Staged text 50 wide, target 5 wide, one value 10 long | `TEST` and `NO_TEST` | Three rows in, two rows out. No error. `Row Count = 2`. |
| A field map object from `addTable(target)` and `addTable(source)` | `TEST` | `ERROR 000466`. `TEST` does not use the map. |

Findings:

- An unmapped target field is null-filled after the truncate. This **held**, and it matches Esri's
  documentation.
- A truncate-then-append drops tracking fields. This **did not hold**. Editor tracking fields are
  refilled. The old created and edited values are lost, and the fields are not NULL. The tool
  reports them as `REGENERATED`.
- Three findings that nobody had listed: `TEST` fails after the truncate, text into a number
  writes `0`, and a too-long text value drops its row with no error. `--cover` checks all three.

The Append page says a field map "can only be used when the schema_type parameter is set to
NO_TEST". It does not say whether `TEST` ignores or rejects a map. The last row of the table is
the answer: `TEST` rejected the call.

### Experiment 2: does a failed append after a delete roll back inside an edit session

The case builds a target of five rows. It opens `arcpy.da.Editor`, calls `startEditing(False,
False)` and `startOperation()`, runs the geoprocessing tool Delete Rows, then runs an `Append`
that fails under `TEST`. It calls `abortOperation()` and `stopEditing(False)`, and counts the
target.

| Case | File geodatabase | Mobile geodatabase |
|---|---|---|
| Delete Rows and a failed Append, no editor | 0 rows | 0 rows |
| The same inside the session, then abort | 5 rows | 5 rows |
| Delete Rows and a good Append inside the session, then save | 3 rows (the staged count) | 3 rows |
| Truncate Table inside the session | `ERROR 160592: Truncation not allowed while editing.` | the same |
| A cursor delete inside the session, then abort | 5 rows | 5 rows |

So the geoprocessing tools do take part in the session on both geodatabase types, and the tool
that can be undone is Delete Rows. Truncate Table cannot be used, which is why `--transactional`
uses Delete Rows. The tool's own end-to-end runs above, and the mobile runs below, repeat this
through `safe_republish.py` itself.

```
$ python safe_republish.py --source mob.geodatabase\main.s10b --target mob.geodatabase\main.t10 --apply --transactional
  FAILED: Replacing mob.geodatabase\main.t10 FAILED inside an edit session. The edit session was rolled back and the target holds its original 5 row(s). ...
[t10 now holds 5 row(s)]           (exit 2)
$ python safe_republish.py --source mob.geodatabase\main.s10 --target mob.geodatabase\main.t10 --apply --transactional
Replaced.                          (exit 0)
[t10 now holds 5 row(s)]
```

The experiment held on the two geodatabase types that could be tested. It did **not** run on an
enterprise geodatabase, so the flag ships off by default and the README does not claim it works
there.

### Experiment 3: which type pairs does Append accept

The case builds one source table and one target table for each pair of eleven field types (short,
long, big integer, float, double, text, date, date only, time only, timestamp with offset, guid),
121 pairs. Each pair copies the empty target, appends a one row source, and reads the value back.
It runs once under `TEST` and once under `NO_TEST`.

| Result | Pairs |
|---|---|
| `TEST` accepts | 13 of 121: the 11 identical types, short into long, float into double |
| `TEST` accepts what `NO_TEST` rejects | none |
| `NO_TEST` accepts with the value intact | 32 of 121, including all 7 pairs in the tool's widening table |
| `TEST` rejects, `NO_TEST` accepts, and the pair is lossless | short into big integer, float or double, and long into big integer or double |

The last row is the reason for two widening tables. Those five pairs lose nothing, `NO_TEST`
appends them correctly, and `TEST` rejects them with `ERROR 000466` after the truncate. A tool
that let them through under `TEST` would empty the target. An earlier version did.

### Experiment 4: what Append does with a different geometry type

The case appends two rows from a source into an empty target. Each line is the number of rows the
target holds afterwards.

| Source into target | `TEST` | `NO_TEST` |
|---|---|---|
| Polygon into point | 0, no error | 0, no error |
| Point into polygon | 0, no error | 0, no error |
| Point into polyline | 0, no error | 0, no error |
| Table into point | 2 | 2 |
| Point into table | 2 | 2 |
| Point with Z into point | 2 | 2 |
| Point into point with Z | 2 | 2 |
| Point with M into point, and the reverse | 2 | 2 |

`--cover` therefore refuses two feature classes whose `Describe().shapeType` differs, and ignores a
table on either side and a Z or M difference. The property is documented as "shapeType (Read only)
The geometry shape type"
([Feature class properties](https://pro.arcgis.com/en/pro-app/latest/arcpy/functions/featureclass-properties.htm)).

## Scanning and planning

`--scan` walks a tree of scripts and lists the truncate-then-append pairs. It marks a file BLIND
when no `GetCount` result in it reaches an `if`, `elif`, `while` or `assert` line, because then
nothing in that file could refuse the truncate. A script that calls `GetCount` only to print it is
BLIND. A file or directory the scan cannot read is listed as UNREADABLE, and the scan then exits 3
instead of reporting a clean tree. This run is on a synthetic tree of three scripts. One of them
counts only for a log line.

```
$ python safe_republish.py --scan etl
gated  etl\gated_job.py
    line 4 TruncateTable -> line 5 Append TEST
BLIND  etl\logged_job.py
    GetCount appears, but no if, elif, while or assert reads it
    line 3 TruncateTable -> line 4 Append TEST
BLIND  etl\deep\locked_job.py
    line 2 TruncateTable -> line 3 Append NO_TEST

3 file(s), 3 truncate-then-append site(s)
2 file(s) and 2 site(s) have no row count that reaches a branch, so nothing in them could refuse the truncate
                                   (exit 1)
```

The same scan after the logged script is removed and the read permission on `locked_job.py` is
denied with `icacls`. Ubuntu gave the same result for a file and a directory set to mode 000.

```
$ python safe_republish.py --scan etl
gated  etl\gated_job.py
    line 4 TruncateTable -> line 5 Append TEST

1 file(s), 1 truncate-then-append site(s)
0 file(s) and 0 site(s) have no row count that reaches a branch, so nothing in them could refuse the truncate
UNREADABLE etl\deep\locked_job.py
1 path(s) could not be read, so this scan is NOT complete.
                                   (exit 3)
```

`--plan` reads a file of `source,target` pairs, refuses a plan that names one target twice or reads
and writes one table before it reads a single count, and refuses the whole plan if any pair is
refused. Slashes, dot segments and case do not hide a repeated target. A comment starts with `#`
at the start of a line or after a space. Every gate on the command line applies to every pair,
and the usage checks of a single run apply too. Before this release `--plan` ignored
`--null-fields`, so a plan could replace a target with a staged table whose owner column was
entirely NULL. The self-test pins the fix, and this run shows it.

```
$ python safe_republish.py --plan plan.txt --null-fields OWNER --apply
scratch.gdb\nulled -> scratch.gdb\live
  REFUSED: staged count 100 is within 0.0% of the live count 100 (limit 15.0%)
  REFUSED: field OWNER is 100.0% NULL or blank in the staged data (100 of 100), over the --max-null-pct limit of 10.0%
VERDICT: REFUSE

scratch.gdb\good -> scratch.gdb\t2
  ok: staged count 100 is within 0.0% of the live count 100 (limit 15.0%)
  ok: field OWNER is 0.0% NULL or blank (limit 10.0%)
VERDICT: ALLOW

At least one step was refused, so no step ran. Nothing was deleted.
[live now holds 100 row(s)]        (exit 1)
```

A plan that names one target in two spellings:

```
$ python safe_republish.py --plan plan.txt --apply
  REFUSED: target C:/USERS/.../SCRATCH.GDB/T2 is named twice, by step 1 and step 2. The second truncate would destroy what the first append wrote.
VERDICT: REFUSE

Nothing was read and nothing was deleted.
[t2 now holds 5 row(s)]            (exit 1)
```

## Why the obvious version is wrong

An absolute row floor is the check people write first, and it does not catch the failure that
actually happens. A 288,000 row parcel layer arriving with 180,000 rows clears any floor you
would reasonably set and is still a 37 percent loss. The comparison that catches it is relative,
against what is live right now, which means reading the target before touching it.

`Append`'s `schema_type` does not help here. `NO_TEST` checks nothing. `TEST` checks the field
shape, and it does so after the truncate has emptied the target.

The subtle one is the empty target. A relative check has nothing to divide by when the target
holds zero rows, so the tempting move is to skip the check and carry on:

```python
if not live_count:
    log.warning("target is empty, relative guard skipped")   # wrong
elif abs(staged - live) / live > 0.15:
    raise RuntimeError(...)
```

That fails open at the worst possible moment. A target is usually empty because a previous run
died part way through, which is exactly when somebody should look before another truncate lands.
This tool refuses instead, and makes you pass `--allow-empty-target` on purpose. The `--self-test`
asserts that behaviour, so the tempting version above fails three assertions.

## What it will not do

- No backup. Without `--transactional` there is no rollback either. With it, the rollback exists
  only where the experiment above ran.
- No schema repair and no field mapping of its own. It reads your map. It does not write one.
- No opinion about where the staged data came from. Downloading and building it is your pipeline.
- Row count is a proxy for plausibility, not proof of it. A feed with the right number of
  entirely wrong rows passes.
- `--cover` compares declared field definitions and the geometry type, not values. A `LENGTH`
  refusal means a long value could exist, not that one does. Accept it with `--cover-allow` when
  you know the data fits.

## Limits

- **Enterprise geodatabases are untested.** `--transactional` was run on a file geodatabase and a
  mobile geodatabase. The tool passes `multiuser_mode` as true for a versioned target and false
  otherwise, which follows the Editor documentation. Delete Rows in a versioned edit session and
  the lock behaviour on a SQL Server geodatabase were not exercised. The self-test covers the
  versioned argument with a stub only. Esri's Truncate Table page says "Versioned data is not supported as
  input", so the non-transactional path sends a versioned target to Delete Rows.
- **A mobile geodatabase that another process holds open for writing refuses the session** with
  `Workspace or data source is read only`. A run that shared its process with the script that built
  the file hit this. The tool reports "could not open an edit session ... nothing was emptied" and
  exits 2. The mobile runs above come from a driver that never imports `arcpy`, so it holds no lock.
- **Large tables in a session are untested.** The runs used five to a hundred rows.
- **Merge rules in a field map are not evaluated.** `--cover` checks the first input of each
  output field. It does not compare domains, subtypes, spatial references or datum
  transformations. It compares declared field definitions and the geometry type only.
- **Widening beyond experiment 3 is not covered.** The two widening tables come from the 121 pairs
  of eleven field types in experiment 3. A type Pro adds later is treated as needing an exact match.
- **Z and M are not compared.** Experiment 4 appended a Z or M difference without loss, so
  `--cover` allows it. The check compares geometry type only.
- **The thresholds are fractions and are not range checked above 1.** `--max-swing 15` allows a
  swing of 1500 percent. `nan` and `inf` are refused, as above.
- **Source and target are compared by path.** Two paths that reach one table through different
  connection files, or through a layer, are not seen as the same table.
- **Row counts come from `GetCount`.** A source or target passed as a layer with a selection or a
  definition query is counted on the selected rows, and Delete Rows on such a layer removes only
  those rows. Pass table or feature class paths, not layers.
- **`--apply` does not decide again.** It re-reads the staged count for its own check. It does not
  re-run the swing decision, so a source that changes between the check and the replace is not
  gated a second time.
- **A chained plan is decided on stale counts.** `--plan` does not check whether one step's source
  is another step's target. In `a,b` then `b,c`, step 2 was decided on a count read before step 1
  replaced `b`.
- **A plan applies one `--field-mapping` string to every pair.** Under `--cover` only the pair
  whose source matches the map can pass. Use a map with `--plan` only when every pair shares that
  source.
- **A plan path cannot contain a comma, or a space followed by `#`.** A comma is a usage error and
  ` #` starts a comment. Copy or rename the data first.
- **The recovery message compares counts only.** If a failure comes after the verified append and
  the replaced rows have the same count as the originals, "rolled back, original rows" cannot tell
  the old rows from the new ones.
- **A failed recount hides the rollback status.** The `--transactional` failure message needs
  `GetCount` on the target to work inside the error handler. If that call raises, the caller prints
  only the recount error.
- **An interrupt is not caught.** A `KeyboardInterrupt` during `--transactional` skips the abort and
  the recount. The open edit session is discarded when the process exits.
- **The scan is a lexical heuristic and it both over- and under-reports.** It reports any `Delete`
  or `DeleteRows` style call within 12 lines before an `Append`, including `Delete_management` on
  an `in_memory` scratch table and `cur.Delete()`. A pair further apart than 12 lines is not
  reported. It does not see an `InsertCursor` refill or `FeatureClassToFeatureClass`, and it does
  not read `.pyt` toolboxes.
- **"Gated" means a count reaches a branch in the same file.** It does not prove the branch stops
  the truncate. A script that counts with a cursor or `len(list(...))` is reported BLIND, and a
  `GetCount` inside a string literal can mark a file gated.
- **A file that `tokenize` rejects is scanned without masking its comments.** Mixed tabs and
  spaces fail on Python 3.12 and later and not on 3.9, so a commented-out `TruncateTable` can count
  as live on one interpreter only. Odd whitespace can add or misplace a finding.
- **`--scan` reads `*.py` as UTF-8 with replacement.** A UTF-16 script matches nothing and is
  reported as having no pairs. It does not follow symlinked directories.
- **A path with a character outside the console code page prints as a backslash escape.** For
  example `\u4e2d` stands for one Chinese character in `--scan` output on a cp1252 Windows console.
  Before this change the same path ended the run with `UnicodeEncodeError` and exit 1.
- **The refusal text rounds to one decimal place.** It can read "staged count 8499 swings 15.0%
  ... over the --max-swing limit of 15.0%" when the real swing is a little over the limit.
- **Not run:** `--transactional` on an enterprise or versioned geodatabase, large tables,
  widening beyond experiment 3, and merge rules in a field map.
- **The `if __name__ == "__main__":` line carries `# pragma: no cover`.** Its false branch cannot
  run when the file is the program. Every other statement and branch is covered.
- **No network and no credentials.** The tool reads geodatabase paths only, so no stub server was
  needed. A grep of every output file kept from the real runs found no credential.
- **Sources checked.** Esri documentation for [Append](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/append.html),
  [Truncate Table](https://pro.arcgis.com/en/pro-app/3.6/tool-reference/data-management/truncatetable.htm),
  [Delete Rows](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/delete-rows.html),
  the [Editor class](https://doc.esri.com/en/arcgis-pro/latest/arcpy/data-access/editor.html) and
  [feature class properties](https://pro.arcgis.com/en/pro-app/latest/arcpy/functions/featureclass-properties.htm).
  The Append page says `TEST` returns an error when the fields do not match. It does not mention
  tracking fields, geometry types, or which widenings `TEST` accepts. Experiments 1, 3 and 4 are
  the evidence for those.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [fcload](https://github.com/uhsear/fcload) - the load itself, refusing the imports that corrupt silently
- [hostedreap](https://github.com/uhsear/hostedreap) - the hosted feature layer equivalent
- [fcpatch](https://github.com/uhsear/fcpatch) - the same refusal applied to a single row rather than a whole layer
