"""
vertipaq_metrics.py
--------------------
Re-implements the core of DAX Studio's "View Metrics" / VertiPaq Analyzer
using documented DMVs, plus one DAX query per table for exact cardinality.

Metrics produced per column (matches DAX Studio / VertiPaq Analyzer naming):
    Table, Column, Data Type
    Cardinality        - distinct value count (via DAX DISTINCTCOUNT)
    Rows                - row count of the parent table
    Data Size           - bytes of compressed column data (segments)
    Dictionary Size     - bytes of the value dictionary
    Hierarchy Size      - bytes of the auto-generated attribute hierarchy
    Total Size          - Data + Dictionary + Hierarchy
    Encoding            - VALUE or HASH

DMVs used (all documented by Microsoft):
    $SYSTEM.DISCOVER_STORAGE_TABLES               -> table row counts
    $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMNS         -> per-column dictionary size,
                                                       datatype, encoding, readable
                                                       table/column names
    $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS -> per-column-segment used size
                                                       (summed = Data Size; the
                                                       same rowset also holds the
                                                       "H$" hierarchy structures,
                                                       summed = Hierarchy Size)

Note on Hierarchy Size (IMPORTANT - this was a real bug, fixed once, do not
reintroduce it): the engine stores each column's auto-generated attribute
hierarchy in a separate pseudo-table whose TABLE_ID is prefixed "H$". Two
separate things both need to be handled correctly here:

1. DISCOVER_STORAGE_TABLE_COLUMNS lists the "H$" pseudo-table's OWN columns
   as their own rows, alongside the real table's rows. If those aren't
   excluded from the main per-column list, every real column ends up
   duplicated by a "ghost" row (DATATYPE/COLUMN_ENCODING blank -> shows up
   as "N/A"/"UNKNOWN", DICTIONARY_SIZE 0) that competes with the real row
   for the hierarchy join below. The main column list is therefore
   filtered down to rows whose TABLE_ID belongs to a *real* data table.

2. COLUMN_ID is only unique *within* a TABLE_ID - it is not a stable,
   shared identifier for "this same column" across two different
   TABLE_IDs. The "H$" pseudo-table numbers its own columns 0, 1, 2, ...
   independently of the real table's own COLUMN_ID sequence, so a real
   column's COLUMN_ID and the COLUMN_ID of *its own* hierarchy segment are
   generally different numbers, even though DIMENSION_NAME (the real
   table's name) matches on both sides. Joining hierarchy segments to real
   columns via (DIMENSION_NAME, COLUMN_ID) - as if the two COLUMN_ID
   sequences lined up - silently attaches the wrong size (or the same
   blended size) to every column in the table.

   The only reliable link between an "H$" hierarchy segment and the real
   column it belongs to is by NAME, routed through the "H$" pseudo-table's
   own metadata row:
       H$ metadata row:    (TABLE_ID=H$xxx, COLUMN_ID=k) -> (DIMENSION_NAME, ATTRIBUTE_NAME)
       H$ segment sizes:   (TABLE_ID=H$xxx, COLUMN_ID=k) -> sum(USED_SIZE)
   Joining those two *pseudo-table-scoped* IDs together first gives a
   result keyed by (DIMENSION_NAME, ATTRIBUTE_NAME) - i.e. the real
   (Table, Column) NAME - which real columns can then be matched against
   safely, without ever assuming the two COLUMN_ID sequences correspond.

   COLUMN_ID is marked "for internal use" by Microsoft, so still treat
   Hierarchy Size as a very good estimate rather than an absolute
   guarantee across every engine version.

Note on Cardinality: deliberately not derived from the "H$" pseudo-table
row counts (that requires fragile, version-dependent TABLE_ID string
parsing and an undocumented off-by-N correction). Instead we run one
DAX query per table - EVALUATE ROW("C0", DISTINCTCOUNT(...), "C1", ...) -
covering all of that table's columns in a single round trip, using the
exact same (Table, Column) names already produced from
DISCOVER_STORAGE_TABLE_COLUMNS, so there's no risk of two DMVs disagreeing
on display names and silently failing to match.
"""

from __future__ import annotations

import pandas as pd

from pbi_connection import PbiConnection


ENCODING_MAP = {
    1: "HASH",
    2: "VALUE",
}

_PSEUDO_TABLE_PATTERN = r"^[A-Z]\$"


def _get_storage_tables(conn: PbiConnection) -> pd.DataFrame:
    return conn.query_dmv("SELECT * FROM $SYSTEM.DISCOVER_STORAGE_TABLES")


def _get_storage_columns(conn: PbiConnection) -> pd.DataFrame:
    return conn.query_dmv("SELECT * FROM $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMNS")


def _get_storage_segments(conn: PbiConnection) -> pd.DataFrame:
    return conn.query_dmv("SELECT * FROM $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMN_SEGMENTS")


