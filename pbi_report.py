"""
pbi_report.py
--------------
Colorized console rendering for the VertiPaq metrics report, using
`rich`. This mirrors (loosely) the color cues DAX Studio's VertiPaq
Analyzer uses in its own grid - bigger/heavier things stand out in
warmer colors, encodings are color-coded, and "expensive" high-cardinality
columns are flagged - so a glance at the terminal tells you where the
model's size is actually going, the same way custom colored loggers
highlight warnings/errors instead of printing flat text.

If `rich` isn't installed, every function in here degrades to plain,
uncolored `pandas.DataFrame.to_string()` output - the report still works,
it's just monochrome.
"""

from __future__ import annotations

import pandas as pd

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via the fallback path
    RICH_AVAILABLE = False

_SIZE_COLUMNS = ["DataSize", "DictionarySize", "HierarchySize", "TotalSize"]

_console: "Console | None" = Console() if RICH_AVAILABLE else None


def human_bytes(n) -> str:
    if pd.isna(n):
        return "-"
    n = float(n)
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


def _size_style(n_bytes) -> str:
    """Color scale for an absolute byte count: heavier = warmer."""
    if pd.isna(n_bytes):
        return "dim"
    n_bytes = float(n_bytes)
    if n_bytes >= 1024 * 1024:        # >= 1 MB
        return "bold red"
    if n_bytes >= 200 * 1024:         # >= 200 KB
        return "yellow"
    if n_bytes >= 20 * 1024:          # >= 20 KB
        return "green"
    return "dim"


def _pct_style(pct) -> str:
    """Color scale for a '% of DB' style figure."""
    if pd.isna(pct):
        return "dim"
    pct = float(pct)
    if pct >= 25:
        return "bold red"
    if pct >= 10:
        return "yellow"
    if pct >= 1:
        return "green"
    return "dim"


def _encoding_style(enc) -> str:
    return {
        "VALUE": "bold green",
        "HASH": "cyan",
        "UNKNOWN": "bold red",
    }.get(enc, "white")


def _cardinality_style(cardinality) -> str:
    """Color scale based on the absolute cardinality value:
    white up to 100k, yellow from 100k to 1M, red above 1M, dark red
    above 2M - independent of the table's row count."""
    if pd.isna(cardinality):
        return "white"
    c = float(cardinality)
    if c > 2_000_000:
        return "bold dark_red"
    if c > 1_000_000:
        return "bold red"
    if c >= 100_000:
        return "yellow"
    return "white"


def _sized_cell(value) -> "tuple[str, str]":
    return human_bytes(value), _size_style(value)


def _format_last_refresh(ts) -> str:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return "unknown"
    try:
        ts = pd.Timestamp(ts)
        if pd.isna(ts):
            return "unknown"
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def print_model_summary(summary: dict, title: str = "MODEL SUMMARY") -> None:
    """Section 0 of the report: total in-memory size, last data refresh,
    and table/column counts - the "how big is this thing and how fresh is
    it" glance before diving into any per-table/per-column detail."""
    total_size_txt = human_bytes(summary.get("TotalSize"))
    refresh_txt = _format_last_refresh(summary.get("LastDataRefresh"))
    num_tables = summary.get("NumTables", "-")
    num_columns = summary.get("NumColumns", "-")

    if not RICH_AVAILABLE:
        print(f"\n=== {title} ===")
        print(f"Total model size (in memory): {total_size_txt}")
        print(f"Last data refresh:            {refresh_txt}")
        print(f"Tables:                        {num_tables}")
        print(f"Columns:                       {num_columns}")
        return

    size_style = _size_style(summary.get("TotalSize"))
    body = (
        f"[bold]Total model size (in memory):[/bold] [{size_style}]{total_size_txt}[/{size_style}]\n"
        f"[bold]Last data refresh:[/bold] {refresh_txt}\n"
        f"[bold]Tables:[/bold] {num_tables}    [bold]Columns:[/bold] {num_columns}"
    )
    panel = Panel(body, title=title, border_style="bold", box=box.ROUNDED, expand=False)
    _console.print()
    _console.print(panel)


