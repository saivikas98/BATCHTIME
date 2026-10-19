"""PDF extraction logic for the EB Execution Date app."""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pypdf import PdfReader

DT_FORMAT = "%d/%m/%Y %H:%M:%S"
DT_PATTERN = r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}"
BATCH_PATTERN = r"[A-Z]{2}\d{5}"

SYSTEM = "System Integrity Report"
BATCH = "Batch Cycle Report"
PFS = "PFS Filling Report"
UNKNOWN = "Unknown Report"

COLUMNS = [
    "Stage", "Machine", "Process Start", "Process End",
    "Duration (hours)", "Index", "Total", "Source", "Validation",
]


def _bytes(source: Any) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()
    if hasattr(source, "getvalue"):
        return source.getvalue()
    if hasattr(source, "read"):
        position = source.tell() if hasattr(source, "tell") else None
        data = source.read()
        if position is not None and hasattr(source, "seek"):
            source.seek(position)
        return data
    raise TypeError("Unsupported PDF input.")


def _text(source: Any, pages: int = 8) -> str:
    reader = PdfReader(io.BytesIO(_bytes(source)))
    return "\n".join(
        (reader.pages[index].extract_text() or "")
        for index in range(min(pages, len(reader.pages)))
    )


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _batch_number(text: str, file_name: str) -> str:
    labelled = re.search(
        rf"Batch\s*(?:no\.?|number)?\s*[:.]?\s*({BATCH_PATTERN})",
        text,
        re.IGNORECASE,
    )
    fallback = re.search(BATCH_PATTERN, file_name, re.IGNORECASE)
    generic = re.search(BATCH_PATTERN, text, re.IGNORECASE)
    match = labelled or fallback or generic
    return match.group(1 if labelled else 0).upper() if match else "UNKNOWN"


