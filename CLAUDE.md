# CLAUDE.md

Guidance for Claude Code (or any future Claude session) working in this repo.

## What this project is

A minimal, Python re-implementation of DAX Studio's "View Metrics" /
VertiPaq Analyzer feature. It connects to the local Analysis Services
("VertiPaq") engine that Power BI Desktop launches in the background
for an open `.pbix` file, and extracts per-column storage metrics:
Cardinality, Rows, Data Size, Dictionary Size, Hierarchy Size, Total Size.

**This is a Windows-only tool.** The local engine (`msmdsrv.exe`) that
Power BI Desktop starts only exists on Windows, and there is no
supported pure-Python / cross-platform path to it — only ADOMD.NET
(the same client library DAX Studio itself uses), reached here via
`pythonnet`. Do not try to make discovery or the connection layer
cross-platform; that's not solvable without a Windows host and a
running Power BI Desktop process.

## Environment / dependency management

This project uses **uv**, not raw `pip`. Source of truth is
`pyproject.toml` + `uv.lock`; there is no `requirements.txt`.

- Install/sync deps: `uv sync` (creates/updates `.venv` from `uv.lock`).
- Run anything in the project's env: `uv run python analyze_pbix.py ...`
  — don't assume a bare `python`/`pip` on PATH is the right interpreter.
- Adding/bumping a dependency: edit `dependencies` in `pyproject.toml`,
  then `uv lock` to regenerate `uv.lock`, then `uv sync`. Don't hand-edit
  `uv.lock` or reach for `pip install X` — that installs outside the
  locked environment and will drift from what `uv.lock` says is pinned.
- `requires-python = ">=3.13"` (see also `.python-version` = `3.13`).
- Sandboxes without network access to `pypi.org`/`files.pythonhosted.org`
  (like this one) can't run `uv sync`/`uv lock` for real — when testing
  changes here, install ad hoc with `pip install <pkg> --break-system-packages`
  instead, or stub out modules (e.g. a fake `pbi_connection.PbiConnection`)
  the way the hierarchy-size fix in this session was verified, since
  there's no live Windows/Power BI Desktop/ADOMD.NET available either.

## File map

