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

## File map

| File | Responsibility |
|---|---|
| `pbi_discover.py` | Finds running `msmdsrv.exe` processes launched by Power BI Desktop, reads `msmdsrv.port.txt` from the per-file workspace folder under `%LOCALAPPDATA%\Microsoft\Power BI Desktop\AnalysisServicesWorkspaces\` to get the TCP port. Falls back to scanning that folder directly if process introspection fails (e.g. permissions). |
| `pbi_connection.py` | Thin ADOMD.NET wrapper via `pythonnet` (`clr.AddReference`). Locates `Microsoft.AnalysisServices.AdomdClient.dll` (bundled with Power BI Desktop, or the standalone AMO/ADOMD.NET redistributable), opens a connection, auto-detects the catalog/database name via `$SYSTEM.DBSCHEMA_CATALOGS` if not given. Exposes `query_dmv()` and `query_dax()`, both returning `pandas.DataFrame`. |
| `vertipaq_metrics.py` | The actual analyzer logic. Pulls `DISCOVER_STORAGE_TABLES`, `DISCOVER_STORAGE_TABLE_COLUMNS`, `DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS` DMVs and joins them in pandas (DMV SQL doesn't support real joins). Runs one `EVALUATE ROW(..., DISTINCTCOUNT(...))` DAX query per table for exact cardinality. Returns tidy per-column and per-table DataFrames. |
| `analyze_pbix.py` | CLI entry point. Auto-discovers/prompts for a running instance, prints console tables, optional `--export metrics.xlsx`/`.csv`. |
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
- **Hierarchy Size**: `SUM(USED_SIZE)` from segments whose `TABLE_ID`
  starts with `H$`, joined back to the real column by matching
  `(DIMENSION_NAME, COLUMN_ID)` together — **not** `COLUMN_ID` alone.
  `COLUMN_ID` is only unique *within* a table, not across the model, so
  a global join on `COLUMN_ID` silently merges unrelated columns in
  different tables that happen to share a small internal ID (0, 1, 2...),
  producing wildly inflated, duplicated numbers (this was a real bug,
  fixed once — don't reintroduce it). Still flagged as an estimate in
  the README since `COLUMN_ID` itself is undocumented for this purpose.
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
- DMV query syntax is DMX-based SQL and does **not** support `JOIN`,
  `GROUP BY`, `LIKE`, `CAST`/`CONVERT` — all correlation across DMVs
  happens in pandas, not in the DMV query text.

## If asked to extend this

- **Relationship size / RI violations**: use `TMSCHEMA_RELATIONSHIPS`
  for metadata + `DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS` filtered to
  `TABLE_ID` prefixed `R$` for size, same join pattern as hierarchies.
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

There's no automated test suite here — the only way to validate
changes is running `analyze_pbix.py` against a real, open Power BI
Desktop file on Windows. When making non-trivial changes, at least
run `python -m py_compile *.py` to catch syntax errors before handing
back to the user, since this sandbox can't exercise the actual
ADOMD.NET connection (Linux, no Power BI Desktop, no .NET AS engine).