def _row(
    stage: str,
    machine: str,
    start: str,
    end: str,
    source: str,
    index: int | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    start_dt = datetime.strptime(start, DT_FORMAT)
    end_dt = datetime.strptime(end, DT_FORMAT)
    return {
        "Stage": stage,
        "Machine": machine,
        "Process Start": start_dt,
        "Process End": end_dt,
        "Duration (hours)": round((end_dt - start_dt).total_seconds() / 3600, 4),
        "Index": index,
        "Total": total,
        "Source": source,
        "Validation": "OK" if end_dt >= start_dt else "End before start",
    }


def detect_report_type(source: Any) -> str:
    probe = _text(source, 8).upper()
    if "SYSTEM INTEGRITY REPORT" in probe:
        return SYSTEM
    if "BATCH START TIME" in probe and (
        "PRODUCTION COUNTERS" in probe or "EQUIPMENT ID" in probe
    ):
        return PFS
    if "PROCESS OVERVIEW" in probe and any(
        value in probe for value in ("WETTING", "PREMIXING", "MIXING EXTRUDER")
    ):
        return BATCH
    return UNKNOWN


def parse_system_integrity(source: Any, file_name: str) -> dict[str, Any]:
    text = _text(source, 2)
    normal = _clean(text)
    pattern = re.compile(
        rf"(Dryrun|Leak\s*Test|Vacuum\s*Test)\s+"
        rf"(Mixing\s*Extruder\s*[123])\s+"
        rf"({DT_PATTERN})\s+({DT_PATTERN})\s+(\d+)\s+(\d+)",
        re.IGNORECASE,
    )
    labels = {
        "dryrun": "Seal dry run",
        "leak test": "Leak test",
        "vacuum test": "Vacuum test (Max)",
    }
    rows = []
    for match in pattern.finditer(normal):
        process = _clean(match.group(1)).lower()
        rows.append(
            _row(
                labels[process],
                _clean(match.group(2)).title(),
                match.group(3),
                match.group(4),
                "System Integrity Process Overview",
                int(match.group(5)),
                int(match.group(6)),
            )
        )
    return {
        "Batch": _batch_number(text, file_name),
        "Report Type": SYSTEM,
        "Rows": pd.DataFrame(rows, columns=COLUMNS),
    }


def parse_batch_cycle(source: Any, file_name: str) -> dict[str, Any]:
    text = _text(source, 4)
    normal = _clean(text)
    pattern = re.compile(
        rf"(API\s*Charge|Water\s*Charge|Wetting|Premixing|Mixing|Blending)\s+"
        rf"(API\s*Charge|Water\s*Charge|Mixing\s*Extruder\s*[123])\s+"
        rf"({DT_PATTERN})\s+({DT_PATTERN})\s+(\d+)\s+(\d+)",
        re.IGNORECASE,
    )
    labels = {
        "api charge": "API charging", "water charge": "Water charging",
        "wetting": "Wetting", "premixing": "Pre-mixing",
        "mixing": "Mixing", "blending": "Blending",
    }
    raw_rows = []
    for match in pattern.finditer(normal):
        process = _clean(match.group(1)).lower()
        raw_rows.append(
            _row(
                labels[process], _clean(match.group(2)).title(),
                match.group(3), match.group(4), "Batch Process Overview",
                int(match.group(5)), int(match.group(6)),
            )
        )

    rows = []
    for stage in ("Water charging", "API charging"):
        matching = [item for item in raw_rows if item["Stage"] == stage]
        if matching:
            start = min(item["Process Start"] for item in matching)
            end = max(item["Process End"] for item in matching)
            rows.append({
                "Stage": stage, "Machine": stage,
                "Process Start": start, "Process End": end,
                "Duration (hours)": round((end - start).total_seconds() / 3600, 4),
                "Index": None, "Total": len(matching),
                "Source": "Minimum start to maximum end of Process Overview",
                "Validation": "OK",
            })
    rows.extend(
        item for item in raw_rows
        if item["Stage"] not in {"Water charging", "API charging"}
    )
    return {
        "Batch": _batch_number(text, file_name),
        "Report Type": BATCH,
        "Rows": pd.DataFrame(rows, columns=COLUMNS),
    }


def parse_pfs(source: Any, file_name: str) -> dict[str, Any]:
    text = _text(source, 8)
    start = re.search(rf"Batch Start Time\s*:\s*({DT_PATTERN})", text, re.I)
    end = re.search(rf"Batch End Time\s*:\s*({DT_PATTERN})", text, re.I)
    rows = []
    if start and end:
        rows.append(_row("Filling", "PFS Filling Line", start.group(1), end.group(1), "PFS batch header"))
    return {
        "Batch": _batch_number(text, file_name),
        "Report Type": PFS,
        "Rows": pd.DataFrame(rows, columns=COLUMNS),
    }


def parse_pdf(source: Any, file_name: str = "") -> dict[str, Any]:
    report_type = detect_report_type(source)
    if report_type == SYSTEM:
        return parse_system_integrity(source, file_name)
    if report_type == BATCH:
        return parse_batch_cycle(source, file_name)
    if report_type == PFS:
        return parse_pfs(source, file_name)
    return {
        "Batch": _batch_number("", file_name),
        "Report Type": UNKNOWN,
        "Rows": pd.DataFrame(columns=COLUMNS),
        "Error": "Unsupported report layout.",
    }


def combine_reports(reports: list[dict[str, Any]]) -> pd.DataFrame:
    combined = []
    for batch in sorted({report["Batch"] for report in reports}):
        group = [report for report in reports if report["Batch"] == batch]
        tables = [report["Rows"] for report in group if not report["Rows"].empty]
        if not tables:
            continue
        table = pd.concat(tables, ignore_index=True)
        cycles = [r for r in group if r["Report Type"] == BATCH and not r["Rows"].empty]
        filling = [r for r in group if r["Report Type"] == PFS and not r["Rows"].empty]
        if cycles and filling:
            start = cycles[0]["Rows"]["Process End"].max()
            end = filling[0]["Rows"]["Process Start"].min()
            if pd.notna(start) and pd.notna(end) and end >= start:
                setup = {
                    "Stage": "Filling setup", "Machine": "Inter-report gap",
                    "Process Start": start, "Process End": end,
                    "Duration (hours)": round((end - start).total_seconds() / 3600, 4),
                    "Index": None, "Total": None,
                    "Source": "Batch-cycle end to PFS filling start", "Validation": "OK",
                }
                table = pd.concat([table, pd.DataFrame([setup])], ignore_index=True)
        table.insert(0, "Batch", batch)
        combined.append(table)
    if not combined:
        return pd.DataFrame(columns=["Batch", *COLUMNS])
    return pd.concat(combined, ignore_index=True).sort_values(
        ["Batch", "Process Start", "Stage", "Machine"]
    ).reset_index(drop=True)