| File | Responsibility |
|---|---|
| `pbi_discover.py` | Finds running `msmdsrv.exe` processes launched by Power BI Desktop, reads `msmdsrv.port.txt` from the per-file workspace folder under `%LOCALAPPDATA%\Microsoft\Power BI Desktop\AnalysisServicesWorkspaces\` to get the TCP port. Falls back to scanning that folder directly if process introspection fails (e.g. permissions). Also resolves the actual .pbix file's name/path/size (`_pbix_file_info_for_parent()`) via `psutil.Process.open_files()` on the parent PBIDesktop.exe process - the only place that knows the real file, since the msmdsrv workspace folder is an anonymized internal copy. |
| `pbi_connection.py` | Thin ADOMD.NET wrapper via `pythonnet` (`clr.AddReference`). Locates `Microsoft.AnalysisServices.AdomdClient.dll` (bundled with Power BI Desktop, or the standalone AMO/ADOMD.NET redistributable), opens a connection, auto-detects the catalog/database name via `$SYSTEM.DBSCHEMA_CATALOGS` if not given. Exposes `query_dmv()` and `query_dax()`, both returning `pandas.DataFrame`. `query_dmv()` runs every raw `AdomdDataReader.GetValue()` result through `_convert_dmv_value()`, which explicitly converts `.NET DateTime`/`DBNull` CLR objects to Python `datetime`/`None` - see the "Key implementation facts" note below on why this isn't optional. |
| `vertipaq_metrics.py` | The actual analyzer logic. Pulls `DISCOVER_STORAGE_TABLES`, `DISCOVER_STORAGE_TABLE_COLUMNS`, `DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS` DMVs and joins them in pandas (DMV SQL doesn't support real joins). Runs one `EVALUATE ROW(..., DISTINCTCOUNT(...))` DAX query per table for exact cardinality. Returns tidy per-column and per-table DataFrames, plus `order_by_table_size_then_field_size()` for the table-grouped ordering used in console section 4 / export sheet `ByTableThenField`, and `get_model_summary()` for the whole-model stats (total size, last data refresh via a 3-source DMV fallback chain, table/column counts) used in console section 0. |
| `pbi_report.py` | Colorized console rendering via `rich` (`print_table_summary`, `print_columns_table`, `print_grouped_by_table`). Every function degrades to plain `to_string()` output if `rich` isn't installed (checked via `pbi_report.RICH_AVAILABLE`) - keep that fallback working when touching this file, since `rich` is a real but non-critical dependency. |
| `analyze_pbix.py` | CLI entry point. Auto-discovers/prompts for a running instance, prints the four console sections via `pbi_report`, optional `--export metrics.xlsx`/`.csv`, `--no-color` to force plain text. |
| `README.md` | End-user setup + usage instructions. |

## Key implementation facts (don't re-derive these from scratch)

- **Data Size**: `SUM(USED_SIZE)` from `DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS`
  grouped by `(TABLE_ID, COLUMN_ID)`, restricted to segments whose
  `TABLE_ID` belongs to a real data table (i.e. *not* one of the
  engine's internal pseudo-tables, which have `TABLE_ID` prefixed
  `H$` = hierarchy, `U$` = user hierarchy, `R$` = relationship).
- **Dictionary Size**: `DICTIONARY_SIZE` column directly from
  `DISCOVER_STORAGE_TABLE_COLUMNS` — this rowset conveniently already
  has human-readable `DIMENSION_NAME`/`ATTRIBUTE_NAME`, so it's also
  used as the master table→column name/datatype/encoding lookup.
- **`base` (the per-column list) is restricted to real tables only**
  (`TABLE_ID` filtered to `real_table_ids`, same test as `real_tables`).
  This was a real, previously-shipped bug: `DISCOVER_STORAGE_TABLE_COLUMNS`
  also lists the "H$" pseudo-table's *own* columns as their own rows
  (same `DIMENSION_NAME`/`ATTRIBUTE_NAME` as the real column, but blank
  `DATATYPE`/`COLUMN_ENCODING` and zero `DICTIONARY_SIZE`). Without this
  filter, every real column is silently duplicated by a "ghost" row that
  shows up as `DataType = N/A`, `Encoding = UNKNOWN` — don't remove this
  filter.
- **Hierarchy Size**: `SUM(USED_SIZE)` from segments whose `TABLE_ID`
  starts with `H$`, resolved back to the real column by **name**, not by
  `COLUMN_ID` — see `_get_hierarchy_sizes_by_name()`. This is the fix for
  a second, related bug that shipped alongside the one above:
  `COLUMN_ID` is only unique *within* a `TABLE_ID`; the `H$` pseudo-table
  numbers its own columns 0, 1, 2, ... completely independently of the
  real table's own `COLUMN_ID` sequence, so a real column's `COLUMN_ID`
  and its own hierarchy segment's `COLUMN_ID` are generally *different
  numbers*, even though `DIMENSION_NAME` (the real table's name) matches
  on both sides. Joining on `(DIMENSION_NAME, COLUMN_ID)` as if the two
  sequences lined up silently attached the wrong (or the same blended)
  size to every column in a table — this was the previously-observed bug
  where every column in a table showed nearly-identical `HierarchySize`
  and real columns showed `0`. The fix: join the `H$` pseudo-table's own
  segment sizes to the `H$` pseudo-table's own metadata row first (both
  scoped to that same `H$...` `TABLE_ID`, so unambiguous), which
  re-keys the result by `(DIMENSION_NAME, ATTRIBUTE_NAME)` — i.e. the
  real `(Table, Column)` name — and match real columns against *that*.
  Still flagged as an estimate in the README since `COLUMN_ID` itself is
  undocumented for this purpose.
- **Cardinality**: deliberately *not* derived from DMVs (the `H$`
  pseudo-table row counts are an unreliable proxy that requires
  fragile `TABLE_ID` string parsing and are off by a version-dependent
  constant). Instead we run `DISTINCTCOUNT()` via DAX, batched as one
  query per table (all columns as measures in a single `ROW()`) to
  minimize round trips. Crucially, the `(Table, Column)` names used to
  build those queries come straight from the same
  `DISCOVER_STORAGE_TABLE_COLUMNS` result already used for the rest of
  the report — never re-derived from a second metadata DMV like
  `TMSCHEMA_COLUMNS` — otherwise a display-name mismatch between two
  DMVs (or a type mismatch on a filter like `Type.isin([...])`) can
  silently zero out cardinality for the whole model with no error.
  **Two failure modes were previously indistinguishable from "it just
  worked" and silently produced an all-blank `Cardinality` column:**
  (1) some ADOMD.NET/engine combinations return `EVALUATE ROW("C0",
  ...)` result columns bracket-wrapped (`"[C0]"`) rather than plain
  (`"C0"`), so a plain `alias in row0.index` check matches nothing,
  with no exception raised; (2) the per-column fallback's bare
  `except Exception: ... = None` swallowed the real error completely.
  Both are fixed: alias matching is now tolerant (`_normalize_alias`
  strips brackets/whitespace/case before comparing), a *mismatch* (not
  just a raised exception) now also triggers the per-column fallback,
  and every failure - batch or per-column - is appended as a
  human-readable string to the optional `cardinality_warnings` list
  passed into `get_column_metrics()`. `analyze_pbix.py` prints a
  summary of these to stderr. If you ever see an all-blank
  `Cardinality` column again, that warning list is where to look first
  - don't just assume DISTINCTCOUNT failed for everything without
  reading it.
- DMV query syntax is DMX-based SQL and does **not** support `JOIN`,
  `GROUP BY`, `LIKE`, `CAST`/`CONVERT` — all correlation across DMVs
  happens in pandas, not in the DMV query text.
- **CLI console output** (`analyze_pbix.py` + `pbi_report.py`) has five
  sections, in order: section 0, `MODEL SUMMARY` (pbix file name/size,
  total in-memory size, last data refresh, table/column counts)
  followed immediately by
  `SIZE DISTRIBUTION BY TABLE` (a horizontal bar chart, biggest table
  first, row/column counts as data labels) - then `TABLE SUMMARY`
  (per-table rollup), `TOP {--top} COLUMNS BY TOTAL SIZE` (capped, for
  a quick glance), `ALL TABLES & COLUMNS (sorted by Total Size, then
  Cardinality)` (the *full*, uncapped column list, sorted flat across
  the whole model rather than grouped per table - mirrors DAX Studio's
  own VertiPaq Analyzer "Columns" tab), and `ALL TABLES & COLUMNS
  (grouped by Table, biggest table first)` (tables grouped and ordered
  by each table's own Total Size descending, fields within a table
  ordered by their own Total Size descending - built by
  `vertipaq_metrics.order_by_table_size_then_field_size()`).
  `--export .xlsx` mirrors this with five sheets: `Overview` (the
  model-summary numbers), `Tables`, `Columns` (natural per-table
  order), `AllColumnsBySize`, `ByTableThenField`. If you change the
  sort/columns of one, keep the export sheet and the console section
  consistent with each other.
- **"Total model size"** in the section-0 summary is `column_metrics
  ["TotalSize"].sum()` - i.e. the same number DAX Studio's VertiPaq
  Analyzer would show as the grand total across every table's "Total
  Size" column. It does *not* include relationship-segment (`R$`)
  overhead or other non-column engine memory, so treat it as a close
  lower bound, not an exact process memory figure - see
  `get_model_summary()`'s docstring.
- **"Last data refresh"** is looked up via `_get_last_data_refresh()` /
  the shared `_timestamps_from_dmv()` helper, checking three documented
  rowsets - **all three, not stopping at the first success**:
  1. `$SYSTEM.TMSCHEMA_PARTITIONS`'s `RefreshedTime` (max across all
     partitions) - the literal, documented meaning of "refresh" in the
     Tabular Object Model (`Partition.RefreshedTime`).
  2. `$SYSTEM.MDSCHEMA_CUBES`'s `LAST_DATA_UPDATE` - traditional
     multidimensional-cube metadata; on Tabular models (every Power BI
     Desktop model) this is frequently present-but-null for every row,
     so falling through past it is expected and normal.
  3. `$SYSTEM.TMSCHEMA_TABLES`'s `ModifiedTime` (max across all tables)
     - deliberately not its sibling `StructureModifiedTime`, which
     tracks schema/structure edits rather than data refreshes.

  Whichever of the three return a value, **the maximum across all of
  them wins** - this function does not stop at the first non-null
  source.

  A fourth source, `$SYSTEM.DBSCHEMA_CATALOGS`'s `DATE_MODIFIED` (the
  whole database's own last-modified timestamp), was added and then
  **removed** - don't re-add it without new, verified evidence. It was
  added to fix failure #3 below, but the user later reported it was
  producing an irrelevant/misleading value on a real report: it tracks
  the database being touched *at all* (which can include things
  unrelated to a real data refresh - e.g. simply having the file open
  or another tool querying its metadata), not specifically "the data
  was reprocessed." Ironic given it was added specifically to fix a
  staleness problem, but a source that's *too fresh* (bumped by
  unrelated activity) is just as wrong as one that's stale - it broke
  the whole point of the feature.

  **This went through three real, user-reported failures already:**
  1. The first version only tried #2 then #3, both untested against a
     real engine, and came back "unknown" against a real report even
     though DAX Studio showed a value - fixed by adding #1
     (`TMSCHEMA_PARTITIONS`) as a source and adding the diagnostics
     described below.
  2. With diagnostics in place, a marshalling bug surfaced: all sources
     reported "present but every value was null/unparseable"
     **simultaneously**, across multiple unrelated DMVs. That pattern -
     every source that could possibly contain a date failing at once -
     was the tell that it wasn't the data, it was `.NET DateTime`
     values from `AdomdDataReader.GetValue()` not always being
     marshalled into Python `datetime` by pythonnet, so
     `pd.to_datetime(..., errors="coerce")` silently produced `NaT` for
     every row. Fixed in `pbi_connection.py`: `query_dmv()` now runs
     every cell through `_convert_dmv_value()`, which explicitly
     converts `System.DateTime` (via its own `.Year`/`.Month`/etc.
     properties, not `str()`, since CLR `DateTime.ToString()` is
     culture-dependent) and `System.DBNull` (→ `None`) before pandas
     ever sees them - this fixes date handling for *any* DMV column
     project-wide, not just refresh detection.
  3. Once dates parsed correctly, a *third* failure mode appeared: the
     function originally stopped at the first source that returned a
     non-null value (#1, most "authoritative"-sounding), but for one
     real report `TMSCHEMA_PARTITIONS.RefreshedTime` returned a stale,
     years-old date (some partition - e.g. a dimension table - hadn't
     been reprocessed in a long time). Fixed (independently of the
     since-removed 4th source) by collecting every source that returns
     a value into a `candidates` list and taking `max()` across all of
     them, rather than returning on the first hit - this part of the
     fix stands regardless of which sources are in the list.

  Don't repeat any of these mistakes: `_timestamps_from_dmv()` takes a
  `diagnostics: list[str]` and appends a *specific* reason every time a
  source doesn't pan out (query raised, empty result, column missing,
  column all-null with a sample raw value and its Python type).
  `_get_last_data_refresh()` additionally appends a `"{label}: found
  {ts}"` line for every candidate whenever more than one source
  succeeds, plus a final `"Used {label} ({ts}) - the most recent of N
  source(s)"` line - that `"Used "` prefix is what `analyze_pbix.py`
  checks to decide whether to print the `[i] sources disagreed` info
  block (it deliberately does NOT print on a routine single-source
  success with the other two failing for the expected "column is
  null" reason - only when there was an actual multi-candidate
  disagreement worth seeing). `get_model_summary()`'s
  `refresh_diagnostics` parameter surfaces all of this up to
  `analyze_pbix.py`. If this fails again on some other real report,
  read the diagnostics - candidate values, sample raw types, which
  source won - before guessing at a fourth DMV or another fix.
  **Timezone note:** the value returned is naive (no UTC offset) -
  ADOMD.NET/pythonnet hands back the underlying .NET `DateTime` as-is,
  which doesn't carry timezone info the way DAX Studio's UI-level
  `+02:00`/`+03:00` display does. Don't assume it's UTC or local
  without checking against the specific engine/session - if exact
  offset handling matters for a future change, that needs its own
  investigation rather than a guessed `tz_localize()`.