def _quote_table(name: str) -> str:
    """Escape a table name for use inside 'TableName' quoting in DAX."""
    return str(name).replace("'", "''")


def _quote_column(name: str) -> str:
    """Escape a column name for use inside [ColumnName] bracket syntax in DAX."""
    return str(name).replace("]", "]]")


def _normalize_alias(name: str) -> str:
    """Normalize a result-set column name for matching against our own
    "C0", "C1", ... aliases. Different ADOMD.NET / engine versions have been
    observed to come back with the alias wrapped in brackets (e.g. "[C0]")
    or with incidental whitespace, rather than the plain "C0" you'd expect
    from EVALUATE ROW("C0", ...). Matching only the exact, unwrapped string
    (as a plain `in row.index` check) silently matches *nothing* in that
    case - with no exception raised - which looks identical to "every
    column's cardinality is unavailable" and gives no clue why."""
    return str(name).strip().strip("[]").strip().upper()


def _get_cardinalities(
    conn: PbiConnection, table_column_pairs, warnings_out: list[str] | None = None
) -> dict[tuple[str, str], int]:
    """Run one DAX query per table, returning DISTINCTCOUNT() for every one
    of its columns in a single row, so we don't need N queries for N columns.

    table_column_pairs: an iterable of (table_name, column_name) - these MUST
    be the exact display names already used elsewhere in the output (e.g.
    taken straight from DISCOVER_STORAGE_TABLE_COLUMNS), not re-derived from
    a different metadata DMV, so that the dict keys line up exactly with the
    rest of the report without relying on name-matching across two DMVs.

    warnings_out: if given, human-readable diagnostic strings are appended
    here whenever a column's cardinality couldn't be determined, instead of
    being silently swallowed. Always check this after calling - a fully
    empty/blank Cardinality column with no entries here would be surprising
    and worth re-reading this docstring's mismatch note above.
    """
    if warnings_out is None:
        warnings_out = []

    cardinalities: dict[tuple[str, str], int] = {}

    by_table: dict[str, list[str]] = {}
    for table_name, col_name in table_column_pairs:
        by_table.setdefault(table_name, []).append(col_name)

    for table_name, col_names in by_table.items():
        if not col_names:
            continue

        safe_table = _quote_table(table_name)
        alias_map = {f"C{i}": col_name for i, col_name in enumerate(col_names)}
        measures = [
            f'"{alias}", DISTINCTCOUNT(\'{safe_table}\'[{_quote_column(col_name)}])'
            for alias, col_name in alias_map.items()
        ]

        dax = "EVALUATE ROW(" + ", ".join(measures) + ")"
        batch_error: Exception | None = None
        try:
            result = conn.query_dax(dax)
            if result is not None and not result.empty:
                row0 = result.iloc[0]
                # Tolerant lookup: match aliases even if the engine returned
                # them bracket-wrapped, differently-cased, or padded (see
                # _normalize_alias). Build this once per table.
                normalized_index = {_normalize_alias(c): c for c in row0.index}
                for alias, col_name in alias_map.items():
                    actual_col = normalized_index.get(_normalize_alias(alias))
                    if actual_col is not None:
                        val = row0[actual_col]
                        cardinalities[(table_name, col_name)] = None if pd.isna(val) else int(val)
            else:
                batch_error = RuntimeError("query returned no rows")
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see fallback below
            batch_error = exc

        # Anything not resolved by the batch query above - whether because
        # the whole query raised, or because it silently returned but some
        # (or all) aliases didn't match anything in the result - gets a
        # per-column retry. A *mismatch* is just as much a failure as an
        # exception and must not be ignored the way it previously was.
        missing = [c for c in col_names if (table_name, c) not in cardinalities]
        for col_name in missing:
            single_dax = (
                f'EVALUATE ROW("C", '
                f"DISTINCTCOUNT('{safe_table}'[{_quote_column(col_name)}]))"
            )
            try:
                r = conn.query_dax(single_dax)
                if r is None or r.empty:
                    raise RuntimeError("query returned no rows")
                normalized_index = {_normalize_alias(c): c for c in r.columns}
                actual_col = normalized_index.get(_normalize_alias("C"), r.columns[0])
                val = r.iloc[0][actual_col]
                cardinalities[(table_name, col_name)] = None if pd.isna(val) else int(val)
            except Exception as exc:  # noqa: BLE001
                cardinalities[(table_name, col_name)] = None
                warnings_out.append(
                    f"{table_name}[{col_name}]: cardinality query failed - "
                    f"{type(exc).__name__}: {exc}"
                )

        if batch_error is not None and missing:
            # Surface the batch-level failure too (once per table), even
            # though the per-column retries above already logged their own
            # errors - the batch error is often the more informative one
            # (e.g. it'll show a DAX syntax/name-resolution error that a
            # single-column query for an unrelated column won't reproduce).
            warnings_out.append(
                f"{table_name}: batch cardinality query failed - "
                f"{type(batch_error).__name__}: {batch_error}"
            )

    return cardinalities


