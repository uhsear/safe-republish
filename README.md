# safe-republish

Refuse to truncate a feature class when the staged replacement fails a plausibility check.

The nightly pattern this guards is everywhere in arcpy ETL:

```python
arcpy.TruncateTable_management(target)      # production is now empty
arcpy.Append_management(staged, target, "NO_TEST")
```

Between those two lines the good copy is gone. A partial download, a vendor feed that came back
half empty, a join that silently nulled out, and you have already destroyed production and
appended the damage. You find out the next morning when the map is blank.

```
$ python safe_republish.py --self-test
safe_republish self-test: no arcpy, no database, no network
--------------------------------------------------------------------
PASS  an identical count swings 0%
PASS  a 15 point drop from 100 swings 15%
...
PASS  a 37% loss is REFUSED even though the count is large
PASS  an EMPTY TARGET is REFUSED by default  <-- pinned defect
...
--------------------------------------------------------------------
49 assertions, 0 failed
```

## Requirements

ArcGIS Pro's Python for a real run, because reading and replacing a feature class needs `arcpy`:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" safe_republish.py --self-test
```

`--self-test` needs none of that. The decision logic is pure Python 3.8+ with no `arcpy` import,
so you can check the tool before you have a geodatabase to point it at. Nothing to install either way.

```
git clone https://github.com/uhsear/safe-republish.git
```

## Quick start

```
python safe_republish.py --self-test
```

## Usage

Check first. Nothing is deleted without `--apply`.

```
python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels
python safe_republish.py --source staged.gdb/Parcels --target prod.sde/Parcels --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--source` | none | Staged replacement data. Required. |
| `--target` | none | Feature class that would be emptied and refilled. Required. |
| `--min-rows` | `1` | Absolute floor for the staged count. Env: `SAFE_REPUBLISH_MIN_ROWS` |
| `--max-swing` | `0.15` | Largest allowed relative change against the live count. Env: `SAFE_REPUBLISH_MAX_SWING` |
| `--allow-empty-target` | off | Proceed when the target is already empty. |
| `--apply` | off | Perform the replace. Without it nothing is deleted. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes: 0 allowed, 1 refused, 2 the replace failed part way, 64 usage error.

## Configuration

Precedence is flag, then environment, then the `CONFIGURATION` block at the top of the file.
`--apply` is the only thing that authorises a delete, and no environment variable can turn it on.

## Why the obvious version is wrong

An absolute row floor is the check people write first, and it does not catch the failure that
actually happens. A 288,000 row parcel layer arriving with 180,000 rows clears any floor you
would reasonably set and is still a 37 percent loss. The comparison that catches it is relative,
against what is live right now, which means reading the target before touching it.

`Append`'s `schema_type` does not help here. It checks field shape, not whether the data is
believable, and the common setting is `NO_TEST`, which checks nothing at all.

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

- No backup and no rollback. If you want the replace itself to be atomic, wrap it in
  `arcpy.da.Editor(workspace, multiuser_mode=False)`, which gives you a real database
  transaction. This tool decides whether to start, it does not undo.
- No field or attribute checking. Row counts only. Nulls are `nullscan`'s job and schema drift
  is `FeatureCompare`'s.
- No schema repair, no field mapping. `Append` is called with `TEST`, so a shape mismatch stops
  the run rather than silently dropping a column.
- No opinion about where the staged data came from. Downloading and building it is your pipeline.
- Row count is a proxy for plausibility, not proof of it. A feed with the right number of
  entirely wrong rows passes.
- Branch versioned data is untested. Traditional versioning dispatches to `DeleteRows` because
  `TruncateTable` refuses a versioned table.

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