- **The `base` column list excludes the engine's auto-generated
  `RowNumber` column** (via `~storage_columns["ISROWNUMBER"]
  .fillna(False)`). **This went back and forth once - here's the
  actual resolution, don't re-litigate it without new evidence:**
  A column-count mismatch against DAX Studio (a table DAX Studio
  reported as 17 columns showed as 16 here) was initially attributed to
  this exclusion, and a version briefly included `RowNumber` to match.
  That theory was then disproven by a precise, byte-level side-by-side
  check against DAX Studio's raw Columns-tab numbers (converting DAX
  Studio's raw byte integers to the same units as this tool's output,
  not comparing a raw integer against an already-rounded MB string):
  Data Size and Dictionary Size matched byte-for-byte on every column
  checked, with `RowNumber` excluded on this side the whole time - so
  whatever caused that particular 17-vs-16 count mismatch on that one
  model, it was not `RowNumber`. Given that, and that the user
  explicitly doesn't want `RowNumber`'s (small) size contribution in
  the report, the exclusion is back and should stay. It's still a
  real, physical column of the real table (shares the real table's own
  `TABLE_ID`, unlike the `H$` pseudo-table ghost rows - a genuinely
  different problem, see the `real_table_ids` filter note above; don't
  conflate the two), it's just deliberately not shown here.
