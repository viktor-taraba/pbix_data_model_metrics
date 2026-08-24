"""
pbi_connection.py
------------------
Thin wrapper around Microsoft's ADOMD.NET client so we can run DMV and
DAX queries against the local Analysis Services engine behind an open
Power BI Desktop file, from Python, via pythonnet.

Setup required (one-time, Windows only):
    pip install pythonnet pandas psutil

You also need the ADOMD.NET client assembly. Power BI Desktop already
ships a copy - by default it's under one of:

    C:\\Program Files\\Microsoft Power BI Desktop\\bin\\ADOMD\\Microsoft.AnalysisServices.AdomdClient.dll
    C:\\Program Files\\Microsoft Power BI Desktop\\bin\\Microsoft.AnalysisServices.AdomdClient.dll

If you don't have Power BI Desktop installed at that path (e.g. Store
version, custom install), install the standalone redistributable:
    https://learn.microsoft.com/analysis-services/client-libraries
(search "AMO and ADOMD.NET client libraries", the MSI installs the DLL
into C:\\Program Files\\Microsoft.NET\\ADOMD.NET\\<version>\\)

Then either set the ADOMD_DLL_PATH environment variable to the full
path of the DLL, or pass adomd_dll_path= explicitly to connect().
"""

from __future__ import annotations
import os
import glob
import datetime as _dt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
_ADOMD_LOADED = False


def _default_adomd_search_paths() -> list[str]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates = [
        os.path.join(pf, "Microsoft Power BI Desktop", "bin", "ADOMD",
                     "Microsoft.AnalysisServices.AdomdClient.dll"),
        os.path.join(pf, "Microsoft Power BI Desktop", "bin",
                     "Microsoft.AnalysisServices.AdomdClient.dll"),
    ]
    # Standalone ADOMD.NET client redistributable, versioned folders
    candidates += glob.glob(
        os.path.join(pf, "Microsoft.NET", "ADOMD.NET", "*",
                     "Microsoft.AnalysisServices.AdomdClient.dll")
    )
    candidates += glob.glob(
        os.path.join(pf86, "Microsoft.NET", "ADOMD.NET", "*",
                     "Microsoft.AnalysisServices.AdomdClient.dll")
    )
    return candidates


def _load_adomd(adomd_dll_path: str | None = None):
    global _ADOMD_LOADED
    if _ADOMD_LOADED:
        return

    import clr  # pythonnet, provided by `pip install pythonnet`

    dll_path = adomd_dll_path or os.environ.get("ADOMD_DLL_PATH")
    search_list = [dll_path] if dll_path else []
    search_list += _default_adomd_search_paths()

    for path in search_list:
        if path and os.path.isfile(path):
            clr.AddReference(path)
            _ADOMD_LOADED = True
            return

    raise FileNotFoundError(
        "Could not locate Microsoft.AnalysisServices.AdomdClient.dll.\n"
        "Set the ADOMD_DLL_PATH environment variable to its full path, "
        "or pass adomd_dll_path= to connect().\n"
        "Searched:\n  " + "\n  ".join(search_list or ["(none)"])
    )


def _convert_dmv_value(value):
    """Convert a single raw value from AdomdDataReader.GetValue() into a
    plain Python type, instead of leaving it as whatever CLR object
    pythonnet handed back.

    GetValue() is declared to return `System.Object` (it has to - a DMV
    row can contain columns of any type), so pythonnet doesn't always
    perform its usual automatic marshalling the way it would for a method
    whose return type is concretely typed. In particular:

    - `System.DateTime` values can come back as an opaque CLR object
      rather than a Python `datetime.datetime`. Left as-is, that object
      is invisible to `pandas.to_datetime()`: parsing it raises/fails
      silently under `errors="coerce"` and produces `NaT` for *every*
      row, indistinguishable from the column genuinely being all-NULL.
      This was a real, user-reported bug (last-data-refresh detection
      reported "every value null/unparseable" across three independent
      DMVs simultaneously - a strong signal it was a marshalling issue,
      not the data actually being empty everywhere).
    - `System.DBNull.Value` (a real SQL NULL) is not the same object as
      Python's `None` and won't compare equal to it or register as NaN,
      so it needs converting explicitly too, or it silently poisons any
      downstream `pd.isna()`/`dropna()` logic the same way.

    Every other value (str, int, float, bool, already-a-datetime, ...)
    is returned unchanged - this function is a no-op for the common
    case, and only special-cases the two CLR types known to cause
    problems.
    """
    if value is None:
        return None

    type_name = type(value).__name__

    if type_name == "DBNull":
        return None

    if type_name == "DateTime":
        # Build the Python datetime from the CLR DateTime's own numeric
        # components rather than str(value), since str() on a CLR
        # DateTime is culture/format-dependent and not safe to re-parse.
        try:
            return _dt.datetime(
                value.Year, value.Month, value.Day,
                value.Hour, value.Minute, value.Second,
                value.Millisecond * 1000,
            )
        except Exception:
            # If the CLR object doesn't actually expose these members
            # (unexpected type, future engine change, etc.), fall back to
            # returning it unchanged rather than raising - the caller's
            # own null/parse-failure handling will surface it.
            return value

    return value


class PbiConnection:
    """A connection to a local Power BI Desktop tabular model."""

    def __init__(self, port: int, database: str | None = None,
                 adomd_dll_path: str | None = None):
        _load_adomd(adomd_dll_path)
        from Microsoft.AnalysisServices.AdomdClient import AdomdConnection  # type: ignore

        self.port = port
        conn_str = f"Data Source=localhost:{port}"
        if database:
            conn_str += f";Initial Catalog={database}"

        self._conn = AdomdConnection(conn_str)
        self._conn.Open()

        if not database:
            self.database = self._first_catalog_name()
            self._conn.Close()
            self._conn = AdomdConnection(conn_str + f";Initial Catalog={self.database}")
            self._conn.Open()
        else:
            self.database = database

    def _first_catalog_name(self) -> str:
        df = self.query_dmv("SELECT * FROM $SYSTEM.DBSCHEMA_CATALOGS")
        if df.empty:
            raise RuntimeError("No catalogs/databases found on this instance.")
        return df.iloc[0]["CATALOG_NAME"]

    def close(self):
        try:
            self._conn.Close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- query helpers --------------------------------------------------

    def _execute_reader(self, command_text: str):
        from Microsoft.AnalysisServices.AdomdClient import AdomdCommand  # type: ignore
        cmd = AdomdCommand(command_text, self._conn)
        return cmd.ExecuteReader()

    def query_dmv(self, dmv_sql: str) -> pd.DataFrame:
        """Run a DMV query, e.g. SELECT * FROM $SYSTEM.DISCOVER_STORAGE_TABLE_COLUMNS"""
        reader = self._execute_reader(dmv_sql)
        cols = [reader.GetName(i) for i in range(reader.FieldCount)]
        rows = []
        while reader.Read():
            rows.append([_convert_dmv_value(reader.GetValue(i)) for i in range(reader.FieldCount)])
        reader.Close()
        return pd.DataFrame(rows, columns=cols)

    def query_dax(self, dax_query: str) -> pd.DataFrame:
        """Run a DAX EVALUATE query and return the single result table."""
        return self.query_dmv(dax_query)  # AdomdCommand handles both the same way
