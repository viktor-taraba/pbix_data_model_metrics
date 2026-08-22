"""
analyze_pbix.py
----------------
Mini DAX Studio "View Metrics" clone.

Usage:
    python analyze_pbix.py                 # auto-discover open Power BI files, pick one
    python analyze_pbix.py --port 54321    # connect directly to a known port
    python analyze_pbix.py --export out.xlsx
    python analyze_pbix.py --no-cardinality  # skip the (slower) DAX distinct-count pass

Requires Power BI Desktop to be open with the file already loaded, on
Windows, with pythonnet + ADOMD.NET client available (see README.md).
"""

from __future__ import annotations

import argparse
import sys
import pandas as pd

from pbi_discover import find_all
from pbi_connection import PbiConnection
from vertipaq_metrics import get_column_metrics, get_table_summary


def human_bytes(n) -> str:
    if pd.isna(n):
        return "-"
    n = float(n)
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


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
    args = parser.parse_args()

    if args.port:
        port = args.port
    else:
        inst = choose_instance()
        port = inst.port
        print(f"\nConnecting to localhost:{port} ...")

    with PbiConnection(port=port, database=args.database, adomd_dll_path=args.adomd_dll) as conn:
        print(f"Connected to database: {conn.database}\n")
        print("Extracting storage metrics ...")
        col_metrics = get_column_metrics(conn, include_cardinality=not args.no_cardinality)
        table_summary = get_table_summary(col_metrics)

    # ---- console output ----
    pd.set_option("display.width", 140)

    print("\n=== TABLE SUMMARY ===")
    disp = table_summary.copy()
    for c in ["DataSize", "DictionarySize", "HierarchySize", "TotalSize"]:
        disp[c] = disp[c].map(human_bytes)
    print(disp.to_string(index=False))

    print(f"\n=== TOP {args.top} COLUMNS BY TOTAL SIZE ===")
    disp_cols = col_metrics.sort_values("TotalSize", ascending=False).head(args.top).copy()
    for c in ["DataSize", "DictionarySize", "HierarchySize", "TotalSize"]:
        disp_cols[c] = disp_cols[c].map(human_bytes)
    print(disp_cols.to_string(index=False))

    # DAX Studio's own "Columns" tab of VertiPaq Analyzer doesn't group by
    # table at all - it lists every column across the whole model in one
    # flat list, sorted by size. This mirrors that: every table and every
    # field, sorted by Total Size (descending), with Cardinality as the
    # tie-breaker for columns that land on the same size.
    sort_cols = ["TotalSize"]
    if "Cardinality" in col_metrics.columns:
        sort_cols.append("Cardinality")
    print(f"\n=== ALL TABLES & COLUMNS (sorted by Total Size, then Cardinality) - {len(col_metrics)} columns ===")
    disp_all = col_metrics.sort_values(sort_cols, ascending=False).copy()
    for c in ["DataSize", "DictionarySize", "HierarchySize", "TotalSize"]:
        disp_all[c] = disp_all[c].map(human_bytes)
    print(disp_all.to_string(index=False))

    if args.export:
        if args.export.lower().endswith(".xlsx"):
            with pd.ExcelWriter(args.export) as writer:
                table_summary.to_excel(writer, sheet_name="Tables", index=False)
                col_metrics.to_excel(writer, sheet_name="Columns", index=False)
                col_metrics.sort_values(sort_cols, ascending=False).to_excel(
                    writer, sheet_name="AllColumnsBySize", index=False
                )
        else:
            col_metrics.to_csv(args.export, index=False)
        print(f"\nExported full results to {args.export}")


if __name__ == "__main__":
    main()