def _get_hierarchy_sizes_by_name(storage_columns: pd.DataFrame, segments: pd.DataFrame) -> pd.Series:
    """Return a Series of HierarchySize indexed by (Table, Column) NAME.

    See the module docstring's "Note on Hierarchy Size" for why this has to
    go by name instead of by COLUMN_ID: the "H$" pseudo-table's own COLUMN_ID
    sequence is independent of the real table's COLUMN_ID sequence, so the
    two can only be reconciled through the "H$" pseudo-table's own metadata
    row (which carries the real column's DIMENSION_NAME/ATTRIBUTE_NAME).
    """
    # The "H$" pseudo-table's own column metadata: (TABLE_ID, COLUMN_ID) -> name
    hier_columns = storage_columns[
        storage_columns["TABLE_ID"].astype(str).str.startswith("H$")
    ].copy()
    if hier_columns.empty:
        return pd.Series(
            dtype="int64",
            index=pd.MultiIndex.from_tuples([], names=["DIMENSION_NAME", "ATTRIBUTE_NAME"]),
            name="HierarchySize",
        )

    hier_columns["TABLE_ID"] = hier_columns["TABLE_ID"].astype(str)
    hier_columns["COLUMN_ID"] = hier_columns["COLUMN_ID"].astype(str)

    # The "H$" pseudo-table's own segment sizes: (TABLE_ID, COLUMN_ID) -> bytes
    hier_segments = segments[
        segments["TABLE_ID"].astype(str).str.startswith("H$")
    ].copy()
    hier_segments["TABLE_ID"] = hier_segments["TABLE_ID"].astype(str)
    hier_segments["COLUMN_ID"] = hier_segments["COLUMN_ID"].astype(str)
    hier_seg_size = (
        hier_segments.groupby(["TABLE_ID", "COLUMN_ID"])["USED_SIZE"]
        .sum()
        .rename("HierarchySize")
    )

    # Join on the pseudo-table's OWN (TABLE_ID, COLUMN_ID) - both sides are
    # scoped to the same "H$..." TABLE_ID, so this join is unambiguous -
    # then the result is naturally keyed by the real (DIMENSION_NAME,
    # ATTRIBUTE_NAME), i.e. real (Table, Column) name.
    hier_by_name = hier_columns.merge(
        hier_seg_size, left_on=["TABLE_ID", "COLUMN_ID"], right_index=True, how="inner"
    )

    return (
        hier_by_name.groupby(["DIMENSION_NAME", "ATTRIBUTE_NAME"])["HierarchySize"]
        .sum()
    )