def print_table_size_distribution(
    table_summary: pd.DataFrame,
    title: str = "SIZE DISTRIBUTION BY TABLE",
    bar_width: int = 30,
) -> None:
    """A simple horizontal bar chart of each table's TotalSize, with row
    count and column count as data labels alongside the size - a quick
    visual read of where the model's bulk actually lives, before the
    detailed per-column sections below. `table_summary` is assumed already
    sorted biggest-first (get_table_summary()'s own default)."""
    if table_summary.empty:
        return

    max_size = float(table_summary["TotalSize"].max()) or 1.0

    if not RICH_AVAILABLE:
        print(f"\n=== {title} ===")
        for _, row in table_summary.iterrows():
            filled = int(round(float(row["TotalSize"]) / max_size * bar_width))
            bar = ("#" * filled).ljust(bar_width)
            print(
                f"{str(row['Table'])[:22]:<22} {bar} "
                f"{human_bytes(row['TotalSize']):>10}  "
                f"({int(row['Rows']):,} rows, {int(row['ColumnCount'])} cols)"
            )
        return

    t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold")
    t.add_column("Table", style="bold")
    t.add_column("Size Distribution", no_wrap=True)
    t.add_column("Total Size", justify="right")
    t.add_column("Rows", justify="right")
    t.add_column("Columns", justify="right")

    for _, row in table_summary.iterrows():
        filled = int(round(float(row["TotalSize"]) / max_size * bar_width))
        filled = max(0, min(bar_width, filled))
        bar_style = _size_style(row["TotalSize"])
        bar = f"[{bar_style}]{'█' * filled}[/{bar_style}][dim]{'░' * (bar_width - filled)}[/dim]"
        total_txt, total_style = _sized_cell(row["TotalSize"])
        t.add_row(
            str(row["Table"]),
            bar,
            f"[{total_style}]{total_txt}[/{total_style}]",
            f"{int(row['Rows']):,}",
            str(int(row["ColumnCount"])),
        )

    _console.print()
    _console.print(t)


def print_table_summary(table_summary: pd.DataFrame, title: str = "TABLE SUMMARY") -> None:
    if not RICH_AVAILABLE:
        disp = table_summary.copy()
        for c in _SIZE_COLUMNS:
            disp[c] = disp[c].map(human_bytes)
        print(f"\n=== {title} ===")
        print(disp.to_string(index=False))
        return

    t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold")
    t.add_column("Table", style="bold")
    t.add_column("Rows", justify="right")
    t.add_column("Columns", justify="right")
    t.add_column("Data", justify="right")
    t.add_column("Dictionary", justify="right")
    t.add_column("Hierarchy", justify="right")
    t.add_column("Total Size", justify="right")
    t.add_column("% of DB", justify="right")

    for _, row in table_summary.iterrows():
        data_txt, data_style = _sized_cell(row["DataSize"])
        dict_txt, dict_style = _sized_cell(row["DictionarySize"])
        hier_txt, hier_style = _sized_cell(row["HierarchySize"])
        total_txt, total_style = _sized_cell(row["TotalSize"])
        pct_style = _pct_style(row["% of DB"])
        t.add_row(
            str(row["Table"]),
            f"{int(row['Rows']):,}",
            str(int(row["ColumnCount"])),
            f"[{data_style}]{data_txt}[/{data_style}]",
            f"[{dict_style}]{dict_txt}[/{dict_style}]",
            f"[{hier_style}]{hier_txt}[/{hier_style}]",
            f"[{total_style}]{total_txt}[/{total_style}]",
            f"[{pct_style}]{row['% of DB']:.2f}%[/{pct_style}]",
        )
    _console.print()
    _console.print(t)


