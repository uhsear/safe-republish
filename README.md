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

The row count is the check people add, and three quieter failures get past it.

- **The field that nothing feeds.** The staged table has no `NOTES` column. `Append` fills every
  target field that nothing maps with NULL ([Append](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/append.html):
  "Fields in the target dataset that are not mapped to fields from the input datasets will contain
  null values"). The count is perfect. The notes that people typed in over the years are gone.
- **The append that fails after the truncate.** With `schema_type` left at `TEST`, a staged table
  that differs from the target by one field is rejected with `ERROR 000466`. The rejection comes
  after the truncate, so the target is empty when it arrives. Esri says "Truncate commands do not use
  database transactions and are unrecoverable"
  ([Truncate Table](https://pro.arcgis.com/en/pro-app/3.6/tool-reference/data-management/truncatetable.htm)).
- **The row that vanishes.** A staged text field declared 50 characters wide feeds a target field
  declared 5 wide. `Append` skips every row whose text is too long. It raises no error and it
  reports `Row Count = 2` for a source of three rows. The count check after the append finds the
  difference, and by then the target is already truncated.

```
$ python safe_republish.py --self-test
safe_republish self-test: no arcpy, no database, no network
--------------------------------------------------------------------
PASS  an identical count swings 0%
...
PASS  an EMPTY TARGET is REFUSED by default  <-- pinned defect
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  a NULLED field refuses the run  <-- pinned defect
PASS  text feeding an integer field is a TYPE refusal  <-- pinned defect
PASS  text declared longer than the target field is a LENGTH refusal  <-- pinned defect
PASS  --cover-allow cannot waive a NULLED field under TEST, the append fails after the truncate  <-- pinned defect
...
PASS  a failed Append in an edit session is rolled back and the target keeps its 80 rows  <-- pinned defect
PASS  an append that silently skips a row is rolled back, not saved  <-- pinned defect
...
PASS  --null-fields applies to every plan pair, it used to be ignored  <-- pinned defect
...
PASS  a prefix --trans of a new flag is REFUSED by the parser  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
...
--------------------------------------------------------------------
252 assertions, 0 failed
```

The full run prints all 252 assertions. Each `...` line above is where this block is cut.

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
  does not say whether geoprocessing tools such as `Append` take part in the session. The
  experiment below runs that on a file and a mobile geodatabase.
- **Delete Rows.** Pro refuses a truncate while editing (`ERROR 160592`), so a session needs a
  row delete, and Delete Rows is the geoprocessing tool for it. Its page suggests Truncate Table
  when you want every row gone. That is why the default path still truncates and `--transactional`
  does not.

A search of GitHub on the day this was written found no other tool that checks field coverage
before a truncate. `gh search repos` for `safe-republish` returned only this repository. Searches
for `arcpy append`, `arcpy truncate` and `append field map` returned nothing relevant except a
QGIS plugin that builds a field map for an append. That shows only that the search found nothing.

## Requirements

Python 3.9 or newer for everything. `arcpy` is needed for a real run, because reading and
replacing a feature class needs it. `--self-test` and `--scan` need none of that.

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" safe_republish.py --self-test
```

```
git clone https://github.com/uhsear/safe-republish.git
```

The self-test prints the same 252 assertions on every host listed here, and the outputs match line
for line.

| Host | Python | `arcpy` | Assertions |
|---|---|---|---|
| Windows | 3.13.2 | no | 252, 0 failed |
| Windows, the Pro 3.6 clone | 3.13.7 | yes | 252, 0 failed |
| Windows | 3.9.25 | no | 252, 0 failed |
| Ubuntu | 3.12.3 | no | 252, 0 failed |

The counts are equal because the self-test never imports `arcpy`. It drives every function
through a stub, and it swaps the stub in and out of `sys.modules`, so the same assertions run
whether or not the real module is installed. Linux has no `arcpy`, so the real runs below were
made on Windows only.

`coverage run --branch safe_republish.py --self-test` reports 100 percent of statements and
branches, on Python 3.13.2 and on the Pro 3.6 interpreter. One line carries
`# pragma: no cover`: the `if __name__ == "__main__":` line, whose false branch cannot run when
the file is the program.

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
| `--max-swing` | `0.15` | Largest allowed relative change against the live count. Env: `SAFE_REPUBLISH_MAX_SWING` |
| `--null-fields` | none | Comma separated fields whose NULL or blank share is checked in the staged data. A field named here that the staged data lacks refuses. |
| `--max-null-pct` | `0.10` | Largest allowed NULL or blank share for each `--null-fields` field. Env: `SAFE_REPUBLISH_MAX_NULL_PCT` |
| `--field-mapping` | none | An arcpy field map string, passed to `Append`. A mapped call is sent as `NO_TEST`. |
| `--cover` | off | Read both field lists before the truncate and refuse what `Append` would damage. |
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

Exit codes: 0 allowed, 1 refused, 2 the replace failed, 64 usage error. With `--transactional`,
exit 2 means the session was rolled back, and the message says whether the recount confirmed it.

## Configuration

Precedence is flag, then environment, then the `CONFIGURATION` block at the top of the file.
`--apply` is the only thing that authorises a delete, and no environment variable can turn it on.

## Field coverage: `--cover`

`--cover` runs after the count checks and before anything is deleted. It lists the target fields,
lists the staged fields, and says what `Append` will do to each one. With `--field-mapping` it reads
the map instead of matching names. `OBJECTID`, the geometry and the shape length and area are
skipped. Names match without regard to case, as `Append` matches them.

| Status | Meaning | Refuses? |
|---|---|---|
| `NULLED` | Nothing feeds this target field, so every row is NULL afterwards. | Yes |
| `TYPE` | The staged type cannot feed the target type. Text into a long integer writes `0`. | Yes |
| `LENGTH` | Staged text is declared wider than the target field. `Append` skips rows. | Yes |
| `DROPPED` | The staged table has a field the target lacks. `Append` drops it. | Under `TEST` only |
| `REGENERATED` | A global id or editor tracking field. The geodatabase refills it. | Under `TEST` only |

Only lossless widenings pass the type check: `SmallInteger` into `Integer`, `BigInteger`, `Single`
or `Double`, `Integer` into `BigInteger` or `Double`, and `Single` into `Double`. Anything else
must match.

Two rules decide the refusals. When the run uses `TEST` (the default, and always without
`--field-mapping`), `Append` itself rejects a missing field, an extra field, a type mismatch, and
a global id or tracking field the staged data lacks. That rejection comes after the truncate. So
`--cover` refuses these before the truncate, and `--cover-allow` cannot waive them. A `LENGTH`
finding stays waivable, because `TEST` does not reject a short text field. When the run uses
`NO_TEST` (a field map), `--cover-allow NOTES` accepts the NULL fill for `NOTES`, and the output
says so.

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
targets. `--plan` still decides every pair before it performs any.

## The experiments

GAMEPLAN item 8.3 said to ship each half only if its experiment held. Both ran on ArcGIS Pro 3.6
(Python 3.13.7) in scratch geodatabases built by the experiment. Nothing here touched a real
database.

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

Findings, in the words of the backlog row that asked for this experiment:

- "An unmapped target field is null-filled after the truncate": **held**. It matches Esri's
  documentation.
- "Tracking fields are dropped": **did not hold**. Editor tracking fields are refilled. The old
  created and edited values are lost, and the fields are not NULL. The tool reports them as
  `REGENERATED`.
- Three findings the backlog row did not name: `TEST` fails after the truncate, text into a number
  writes `0`, and a too-long text value drops its row with no error. `--cover` checks all three.

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

## Scanning and planning

`--scan` walks a tree of scripts and lists the truncate-then-append pairs. It marks a file BLIND
when it calls no `GetCount`, because nothing in that file could refuse the truncate. This run is on
a synthetic tree of two scripts.

```
$ python safe_republish.py --scan etl
BLIND  etl\blind_job.py
    line 2 TruncateTable -> line 3 Append NO_TEST
counts etl\careful_job.py
    line 3 TruncateTable -> line 4 Append TEST

2 file(s), 2 truncate-then-append site(s)
1 file(s) and 1 site(s) count no rows anywhere, so nothing in them could refuse the truncate
                                   (exit 1)
```

`--plan` reads a file of `source,target` pairs, refuses a plan that names one target twice before
it reads a single count, and refuses the whole plan if any pair is refused. Every gate on the
command line applies to every pair. Before this release `--plan` ignored `--null-fields`, so a
plan could replace a target with a staged table whose owner column was entirely NULL. The
self-test pins the fix, and this run shows it.

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
- `--cover` compares declared field definitions, not values. A `LENGTH` refusal means a long value
  could exist, not that one does. Accept it with `--cover-allow` when you know the data fits.

## Limits

- **Enterprise geodatabases are untested.** `--transactional` was run on a file geodatabase and a
  mobile geodatabase. The tool passes `multiuser_mode` as true for a versioned target and false
  otherwise, which follows the Editor documentation. Delete Rows in a versioned edit session and
  the lock behaviour on a SQL Server geodatabase were not exercised. The self-test covers the
  versioned argument with a stub only. Esri's Truncate Table page says "Versioned data is not supported as
  input", so the non-transactional path sends a versioned target to Delete Rows.
- **A mobile geodatabase that another process holds open for writing refuses the session** with
  `Workspace or data source is read only`. The first end-to-end run hit this because the driver
  script still held the file. The tool reports "could not open an edit session ... nothing was
  emptied" and exits 2.
- **Large tables in a session are untested.** The runs used five to a hundred rows.
- **Merge rules in a field map are not evaluated.** `--cover` checks the first input of each
  output field.
- **Lossless widening is a judgement.** Only `SmallInteger` into `Integer` was run on Pro. The rest
  of the table in `WIDENING` is arithmetic, not an Esri statement.
- **The `if __name__ == "__main__":` line carries `# pragma: no cover`.** Its false branch cannot
  run when the file is the program. Every other statement and branch is covered.
- **No network and no credentials.** The tool reads geodatabase paths only, so no stub server was
  needed.
- **Sources checked.** Esri documentation for [Append](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/append.html),
  [Truncate Table](https://pro.arcgis.com/en/pro-app/3.6/tool-reference/data-management/truncatetable.htm),
  [Delete Rows](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/delete-rows.html)
  and the [Editor class](https://doc.esri.com/en/arcgis-pro/latest/arcpy/data-access/editor.html).
  The Append page does not say what `TEST` does with a field map, whether it ignores or rejects
  it, and it does not mention tracking fields. The experiment above is the evidence for both.

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