def get_column_metrics(
    conn: PbiConnection,
    include_cardinality: bool = True,
    cardinality_warnings: list[str] | None = None,
) -> pd.DataFrame:
    """Main entry point: returns a tidy DataFrame, one row per column,
    with Data/Dictionary/Hierarchy/Total size in bytes plus cardinality.

    cardinality_warnings: if given (e.g. an empty list you hold onto), it
    gets filled in-place with one diagnostic string per column whose
    cardinality couldn't be determined. Pass this in and check it whenever
    the Cardinality column comes back unexpectedly blank - previously those
    failures were swallowed with zero indication of what went wrong."""

    storage_tables = _get_storage_tables(conn)
    storage_columns = _get_storage_columns(conn)
    segments = _get_storage_segments(conn)

    # Row counts for real data tables (exclude internal $-prefixed pseudo
    # tables like H$..., U$..., R$... which represent hierarchies/relationships)
    real_tables = storage_tables[
        ~storage_tables["TABLE_ID"].astype(str).str.contains(_PSEUDO_TABLE_PATTERN, regex=True)
    ].copy()
    row_counts = real_tables.groupby("DIMENSION_NAME")["ROWS_COUNT"].max().to_dict()
    real_table_ids = set(real_tables["TABLE_ID"].astype(str))

    # Data size: sum USED_SIZE per (TABLE_ID, COLUMN_ID) for segments whose
    # TABLE_ID belongs to a real data table
    data_segments = segments[segments["TABLE_ID"].astype(str).isin(real_table_ids)].copy()
    data_segments["TABLE_ID"] = data_segments["TABLE_ID"].astype(str)
    data_segments["COLUMN_ID"] = data_segments["COLUMN_ID"].astype(str)
    data_size = (
        data_segments.groupby(["TABLE_ID", "COLUMN_ID"])["USED_SIZE"]
        .sum()
        .rename("DataSize")
    )

    # Hierarchy size, resolved to real (Table, Column) NAMES - see
    # _get_hierarchy_sizes_by_name() and the module docstring for why this
    # can't be done via COLUMN_ID matching.
    hier_size = _get_hierarchy_sizes_by_name(storage_columns, segments)

    # Build the base column list with readable names, from
    # DISCOVER_STORAGE_TABLE_COLUMNS - restricted to REAL tables only.
    # DISCOVER_STORAGE_TABLE_COLUMNS also lists the engine's internal
    # "H$"/"U$"/"R$" pseudo-tables' own columns as their own rows; if those
    # aren't excluded here, every real column ends up duplicated by a
    # "ghost" row (DATATYPE/COLUMN_ENCODING blank -> "N/A"/"UNKNOWN",
    # DICTIONARY_SIZE 0) sitting right alongside the real one. This was a
    # real, observed bug: don't reintroduce it by removing this filter.
    base = storage_columns[
        (~storage_columns["ISROWNUMBER"].fillna(False))
        & (storage_columns["TABLE_ID"].astype(str).isin(real_table_ids))
    ].copy()
    base = base.rename(columns={
        "DIMENSION_NAME": "Table",
        "ATTRIBUTE_NAME": "Column",
        "DICTIONARY_SIZE": "DictionarySize",
        "DATATYPE": "DataType",
        "COLUMN_ENCODING": "EncodingCode",
    })

    base["TABLE_ID"] = base["TABLE_ID"].astype(str)
    base["COLUMN_ID"] = base["COLUMN_ID"].astype(str)

    base = base.merge(
        data_size, left_on=["TABLE_ID", "COLUMN_ID"], right_index=True, how="left"
    )
    base = base.merge(
        hier_size, left_on=["Table", "Column"], right_index=True, how="left"
    )

    base["DataSize"] = base["DataSize"].fillna(0).astype("int64")
    base["DictionarySize"] = base["DictionarySize"].fillna(0).astype("int64")
    base["HierarchySize"] = base["HierarchySize"].fillna(0).astype("int64")
    base["TotalSize"] = base["DataSize"] + base["DictionarySize"] + base["HierarchySize"]
    base["Encoding"] = base["EncodingCode"].map(ENCODING_MAP).fillna("UNKNOWN")
    base["Rows"] = base["Table"].map(row_counts)

    cols_out = ["Table", "Column", "DataType", "Encoding", "Rows",
                "DataSize", "DictionarySize", "HierarchySize", "TotalSize"]

    if include_cardinality:
        # Use base's own (Table, Column) names directly - these are exactly
        # what the rest of the report uses, so the dict keys are guaranteed
        # to match (no risk of mismatched display names across DMVs).
        cardinalities = _get_cardinalities(
            conn, zip(base["Table"], base["Column"]), warnings_out=cardinality_warnings
        )
        base["Cardinality"] = [
            cardinalities.get((t, c)) for t, c in zip(base["Table"], base["Column"])
        ]
        cols_out.insert(4, "Cardinality")

    result = base[cols_out].sort_values(["Table", "TotalSize"], ascending=[True, False])
    result = result.reset_index(drop=True)
    return result


def get_table_summary(column_metrics: pd.DataFrame) -> pd.DataFrame:
    """Roll the per-column metrics up to a per-table summary, similar to
    the 'Tables' tab in DAX Studio's VertiPaq Analyzer."""
    agg = {
        "Rows": "max",
        "DataSize": "sum",
        "DictionarySize": "sum",
        "HierarchySize": "sum",
        "TotalSize": "sum",
        "Column": "count",
    }
    summary = column_metrics.groupby("Table").agg(agg).rename(columns={"Column": "ColumnCount"})
    summary["% of DB"] = (summary["TotalSize"] / summary["TotalSize"].sum() * 100).round(2)
    return summary.sort_values("TotalSize", ascending=False).reset_index()


def order_by_table_size_then_field_size(
    column_metrics: pd.DataFrame, table_summary: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Return column_metrics reordered so that:
      1. Tables are grouped together, biggest table (by total TotalSize)
         first.
      2. Within each table, fields are ordered by their own TotalSize,
         largest first.

    This differs from get_column_metrics()'s own default ordering, which
    sorts tables alphabetically and only orders fields by size *within*
    that alphabetical grouping.
    """
    if table_summary is None:
        table_summary = get_table_summary(column_metrics)

    table_order = table_summary.sort_values("TotalSize", ascending=False)["Table"].tolist()

    ordered = column_metrics.copy()
    # A pandas Categorical whose category order is table_order lets us sort
    # by table "rank" (biggest table's rows first) instead of alphabetically,
    # while still sorting each table's own rows by field TotalSize.
    ordered["Table"] = pd.Categorical(ordered["Table"], categories=table_order, ordered=True)
    ordered = ordered.sort_values(["Table", "TotalSize"], ascending=[True, False])
    ordered["Table"] = ordered["Table"].astype(str)
    return ordered.reset_index(drop=True)
