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
   `...\AnalysisServicesWorkspaces\<guid>\Data\msmdsrv.port.txt`.
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

Each run prints four sections:

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

When exporting to `.xlsx`, all four views are written out as separate
sheets: `Tables`, `Columns` (per-table natural order matching the
column metrics' own sort), `AllColumnsBySize` (section 3, flat and
size-sorted), and `ByTableThenField` (section 4, grouped/ordered as
above).

## Notes / limitations

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
