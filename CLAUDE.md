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
| `pbi_discover.py` | Finds running `msmdsrv.exe` processes launched by Power BI Desktop, reads `msmdsrv.port.txt` from the per-file workspace folder under `%LOCALAPPDATA%\Microsoft\Power BI Desktop\AnalysisServicesWorkspaces\` to get the TCP port. Falls back to scanning that folder directly if process introspection fails (e.g. permissions). |
| `pbi_connection.py` | Thin ADOMD.NET wrapper via `pythonnet` (`clr.AddReference`). Locates `Microsoft.AnalysisServices.AdomdClient.dll` (bundled with Power BI Desktop, or the standalone AMO/ADOMD.NET redistributable), opens a connection, auto-detects the catalog/database name via `$SYSTEM.DBSCHEMA_CATALOGS` if not given. Exposes `query_dmv()` and `query_dax()`, both returning `pandas.DataFrame`. |
| `vertipaq_metrics.py` | The actual analyzer logic. Pulls `DISCOVER_STORAGE_TABLES`, `DISCOVER_STORAGE_TABLE_COLUMNS`, `DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS` DMVs and joins them in pandas (DMV SQL doesn't support real joins). Runs one `EVALUATE ROW(..., DISTINCTCOUNT(...))` DAX query per table for exact cardinality. Returns tidy per-column and per-table DataFrames, plus `order_by_table_size_then_field_size()` for the table-grouped ordering used in console section 4 / export sheet `ByTableThenField`. |
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
- **CLI console output** (`analyze_pbix.py` + `pbi_report.py`) has four
  sections, in order: `TABLE SUMMARY` (per-table rollup), `TOP {--top}
  COLUMNS BY TOTAL SIZE` (capped, for a quick glance), `ALL TABLES &
  COLUMNS (sorted by Total Size, then Cardinality)` (the *full*,
  uncapped column list, sorted flat across the whole model rather than
  grouped per table - mirrors DAX Studio's own VertiPaq Analyzer
  "Columns" tab), and `ALL TABLES & COLUMNS (grouped by Table, biggest
  table first)` (tables grouped and ordered by each table's own Total
  Size descending, fields within a table ordered by their own Total
  Size descending - built by
  `vertipaq_metrics.order_by_table_size_then_field_size()`).
  `--export .xlsx` mirrors this with four sheets: `Tables`, `Columns`
  (natural per-table order), `AllColumnsBySize` (section 3),
  `ByTableThenField` (section 4). If you change the sort/columns of
  one, keep the export sheet and the console section consistent with
  each other.
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