- **One small, real, unresolved discrepancy vs. DAX Studio**: Hierarchy
  Size for a handful of very-low-cardinality columns differs by tens of
  bytes (e.g. 32 vs 64 bytes, or +32 bytes flat) - found via the same
  byte-level check above. Data Size and Dictionary Size were exact
  matches on every column; only Hierarchy Size showed this, and only
  for columns with a handful of distinct values. Not chased further:
  the absolute magnitude is immaterial (tens of bytes in a
  megabyte-scale table) and guessing at a root cause without live
  engine access risks repeating the same mistake as the DateTime
  marshalling and last-refresh issues above (shipping an unverified
  "fix" for something not actually confirmed). If you get access to a
  real engine and want to chase this: check whether the table has
  multiple partitions, and whether `_get_hierarchy_sizes_by_name()`'s
  groupby is summing one `H$` segment per partition for what should be
  a single logical hierarchy value - that's the leading hypothesis, not
  a confirmed cause.
- **PBIX name/size** in the section-0 summary come from
  `pbi_discover._pbix_file_info_for_parent()`, via
  `psutil.Process.open_files()` on the parent PBIDesktop.exe process -
  not from any DMV. `analyze_pbix.py` re-runs discovery even when
  `--port` was passed explicitly (matching the given port against
  `find_all()`'s results) purely to populate these fields; if no match
  is found (or the fields come back `None` - unsaved report, or
  Windows denies the handle-enumeration call), `pbi_report` shows
  "unknown" rather than failing.
- **`human_bytes()` in `pbi_report.py` uses 3 decimal places for
  MB/GB/TB, 1 decimal for B/KB** - not uniformly 1 decimal everywhere.
  This is deliberate: 1 decimal at MB scale hides a ±50 KB range (e.g.
  "4.2 MB" could be 4.15-4.25 MB), which is exactly what made an
  earlier side-by-side comparison against DAX Studio look like a real
  discrepancy when the underlying byte values actually matched. Don't
  revert this to save horizontal space - the extra precision is what
  makes this tool's numbers independently verifiable against DAX
  Studio (or anything else) instead of requiring the reader to trust
  a rounded figure.
- **Colors** are handled entirely in `pbi_report.py` via `rich`; there's
  a plain-text fallback path (`RICH_AVAILABLE = False`) exercised both
  when `rich` isn't installed and when the user passes `--no-color`
  (`analyze_pbix.py` flips `pbi_report.RICH_AVAILABLE` directly rather
  than threading a flag through every call). Color thresholds (byte
  size, `% of DB`, cardinality ratio) are small pure functions
  (`_size_style`, `_pct_style`, `_cardinality_style`, `_encoding_style`)
  - if you add a new colored metric, follow that pattern rather than
  inlining `rich` markup into the row-building loops.

## If asked to extend this

- **Relationship size / RI violations**: use `TMSCHEMA_RELATIONSHIPS`
  for metadata + `DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS` filtered to
  `TABLE_ID` prefixed `R$` for size, same join pattern as hierarchies —
  and remember to route through the `R$` pseudo-table's own column
  metadata to resolve by name, exactly like `H$`, rather than trusting
  `COLUMN_ID` to line up with the real table's own numbering.
- **Direct Lake / DirectQuery models**: the segment-based size DMVs
  don't apply the same way (Direct Lake pages columns into memory on
  demand rather than storing full VertiPaq segments). Don't silently
  reuse the Import-mode logic for these — flag it as unsupported or
  branch on model storage mode first.
