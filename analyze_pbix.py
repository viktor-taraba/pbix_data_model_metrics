"""
analyze_pbix.py
----------------
Mini DAX Studio "View Metrics" clone.

Usage:
    python analyze_pbix.py                 # auto-discover open Power BI files, pick one
    python analyze_pbix.py --port 54321    # connect directly to a known port
    python analyze_pbix.py --export out.xlsx
    python analyze_pbix.py --no-cardinality  # skip the (slower) DAX distinct-count pass
    python analyze_pbix.py --no-color        # plain text output, no rich/colors

Requires Power BI Desktop to be open with the file already loaded, on
Windows, with pythonnet + ADOMD.NET client available (see README.md).

Console output is colorized via `rich` when it's installed (see
pbi_report.py) - heavier columns/tables show up in warmer colors,
encodings are color-coded, and high-cardinality columns are flagged.
Falls back to plain text automatically if `rich` isn't installed, or
if --no-color is passed.
"""

from __future__ import annotations

import argparse
import sys
import pandas as pd

from pbi_discover import find_all
from pbi_connection import PbiConnection
from vertipaq_metrics import (
    get_column_metrics,
    get_table_summary,
    get_model_summary,
    order_by_table_size_then_field_size,
)
import pbi_report


def choose_instance():
    instances = find_all()
    if not instances:
        print("No running Power BI Desktop instances found.")
        print("Open a .pbix file in Power BI Desktop and try again.")
        sys.exit(1)

    if len(instances) == 1:
        return instances[0]

    print("Multiple Power BI Desktop instances found:\n")
    for i, inst in enumerate(instances):
        print(f"  [{i}] {inst}")
    choice = input("\nPick an instance number: ").strip()
    return instances[int(choice)]


