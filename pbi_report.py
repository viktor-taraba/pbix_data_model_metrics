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
    """Format a byte count for display. B/KB stay at 1 decimal place
    (sub-KB precision beyond that isn't meaningful); MB/GB/TB use 3
    decimal places, since 1 decimal at that scale hides real differences
    of tens of KB (e.g. "4.2 MB" could be anywhere from ~4.15-4.25 MB) -
    3 decimals make those actually visible when comparing against another
    tool's numbers instead of masking them behind rounding."""
    if pd.isna(n):
        return "-"
    n = float(n)
    if n < 1024:
        return f"{n:,.1f} B"
    n /= 1024
    if n < 1024:
        return f"{n:,.1f} KB"
    n /= 1024
    if n < 1024:
        return f"{n:,.3f} MB"
    n /= 1024
    if n < 1024:
        return f"{n:,.3f} GB"
    n /= 1024
    return f"{n:,.3f} TB"


def _size_style(n_bytes) -> str:
    """Color scale for an absolute byte count: heavier = warmer.

    Thresholds: bold red >= 5 MB, yellow >= 3 MB, green >= 1 MB, dim
    below that (and for NaN/missing)."""
    if pd.isna(n_bytes):
        return "dim"
    n_bytes = float(n_bytes)
    if n_bytes >= 5 * 1024 * 1024:    # >= 5 MB
        return "bold red"
    if n_bytes >= 3 * 1024 * 1024:    # >= 3 MB
        return "yellow"
    if n_bytes >= 1 * 1024 * 1024:    # >= 1 MB
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
    """Section 0 of the report: pbix file name/size, total in-memory
    size, last data refresh, and table/column counts - the "what is this
    file and how big/fresh is it" glance before diving into any
    per-table/per-column detail."""
    pbix_name = summary.get("PbixName") or "unknown (unsaved report, or file couldn't be located)"
    pbix_size_txt = (
        human_bytes(summary.get("PbixSizeBytes"))
        if summary.get("PbixSizeBytes") is not None
        else "-"
    )
    total_size_txt = human_bytes(summary.get("TotalSize"))
    refresh_txt = _format_last_refresh(summary.get("LastDataRefresh"))
    num_tables = summary.get("NumTables", "-")
    num_columns = summary.get("NumColumns", "-")

    if not RICH_AVAILABLE:
        print(f"\n=== {title} ===")
        print(f"PBIX file:                     {pbix_name}  ({pbix_size_txt})")
        print(f"Total model size (in memory):  {total_size_txt}")
        print(f"Last data refresh:             {refresh_txt}")
        print(f"Tables:                        {num_tables}")
        print(f"Columns:                       {num_columns}")
        return

    size_style = _size_style(summary.get("TotalSize"))
    pbix_size_style = _size_style(summary.get("PbixSizeBytes"))
    body = (
        f"[bold]PBIX file:[/bold] {pbix_name}  "
        f"([{pbix_size_style}]{pbix_size_txt}[/{pbix_size_style}] on disk)\n"
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


def print_column_size_distribution(
    col_metrics: pd.DataFrame,
    total_model_size: float | int | None = None,
    title: str = "TOP 10 COLUMNS BY TOTAL SIZE",
    top_n: int = 10,
    bar_width: int = 30,
) -> None:
    """Column-level companion to print_table_size_distribution(): a
    horizontal bar chart of the `top_n` (default 10) biggest columns in
    the whole model by TotalSize, biggest first, alongside each column's
    own % of the whole model's size AND the running cumulative % of the
    model's total size accounted for once you include that column and
    every column listed above it.

    That cumulative figure is the point of this section: a single
    column's own % of DB doesn't answer "how much of my model's bulk is
    concentrated in a handful of columns" - the cumulative line does
    (e.g. "the top 10 columns together account for 78% of this model").

    `col_metrics` should be the FULL, un-truncated per-column metrics
    DataFrame (this function does its own top-N selection/sorting) -
    passing an already-truncated or already-sorted-differently frame will
    silently produce a truncated/incorrect top-N and cumulative total.

    total_model_size: the whole model's total size in bytes, i.e.
    the same number shown in MODEL SUMMARY
    (model_summary["TotalSize"] / column_metrics["TotalSize"].sum()
    computed over the FULL model). Pass this explicitly - it's the
    correct denominator for "% of DB" even if a caller ever passes a
    filtered/partial `col_metrics` in for some other reason. Falls back
    to summing the given `col_metrics` if omitted, which is only correct
    when the full model's column metrics were passed in.
    """
    if col_metrics.empty:
        return

    top = col_metrics.sort_values("TotalSize", ascending=False).head(top_n).copy()
    if top.empty:
        return

    if total_model_size is None:
        total_model_size = float(col_metrics["TotalSize"].sum())
    total_model_size = float(total_model_size) or 1.0

    top["CumulativeSize"] = top["TotalSize"].cumsum()
    top["CumulativePct"] = (top["CumulativeSize"] / total_model_size * 100).clip(upper=100.0)
    top["PctOfDb"] = top["TotalSize"] / total_model_size * 100

    max_size = float(top["TotalSize"].max()) or 1.0

    if not RICH_AVAILABLE:
        print(f"\n=== {title} ===")
        for _, row in top.iterrows():
            filled = int(round(float(row["TotalSize"]) / max_size * bar_width))
            bar = ("#" * filled).ljust(bar_width)
            label = f"{row['Table']}[{row['Column']}]"
            print(
                f"{label[:38]:<38} {bar} "
                f"{human_bytes(row['TotalSize']):>10}  "
                f"({row['PctOfDb']:.2f}% of DB, cum. {row['CumulativePct']:.2f}%)"
            )
        return

    t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold")
    t.add_column("Table", style="bold")
    t.add_column("Column")
    t.add_column("Size Distribution", no_wrap=True)
    t.add_column("Total Size", justify="right")
    t.add_column("% of DB", justify="right")
    t.add_column("Cumulative %", justify="right")

    for _, row in top.iterrows():
        filled = int(round(float(row["TotalSize"]) / max_size * bar_width))
        filled = max(0, min(bar_width, filled))
        bar_style = _size_style(row["TotalSize"])
        bar = f"[{bar_style}]{'█' * filled}[/{bar_style}][dim]{'░' * (bar_width - filled)}[/dim]"
        total_txt, total_style = _sized_cell(row["TotalSize"])
        pct_style = _pct_style(row["PctOfDb"])
        cum_style = _pct_style(row["CumulativePct"])
        t.add_row(
            str(row["Table"]),
            str(row["Column"]),
            bar,
            f"[{total_style}]{total_txt}[/{total_style}]",
            f"[{pct_style}]{row['PctOfDb']:.2f}%[/{pct_style}]",
            f"[{cum_style}]{row['CumulativePct']:.2f}%[/{cum_style}]",
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
