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

## Setup

```bash
pip install pythonnet pandas psutil openpyxl
```

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

## Usage

1. Open your `.pbix` file in Power BI Desktop and let it fully load.
2. Run:

```bash
python analyze_pbix.py
```

If more than one Power BI file is open, it lists them so you can pick
one. Add `--export model_metrics.xlsx` to save the full column-level
and table-level breakdown to Excel.

Other options:

```bash
python analyze_pbix.py --port 54321          # skip discovery, connect directly
python analyze_pbix.py --no-cardinality      # faster, skips the DISTINCTCOUNT pass
python analyze_pbix.py --export metrics.csv
```

## Notes / limitations

- **Hierarchy Size** is derived by matching the engine's internal
  per-column "H$" structures back to columns via `COLUMN_ID`. Microsoft
  documents `COLUMN_ID` as "for internal use," so treat this figure as
  a very close estimate rather than a guaranteed-exact number — this
  matches how most community VertiPaq-analyzer scripts do it too, since
  there's no cleaner documented DMV for it.
- Calculated columns/tables and Direct Lake / DirectQuery sources
  behave differently (Direct Lake in particular doesn't page the same
  way); this script targets standard Import-mode models, same as
  DAX Studio's default metrics view.
- Cardinality is computed with one DAX query per table (all its
  columns as measures in a single row), which is quick for most models.
  Very wide fact tables with dozens of high-cardinality columns will
  take longer — use `--no-cardinality` if you just want size metrics.