def main():
    parser = argparse.ArgumentParser(description="Mini VertiPaq/DAX Studio size analyzer")
    parser.add_argument("--port", type=int, help="Connect directly to this port instead of auto-discovering")
    parser.add_argument("--database", type=str, help="Catalog/database name (optional if only one exists)")
    parser.add_argument("--adomd-dll", type=str, help="Full path to Microsoft.AnalysisServices.AdomdClient.dll")
    parser.add_argument("--export", type=str, help="Export results to this .xlsx or .csv path")
    parser.add_argument("--no-cardinality", action="store_true",
                         help="Skip the DAX DISTINCTCOUNT pass (faster, no Cardinality column)")
    parser.add_argument("--top", type=int, default=25, help="How many columns to print to console (default 25)")
    parser.add_argument("--no-color", action="store_true",
                         help="Disable colored output even if `rich` is installed")
    args = parser.parse_args()

    if args.no_color:
        pbi_report.RICH_AVAILABLE = False

    if args.port:
        port = args.port
        # We bypassed choose_instance(), but discovery is cheap (just a
        # process scan) - run it anyway purely to enrich the report with
        # the actual .pbix file's name/size if we can match this port to
        # a running instance, rather than leaving those fields blank just
        # because the user already knew the port.
        inst = next((i for i in find_all() if i.port == port), None)
    else:
        inst = choose_instance()
        port = inst.port
        print(f"\nConnecting to localhost:{port} ...")

    with PbiConnection(port=port, database=args.database, adomd_dll_path=args.adomd_dll) as conn:
        print(f"Connected to database: {conn.database}\n")
        print("Extracting storage metrics ...")
        cardinality_warnings: list[str] = []
        col_metrics = get_column_metrics(
            conn,
            include_cardinality=not args.no_cardinality,
            cardinality_warnings=cardinality_warnings,
        )
        table_summary = get_table_summary(col_metrics)
        # Fetched inside the `with` block since it needs another DMV round
        # trip on the still-open connection.
        refresh_diagnostics: list[str] = []
        model_summary = get_model_summary(
            conn, col_metrics, table_summary, refresh_diagnostics=refresh_diagnostics
        )
        # The .pbix file's name/size comes from process introspection
        # (pbi_discover), not the DMV connection - merge it in here so
        # print_model_summary() has one dict with everything it needs.
        model_summary["PbixName"] = getattr(inst, "pbix_name", None) if inst else None
        model_summary["PbixSizeBytes"] = getattr(inst, "pbix_size_bytes", None) if inst else None

    if cardinality_warnings:
        n_failed = len(cardinality_warnings)
        n_total = len(col_metrics)
        print(
            f"\n[!] Cardinality could not be determined for {n_failed} of "
            f"{n_total} columns. First few reasons:",
            file=sys.stderr,
        )
        for msg in cardinality_warnings[:10]:
            print(f"    - {msg}", file=sys.stderr)
        if n_failed > 10:
            print(f"    ... and {n_failed - 10} more.", file=sys.stderr)
        print(
            "    (Those columns show '-' for Cardinality below instead of "
            "silently looking like every column succeeded.)\n",
            file=sys.stderr,
        )

    if refresh_diagnostics:
        if model_summary["LastDataRefresh"] is None:
            print(
                "\n[!] Could not determine last data refresh. Reasons per source tried:",
                file=sys.stderr,
            )
            for msg in refresh_diagnostics:
                print(f"    - {msg}", file=sys.stderr)
            print(
                "    (Shows as 'unknown' in MODEL SUMMARY below instead of silently "
                "picking a wrong date.)\n",
                file=sys.stderr,
            )
        elif any(msg.startswith("Used ") for msg in refresh_diagnostics):
            # This marker only appears when more than one source actually
            # returned a value (see _get_last_data_refresh) - a routine run
            # where only one source succeeds and the rest fail with an
            # expected "column is null" reason does not trigger this, so
            # this block only fires for a genuine, worth-knowing-about
            # disagreement between sources.
            print(
                "\n[i] Last data refresh: sources disagreed, used the most recent:",
                file=sys.stderr,
            )
            for msg in refresh_diagnostics:
                print(f"    - {msg}", file=sys.stderr)
            print(file=sys.stderr)

    # ---- console output (six sections) ----

    # 0. Whole-model summary: total in-memory size, last data refresh,
    # table/column counts, and a size-by-table distribution visual,
    # followed immediately by the column-level equivalent: the top 10
    # columns by Total Size with a cumulative % of the model's total
    # size, so you can see at a glance how concentrated the model's bulk
    # is in just a handful of columns.
    pbi_report.print_model_summary(model_summary)
    pbi_report.print_table_size_distribution(table_summary)
    pbi_report.print_column_size_distribution(
        col_metrics, total_model_size=model_summary["TotalSize"]
    )

    # 1. Per-table rollup.
    pbi_report.print_table_summary(table_summary, title="TABLE SUMMARY")

    # 2. The `--top` biggest columns in the whole model, for a quick glance.
    top_cols = col_metrics.sort_values("TotalSize", ascending=False).head(args.top)
    pbi_report.print_columns_table(top_cols, title=f"TOP {args.top} COLUMNS BY TOTAL SIZE")

    # 3. Every column, flat across the whole model, sorted purely by size
    # (ties broken by cardinality) - mirrors DAX Studio's own VertiPaq
    # Analyzer "Columns" tab, which also isn't grouped by table.
    sort_cols = ["TotalSize"]
    if "Cardinality" in col_metrics.columns:
        sort_cols.append("Cardinality")
    all_by_size = col_metrics.sort_values(sort_cols, ascending=False)
    pbi_report.print_columns_table(
        all_by_size, title="ALL TABLES & COLUMNS (sorted by Total Size, then Cardinality)"
    )

    # 4. Grouped by table - biggest table first, and within each table its
    # fields ordered by their own Total Size, largest first.
    by_table_then_field = order_by_table_size_then_field_size(col_metrics, table_summary)
    pbi_report.print_grouped_by_table(by_table_then_field, table_summary)

    if args.export:
        if args.export.lower().endswith(".xlsx"):
            with pd.ExcelWriter(args.export) as writer:
                overview = pd.DataFrame([{
                    "PBIX Name": model_summary.get("PbixName"),
                    "PBIX Size (bytes)": model_summary.get("PbixSizeBytes"),
                    "Total Model Size (bytes)": model_summary["TotalSize"],
                    "Last Data Refresh": model_summary["LastDataRefresh"],
                    "Tables": model_summary["NumTables"],
                    "Columns": model_summary["NumColumns"],
                }])
                overview.to_excel(writer, sheet_name="Overview", index=False)
                table_summary.to_excel(writer, sheet_name="Tables", index=False)
                col_metrics.to_excel(writer, sheet_name="Columns", index=False)
                all_by_size.to_excel(writer, sheet_name="AllColumnsBySize", index=False)
                by_table_then_field.to_excel(writer, sheet_name="ByTableThenField", index=False)
        else:
            col_metrics.to_csv(args.export, index=False)
        print(f"\nExported full results to {args.export}")


if __name__ == "__main__":
    main()