- **Publishing to the Power BI service** (not local Desktop): would
  need an XMLA endpoint connection string instead of `localhost:<port>`
  discovery — that's a different, simpler connection path (no process
  scanning needed) but requires Premium/Fabric capacity and different
  auth (Azure AD), so keep it as a separate connection mode rather than
  bolting it onto `pbi_discover.py`.
- Keep new DMV-derived metrics joined in pandas, not by writing wider
  DMV SQL — the engine's DMV dialect won't support it.

## Testing

There's no automated test suite here — the only way to fully validate
changes end-to-end is running `uv run python analyze_pbix.py` against a
real, open Power BI Desktop file on Windows (this sandbox can't
exercise the actual ADOMD.NET connection: Linux, no Power BI Desktop,
no .NET AS engine, and no `uv sync` without PyPI network access).

For logic changes to `vertipaq_metrics.py` specifically (e.g. the
hierarchy-size join), prefer a synthetic-DMV unit test over guessing:
build small `pandas.DataFrame`s shaped like real
`DISCOVER_STORAGE_TABLES` / `DISCOVER_STORAGE_TABLE_COLUMNS` /
`DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS` output (including a `H$...`
pseudo-table row whose `COLUMN_ID` sequence deliberately does *not*
line up with the real table's own `COLUMN_ID`s, to catch the exact bug
class described above), stub `PbiConnection.query_dmv`/`query_dax` to
return them, and assert on the resulting DataFrame. This is how the
hierarchy-size fix in this session was verified without a live PBIX.

At minimum, always run `python -m py_compile *.py` (or `uv run python
-m py_compile *.py`) to catch syntax errors before handing back to the
user.
