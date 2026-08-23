r"""
pbi_discover.py
----------------
Finds the local Analysis Services engine that Power BI Desktop starts
in the background for each open .pbix file, and figures out which
TCP port and catalog (database) name to connect to.

How it works
------------
Every time you open a .pbix in Power BI Desktop, Desktop launches a
private copy of msmdsrv.exe (the SSAS/VertiPaq engine) with a
"-s <workspace folder>" argument pointing at a per-file workspace
directory under:

    %LOCALAPPDATA%\Microsoft\Power BI Desktop\AnalysisServicesWorkspaces\

Once that engine starts, it writes the TCP port it bound to into:

    <workspace folder>\Data\msmdsrv.port.txt   (UTF-16 text file, just a number)

This module enumerates running msmdsrv.exe processes, reads that
port file for each one, and (optionally) asks the engine itself for
the catalog/database name so you don't have to guess it.

It also does a best-effort lookup of the actual .pbix file each
instance belongs to (name, full path, size on disk) - see
_pbix_file_info_for_parent() - by inspecting which file handles the
parent PBIDesktop.exe process has open, since Power BI Desktop keeps
the .pbix file itself open (locked) while it's being edited.

Requires: psutil
"""

from __future__ import annotations

import os
import re
import glob
from dataclasses import dataclass
from typing import Optional

import psutil


@dataclass
class PbiInstance:
    pid: int
    workspace_dir: str
    port: Optional[int]
    pbix_title: Optional[str] = None  # best-effort, from the parent PBIDesktop.exe window/process
    pbix_path: Optional[str] = None  # full path to the .pbix file, if it could be determined
    pbix_name: Optional[str] = None  # just the filename, e.g. "MyReport.pbix"
    pbix_size_bytes: Optional[int] = None  # size on disk of the .pbix file, if known

    def __str__(self) -> str:
        title = f" ({self.pbix_title})" if self.pbix_title else ""
        return f"PID {self.pid}{title} -> localhost:{self.port}  [{self.workspace_dir}]"


def _extract_workspace_dir(cmdline: list[str]) -> Optional[str]:
    """msmdsrv.exe is launched like: msmdsrv.exe -s "<workspace>\\Data" """
    for i, tok in enumerate(cmdline):
        if tok == "-s" and i + 1 < len(cmdline):
            return cmdline[i + 1]
    # Some versions pass it as a single "-s<path>" token
    for tok in cmdline:
        m = re.match(r"^-s(.+)$", tok)
        if m:
            return m.group(1)
    return None


def _read_port_file(data_dir: str) -> Optional[int]:
    port_file = os.path.join(data_dir, "msmdsrv.port.txt")
    if not os.path.isfile(port_file):
        return None
    try:
        # The file is UTF-16 LE with a BOM
        with open(port_file, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-16-le", errors="ignore").strip("\ufeff\x00\r\n ")
        return int(re.sub(r"[^\d]", "", text))
    except Exception:
        return None


def _guess_title_for_parent(pid: int) -> Optional[str]:
    """Best-effort: find the parent PBIDesktop.exe process and try to get
    something identifying which file it has open. Windows doesn't expose
    the open filename directly via psutil, so this only returns the exe
    path / pid as a fallback label."""
    try:
        proc = psutil.Process(pid)
        parent = proc.parent()
        if parent and "pbidesktop" in parent.name().lower():
            return f"PBIDesktop.exe pid {parent.pid}"
    except Exception:
        pass
    return None


def _pbix_file_info_for_parent(pid: int) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Best-effort: find the parent PBIDesktop.exe process and inspect its
    open file handles for the actual .pbix file it's editing, via
    psutil.Process.open_files(). Power BI Desktop keeps the .pbix file
    itself open (locked) for the whole editing session, so this is a
    reliable way to get the real file - name, full path, and size on
    disk - without needing the user to tell us, unlike the msmdsrv.exe
    workspace folder (which is an internal, randomly-named copy that
    reveals nothing about the original file).

    Returns (name, full_path, size_bytes), any/all of which may be None:
    - A brand-new, never-saved report ("Untitled.pbix") has no file on
      disk at all, so there's nothing to find here.
    - `open_files()` itself can raise psutil.AccessDenied on some Windows
      configurations even for the current user's own processes, since it
      relies on an OS-level handle enumeration API that isn't always
      unrestricted - that's treated as "couldn't determine it", not an
      error worth surfacing to the end user.
    """
    try:
        proc = psutil.Process(pid)
        parent = proc.parent()
        if not parent or "pbidesktop" not in parent.name().lower():
            return None, None, None

        for f in parent.open_files():
            if f.path.lower().endswith(".pbix"):
                name = os.path.basename(f.path)
                try:
                    size = os.path.getsize(f.path)
                except OSError:
                    size = None
                return name, f.path, size
    except Exception:
        pass
    return None, None, None


def find_running_instances() -> list[PbiInstance]:
    """Scan all running processes for msmdsrv.exe instances that look like
    they were launched by Power BI Desktop, and resolve their port."""
    results: list[PbiInstance] = []

    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name != "msmdsrv.exe":
                continue
            cmdline = proc.info.get("cmdline") or []
            workspace_dir = _extract_workspace_dir(cmdline)
            if not workspace_dir:
                continue
            # Only care about Power BI Desktop's private workspaces, not a
            # full standalone SSAS/AAS install.
            if "analysisserviceworkspace" not in workspace_dir.lower() and \
               "analysisserviceswork" not in workspace_dir.lower():
                # Still include it - some locales/paths differ - just don't filter hard.
                pass

            port = _read_port_file(workspace_dir)
            title = _guess_title_for_parent(proc.info["pid"])
            pbix_name, pbix_path, pbix_size = _pbix_file_info_for_parent(proc.info["pid"])
            results.append(PbiInstance(
                pid=proc.info["pid"],
                workspace_dir=workspace_dir,
                port=port,
                pbix_title=pbix_name or title,
                pbix_path=pbix_path,
                pbix_name=pbix_name,
                pbix_size_bytes=pbix_size,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return results


def find_by_scanning_appdata() -> list[PbiInstance]:
    """Fallback discovery method: directly scan Power BI's workspace folder
    under %LOCALAPPDATA% for any msmdsrv.port.txt files, in case the
    process cmdline couldn't be read (e.g. due to permissions)."""
    base = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "Power BI Desktop", "AnalysisServicesWorkspaces",
    )
    results = []
    for port_file in glob.glob(os.path.join(base, "*", "Data", "msmdsrv.port.txt")):
        data_dir = os.path.dirname(port_file)
        port = _read_port_file(data_dir)
        if port:
            results.append(PbiInstance(pid=-1, workspace_dir=data_dir, port=port))
    return results


def find_all() -> list[PbiInstance]:
    found = find_running_instances()
    if not found:
        found = find_by_scanning_appdata()
    return found


if __name__ == "__main__":
    instances = find_all()
    if not instances:
        print("No running Power BI Desktop local model instances found.")
        print("Make sure Power BI Desktop is open with a .pbix file loaded.")
    else:
        print(f"Found {len(instances)} instance(s):\n")
        for inst in instances:
            print(" -", inst)
