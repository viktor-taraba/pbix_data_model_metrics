# Mini VertiPaq Analyzer (a tiny DAX Studio, in Python)

Connects to the local Analysis Services engine that Power BI Desktop
runs in the background for an open `.pbix` file, and extracts the same
per-column size metrics DAX Studio's "View Metrics" shows: **Cardinality,
Rows, Data Size, Dictionary Size, Hierarchy Size, Total Size**.

**Windows only.** Power BI Desktop's local model engine (`msmdsrv.exe`)
only runs on Windows, and there's no local network endpoint to it other
than ADOMD.NET / AMO (the same client libraries DAX Studio itself uses).

## How it works

1. **Discovery** (`pbi_discover.py`) - scans running processes for
   `msmdsrv.exe` instances launched by Power BI Desktop, and reads the
   port number Windows assigned it from
   `...\AnalysisServicesWorkspaces\<guid>\Data\msmdsrv.port.txt`. Also
   does a best-effort lookup of the actual .pbix file's name/path/size
   by inspecting the parent Power BI Desktop process's open file
   handles (it keeps the .pbix locked while editing).
2. **Connection** (`pbi_connection.py`) - opens an ADOMD.NET connection
   to `localhost:<port>` via `pythonnet`, auto-detecting the catalog
   (database) name if you don't supply one.