def print_columns_table(
    col_metrics: pd.DataFrame,
    title: str,
    show_table_column: bool = True,
    show_header_line: bool = True,
) -> None:
    """Render a per-column table. `col_metrics` should already be sorted
    the way it should be displayed - this function doesn't re-sort.

    `show_header_line=False` suppresses this function's own "=== title ==="
    / rich table title, for callers (like print_grouped_by_table) that
    already printed their own header line just above."""
    if not RICH_AVAILABLE:
        disp = col_metrics.copy()
        for c in _SIZE_COLUMNS:
            disp[c] = disp[c].map(human_bytes)
        if show_header_line:
            print(f"\n=== {title} ({len(disp)} columns) ===")
        print(disp.to_string(index=False))
        return

    table_title = f"{title} ({len(col_metrics)} columns)" if show_header_line else None
    t = Table(title=table_title, box=box.SIMPLE_HEAVY, header_style="bold")
    if show_table_column:
        t.add_column("Table", style="bold")
    t.add_column("Column")
    t.add_column("Data Type", style="dim")
    t.add_column("Encoding")
    if "Cardinality" in col_metrics.columns:
        t.add_column("Cardinality", justify="right")
    t.add_column("Rows", justify="right")
    t.add_column("Data", justify="right")
    t.add_column("Dictionary", justify="right")
    t.add_column("Hierarchy", justify="right")
    t.add_column("Total Size", justify="right")

    for _, row in col_metrics.iterrows():
        data_txt, data_style = _sized_cell(row["DataSize"])
        dict_txt, dict_style = _sized_cell(row["DictionarySize"])
        hier_txt, hier_style = _sized_cell(row["HierarchySize"])
        total_txt, total_style = _sized_cell(row["TotalSize"])
        enc_style = _encoding_style(row["Encoding"])

        cells = []
        if show_table_column:
            cells.append(str(row["Table"]))
        cells.append(str(row["Column"]))
        cells.append(str(row["DataType"]))
        cells.append(f"[{enc_style}]{row['Encoding']}[/{enc_style}]")
        if "Cardinality" in col_metrics.columns:
            card_style = _cardinality_style(row["Cardinality"])
            card_val = "-" if pd.isna(row["Cardinality"]) else f"{int(row['Cardinality']):,}"
            cells.append(f"[{card_style}]{card_val}[/{card_style}]")
        cells.append(f"{int(row['Rows']):,}" if not pd.isna(row["Rows"]) else "-")
        cells.append(f"[{data_style}]{data_txt}[/{data_style}]")
        cells.append(f"[{dict_style}]{dict_txt}[/{dict_style}]")
        cells.append(f"[{hier_style}]{hier_txt}[/{hier_style}]")
        cells.append(f"[{total_style}]{total_txt}[/{total_style}]")
        t.add_row(*cells)

    _console.print()
    _console.print(t)


def print_grouped_by_table(
    col_metrics_ordered: pd.DataFrame,
    table_summary: pd.DataFrame,
    title: str = "ALL TABLES & COLUMNS (grouped by Table, biggest table first)",
) -> None:
    """Print one colored sub-table per model table, in the order the
    tables already appear in `col_metrics_ordered` (i.e. biggest table's
    total size first), each field within a table ordered by its own
    TotalSize (also assumed already sorted that way)."""
    table_sizes = table_summary.set_index("Table")["TotalSize"].to_dict()

    if not RICH_AVAILABLE:
        print(f"\n=== {title} ===")
        for tbl in col_metrics_ordered["Table"].drop_duplicates():
            sub = col_metrics_ordered[col_metrics_ordered["Table"] == tbl]
            print(f"\n-- {tbl}  (table total: {human_bytes(table_sizes.get(tbl))}) --")
            disp = sub.copy()
            for c in _SIZE_COLUMNS:
                disp[c] = disp[c].map(human_bytes)
            print(disp.drop(columns=["Table"]).to_string(index=False))
        return

    _console.print()
    _console.rule(f"[bold]{title}[/bold]")
    for tbl in col_metrics_ordered["Table"].drop_duplicates():
        sub = col_metrics_ordered[col_metrics_ordered["Table"] == tbl]
        total = table_sizes.get(tbl)
        total_style = _size_style(total)
        _console.print(
            f"\n[bold]{tbl}[/bold]  "
            f"[dim]({len(sub)} columns, table total:[/dim] "
            f"[{total_style}]{human_bytes(total)}[/{total_style}][dim])[/dim]"
        )
        print_columns_table(sub, title=str(tbl), show_table_column=False, show_header_line=False)