3. **Metrics** (`vertipaq_metrics.py`) - runs the same Dynamic
   Management View (DMV) queries DAX Studio/VertiPaq Analyzer use:
   - `DISCOVER_STORAGE_TABLES` — table row counts
   - `DISCOVER_STORAGE_TABLE_COLUMNS` — dictionary size, data type, encoding
   - `DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS` — compressed data size, and
     (for the engine's internal "H$" pseudo-tables) hierarchy size
   - plus one `EVALUATE ROW(..., DISTINCTCOUNT(...))` DAX query per table
     to get **exact** cardinality (DMVs alone don't expose this reliably)
4. **Reporting** (`pbi_report.py`) - renders the metrics to the console
   as colorized tables via `rich` (falls back to plain text if `rich`
   isn't installed or `--no-color` is passed).

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency
management (`pyproject.toml` + `uv.lock`) — there's no separate
`requirements.txt` and dependencies are not installed with `pip install`.

```bash
uv sync
```

This creates/updates a local `.venv` with the exact locked versions of
`pandas`, `openpyxl`, `psutil`, `python-dotenv`, `pythonnet`, and `rich`
(used for the colorized console output - see "Console output" below).
Run project scripts through `uv run` so they pick up that environment
automatically, e.g.:

```bash
uv run python analyze_pbix.py
```

(If you add or bump a dependency, edit `pyproject.toml`'s
`dependencies` list and run `uv lock` to refresh `uv.lock`, then
`uv sync` again — don't hand-edit `uv.lock`. This was just done to add
`rich`, so `uv sync` needs to be re-run once after pulling that change.)

You also need the ADOMD.NET client DLL. If Power BI Desktop is
installed normally, it's already on your machine and the script finds
it automatically at:

```
C:\Program Files\Microsoft Power BI Desktop\bin\ADOMD\Microsoft.AnalysisServices.AdomdClient.dll
```

If not found, download the "AMO and ADOMD.NET client libraries" from
Microsoft (search that phrase on learn.microsoft.com), install it, then
either let auto-discovery find it under
`C:\Program Files\Microsoft.NET\ADOMD.NET\<version>\`, or point at it
explicitly:

```bash
set ADOMD_DLL_PATH=C:\path\to\Microsoft.AnalysisServices.AdomdClient.dll
```

(`pbi_connection.py` also reads this from a `.env` file via
`python-dotenv`, if you'd rather set it there than as a real
environment variable.)

## Usage

1. Open your `.pbix` file in Power BI Desktop and let it fully load.
2. Run:

```bash
uv run python analyze_pbix.py
```

If more than one Power BI file is open, it lists them so you can pick
one. Add `--export model_metrics.xlsx` to save the full column-level
and table-level breakdown to Excel.

Other options:

```bash
uv run python analyze_pbix.py --port 54321          # skip discovery, connect directly
uv run python analyze_pbix.py --no-cardinality      # faster, skips the DISTINCTCOUNT pass
uv run python analyze_pbix.py --export metrics.csv
uv run python analyze_pbix.py --export metrics.xlsx --top 50   # show/print more columns in the console top-N section
uv run python analyze_pbix.py --no-color            # plain text output, no colors
```

### Console output

Console output is colorized via [`rich`](https://github.com/Textualize/rich)
(`pbi_report.py`) - similar in spirit to a custom colored logger: bigger,
heavier columns/tables are shown in warmer colors, encodings are
color-coded, and columns whose cardinality is a large fraction of their
table's row count (i.e. "expensive", hard-to-compress columns) are
flagged. If `rich` isn't installed, or if you pass `--no-color`, every
section below still prints - just as plain, uncolored text.

Color legend:

| What | Meaning |
|---|---|
| Size cells (Data/Dictionary/Hierarchy/Total) | bold red ≥ 1 MB, yellow ≥ 200 KB, green ≥ 20 KB, dim below that |
| `% of DB` (table summary) | bold red ≥ 25%, yellow ≥ 10%, green ≥ 1%, dim below that |
| `Encoding` | `VALUE` = bold green, `HASH` = cyan, `UNKNOWN` = bold red |
| `Cardinality` | white ≤ 100k, yellow 100k–1M, red > 1M, dark red > 2M |
| Size Distribution bar (model summary) | same scale as size cells - the bar's own color reflects that table's Total Size |

Each run prints five sections:

0. **MODEL SUMMARY** — a small panel with the .pbix file's name and
   size on disk (detected by inspecting Power BI Desktop's open file
   handles — see "PBIX file detection" below), the total model size in
   memory (sum of every column's Data + Dictionary + Hierarchy size),
   the model's last data refresh timestamp (see "Last data refresh"
   below), and the table/column counts — followed by **SIZE
   DISTRIBUTION BY TABLE**, a horizontal bar chart of each table's
   total size (biggest first), with row count and column count as
   data labels next to the size.
1. **TABLE SUMMARY** — one row per table, rolled up from the column
   metrics (row count, total data/dictionary/hierarchy/total size,
   column count, % of DB).
2. **TOP N COLUMNS BY TOTAL SIZE** — the `--top` (default 25) largest
   columns in the whole model, for a quick "what's eating my model"
   view.
3. **ALL TABLES & COLUMNS (sorted by Total Size, then Cardinality)** —
   every column across every table in one flat list, sorted purely by
   size (ties broken by cardinality) rather than grouped per table —
   this mirrors DAX Studio's own VertiPaq Analyzer "Columns" tab, which
   also lists the whole model flat and sorted by size rather than
   nested under each table.
4. **ALL TABLES & COLUMNS (grouped by Table, biggest table first)** —
   the opposite grouping from #3: tables are grouped together and
   ordered biggest-table-first (by the table's own Total Size), and
   *within* each table its fields are ordered by their own Total Size,
   largest first. Useful when you want to review a model table-by-table
   rather than as one flat, cross-table list.

When exporting to `.xlsx`, all of the above are written out as
separate sheets: `Overview` (the model summary numbers), `Tables`,
`Columns` (per-table natural order matching the column metrics' own
sort), `AllColumnsBySize` (section 3, flat and size-sorted), and
`ByTableThenField` (section 4, grouped/ordered as above).

Note on "PBIX file detection": the name/size shown come from inspecting
which file handles the parent Power BI Desktop process has open (it
keeps the .pbix locked for the whole editing session), not from any
DMV. This works even when connecting via `--port` directly, as long as
that port still matches a running, discoverable instance. It shows as
"unknown" for a never-saved ("Untitled.pbix") report, or if Windows
denies the handle-enumeration call this relies on.

Note on "last data refresh": this is looked up via three documented
rowsets - `$SYSTEM.TMSCHEMA_PARTITIONS`'s `RefreshedTime`,
`$SYSTEM.MDSCHEMA_CUBES`'s `LAST_DATA_UPDATE`, and
`$SYSTEM.TMSCHEMA_TABLES`'s `ModifiedTime` - and **all three are
checked, not just the first one that answers**; whichever source(s)
return a value, the most recent one wins. This matters: a real case
had a partition's `RefreshedTime` succeed with a *stale*, years-old
answer (a dimension table that hadn't been reprocessed in a while), so
stopping at the first non-null source (an earlier version of this
lookup did) would have silently picked the stale one instead of
checking whether a later value existed elsewhere.

(A fourth candidate, `$SYSTEM.DBSCHEMA_CATALOGS`'s `DATE_MODIFIED` -
the whole database's own last-modified timestamp - was tried and
removed: it tracks the database being touched *at all*, which isn't
necessarily a real data refresh - e.g. it can be bumped just by having
the file open or querying its metadata - so it produced misleading
results and isn't used here.)

If it still shows "unknown", none of the three had anything usable -
and that's no longer silent either: run without `--no-color` piped
through, and a `[!] Could not determine last data refresh` block on
stderr lists the *specific* reason each source didn't work (query
error, empty result, missing column, or an all-null column, complete
with a sample raw value and its type when that happens). If multiple
sources *did* return a value but they disagreed, you'll instead see a
`[i] Last data refresh: sources disagreed, used the most recent`
block listing every candidate found and which one was used - this
only appears when there's an actual disagreement worth knowing about,
not on a routine run where only one source succeeds.

One real cause already found and fixed this way: all sources came back
"present but every value was null/unparseable" *simultaneously* across
multiple unrelated DMVs at once - a strong signal it wasn't the data
genuinely being empty everywhere, but a systemic bug. It turned out
`.NET DateTime` values from `AdomdDataReader.GetValue()` weren't
always being marshalled into Python `datetime` objects by pythonnet,
so `pandas.to_datetime()` silently turned every one of them into `NaT`
with no error. `pbi_connection.py`'s `query_dmv()` now explicitly
converts `DateTime`/`DBNull` CLR values before they reach pandas
(`_convert_dmv_value()`).

## Notes / limitations

- **The internal `RowNumber` column is excluded from every count and
  size total**, matching what a user actually wants to see (a table's
  real, user-facing columns) rather than the engine's own internal
  bookkeeping column. This is a deliberate choice, not an oversight -
  don't "fix" a reported column-count mismatch against another tool by
  re-including it; that specific theory was tried and disproven (see
  `CLAUDE.md`).
- **Sizes match DAX Studio almost exactly.** Side-by-side verification
  against DAX Studio's VertiPaq Analyzer (Human Resources sample model)
  showed Data Size and Dictionary Size matching byte-for-byte on every
  column checked. Displayed MB/GB/TB values use 3 decimal places (not
  1) specifically so this kind of comparison is actually possible -
  "4.2 MB" hides a ±50 KB range, "4.169 MB" doesn't. The one small,
  real gap found was in Hierarchy Size for a handful of very-low-
  cardinality columns (tens of bytes out of megabyte-scale tables,
  e.g. 32 vs 64 bytes) - immaterial for identifying what's actually
  consuming space in a model, and not chased further without a
  verified root cause to fix rather than guess at.
- **Hierarchy Size** is derived from the engine's internal per-column
  "H$" pseudo-table structures. Getting this right required routing the
  join through the "H$" pseudo-table's *own* column metadata rather than
  matching directly on `COLUMN_ID`: `COLUMN_ID` is only unique *within*
  a `TABLE_ID`, and the "H$" pseudo-table numbers its own columns
  independently of the real table's numbering, so the two `COLUMN_ID`
  sequences generally don't correspond to the same column. The fix
  joins hierarchy segment sizes to the "H$" pseudo-table's own metadata
  (both scoped to that pseudo-table's `TABLE_ID`), which resolves to the
  real column's *name*, and then matches real columns by `(Table,
  Column)` name instead of by `COLUMN_ID`. Because Microsoft still
  documents `COLUMN_ID` as "for internal use," treat this figure as a
  very close estimate rather than a guaranteed-exact number.
- Calculated columns/tables and Direct Lake / DirectQuery sources
  behave differently (Direct Lake in particular doesn't page the same
  way); this script targets standard Import-mode models, same as
  DAX Studio's default metrics view.
- Cardinality is computed with one DAX query per table (all its
  columns as measures in a single row), which is quick for most models.
  Very wide fact tables with dozens of high-cardinality columns will
  take longer — use `--no-cardinality` if you just want size metrics.
- If the `Cardinality` column comes back entirely blank (`-` for every
  column), that's no longer silent: the tool prints a
  `[!] Cardinality could not be determined for N of M columns` summary
  to stderr with the actual underlying error for each failure. Read
  that message - it'll point at the real cause (a genuine DAX error,
  a permissions/role issue, etc.) rather than you having to guess.
