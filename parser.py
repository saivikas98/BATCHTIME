"""PDF parsers for the EB Execution Date Streamlit application.

Supported report types:
1. System Integrity Report
2. Batch Cycle Report
3. PFS Filling Report

The parser extracts only execution-date information required by the reference
workbook. It does not store uploaded PDFs or invent missing timestamps.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import fitz  # PyMuPDF
import pandas as pd


DATE_TIME_FORMAT = "%d/%m/%Y %H:%M:%S"
DATE_TIME_PATTERN = r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}"
BATCH_PATTERN = r"[A-Z]{2}\d{5}"

REPORT_SYSTEM_INTEGRITY = "System Integrity Report"
REPORT_BATCH_CYCLE = "Batch Cycle Report"
REPORT_PFS_FILLING = "PFS Filling Report"
REPORT_UNKNOWN = "Unknown Report"

OUTPUT_COLUMNS = [
    "Stage",
    "Machine",
    "Process Start",
    "Process End",
    "Duration (hours)",
    "Index",
    "Total",
    "Source",
    "Validation",
]


def _empty_rows() -> pd.DataFrame:
    """Return an empty result table with a stable schema."""
    return pd.DataFrame(columns=OUTPUT_COLUMNS)


def _to_datetime(value: str) -> datetime:
    """Convert report date text to a Python datetime."""
    return datetime.strptime(value.strip(), DATE_TIME_FORMAT)


def _normalise_space(value: str) -> str:
    """Replace line breaks and repeated spaces with a single space."""
    return re.sub(r"\s+", " ", value).strip()


def _read_pdf_bytes(source: Any) -> bytes:
    """Read PDF bytes from Streamlit uploads, file paths, or file-like objects."""
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()

    if isinstance(source, bytes):
        return source

    if hasattr(source, "getvalue"):
        return source.getvalue()

    if hasattr(source, "read"):
        current_position = None
        if hasattr(source, "tell"):
            current_position = source.tell()
        data = source.read()
        if current_position is not None and hasattr(source, "seek"):
            source.seek(current_position)
        return data

    raise TypeError("Unsupported PDF input type.")


def _open_pdf(source: Any) -> fitz.Document:
    """Open a PDF from memory without writing uploaded content to disk."""
    return fitz.open(stream=_read_pdf_bytes(source), filetype="pdf")


def _extract_pages_text(document: fitz.Document, page_count: int) -> str:
    """Extract sorted text from the first requested pages."""
    text_parts: list[str] = []
    for page_number in range(min(page_count, len(document))):
        text_parts.append(document.load_page(page_number).get_text("text", sort=True))
    return "\n".join(text_parts)


def _extract_batch_number(text: str, file_name: str = "") -> str:
    """Extract the batch number from report text, then fall back to the name."""
    labelled_match = re.search(
        rf"Batch\s*(?:no\.?|number)?\s*[:.]?\s*({BATCH_PATTERN})",
        text,
        flags=re.IGNORECASE,
    )
    if labelled_match:
        return labelled_match.group(1).upper()

    file_name_match = re.search(BATCH_PATTERN, file_name, flags=re.IGNORECASE)
    if file_name_match:
        return file_name_match.group(0).upper()

    generic_match = re.search(BATCH_PATTERN, text, flags=re.IGNORECASE)
    if generic_match:
        return generic_match.group(0).upper()

    return "UNKNOWN"


def _build_row(
    stage: str,
    machine: str,
    start_text: str,
    end_text: str,
    source: str,
    execution_index: int | None = None,
    execution_total: int | None = None,
) -> dict[str, Any]:
    """Create one validated execution record."""
    start_time = _to_datetime(start_text)
    end_time = _to_datetime(end_text)
    duration_hours = (end_time - start_time).total_seconds() / 3600

    return {
        "Stage": stage,
        "Machine": machine,
        "Process Start": start_time,
        "Process End": end_time,
        "Duration (hours)": round(duration_hours, 4),
        "Index": execution_index,
        "Total": execution_total,
        "Source": source,
        "Validation": "OK" if end_time >= start_time else "End before start",
    }


def detect_report_type(source: Any) -> str:
    """Identify the uploaded report using text from its first pages."""
    with _open_pdf(source) as document:
        probe_text = _extract_pages_text(document, 8).upper()

    if "SYSTEM INTEGRITY REPORT" in probe_text:
        return REPORT_SYSTEM_INTEGRITY

    if "BATCH START TIME" in probe_text and (
        "PRODUCTION COUNTERS" in probe_text or "EQUIPMENT ID" in probe_text
    ):
        return REPORT_PFS_FILLING

    if "PROCESS OVERVIEW" in probe_text and any(
        marker in probe_text
        for marker in ("WETTING", "PREMIXING", "MIXING EXTRUDER")
    ):
        return REPORT_BATCH_CYCLE

    return REPORT_UNKNOWN


def parse_system_integrity_report(
    source: Any,
    file_name: str = "",
) -> dict[str, Any]:
    """Extract Dryrun, Leak Test, and Vacuum Test Process Overview rows."""
    with _open_pdf(source) as document:
        overview_text = _extract_pages_text(document, 2)

    batch_number = _extract_batch_number(overview_text, file_name)
    normalised_text = _normalise_space(overview_text)

    process_pattern = re.compile(
        rf"(Dryrun|Leak\s*Test|Vacuum\s*Test)\s+"
        rf"(Mixing\s*Extruder\s*[123])\s+"
        rf"({DATE_TIME_PATTERN})\s+"
        rf"({DATE_TIME_PATTERN})\s+"
        rf"(\d+)\s+(\d+)",
        flags=re.IGNORECASE,
    )

    stage_names = {
        "dryrun": "Seal dry run",
        "leak test": "Leak test",
        "vacuum test": "Vacuum test (Max)",
    }

    rows: list[dict[str, Any]] = []
    for match in process_pattern.finditer(normalised_text):
        raw_process = _normalise_space(match.group(1)).lower()
        machine = _normalise_space(match.group(2)).title()

        rows.append(
            _build_row(
                stage=stage_names[raw_process],
                machine=machine,
                start_text=match.group(3),
                end_text=match.group(4),
                source="System Integrity Process Overview",
                execution_index=int(match.group(5)),
                execution_total=int(match.group(6)),
            )
        )

    result_rows = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    return {
        "Batch": batch_number,
        "Report Type": REPORT_SYSTEM_INTEGRITY,
        "Rows": result_rows,
    }


def parse_pfs_filling_report(
    source: Any,
    file_name: str = "",
) -> dict[str, Any]:
    """Extract the filling interval and available production counters."""
    with _open_pdf(source) as document:
        header_text = _extract_pages_text(document, 8)

    batch_number = _extract_batch_number(header_text, file_name)

    start_match = re.search(
        rf"Batch Start Time\s*:\s*({DATE_TIME_PATTERN})",
        header_text,
        flags=re.IGNORECASE,
    )
    end_match = re.search(
        rf"Batch End Time\s*:\s*({DATE_TIME_PATTERN})",
        header_text,
        flags=re.IGNORECASE,
    )

    rows: list[dict[str, Any]] = []
    if start_match and end_match:
        rows.append(
            _build_row(
                stage="Filling",
                machine="PFS Filling Line",
                start_text=start_match.group(1),
                end_text=end_match.group(1),
                source="PFS batch header",
            )
        )

    counters: dict[str, int] = {}
    counter_match = re.search(
        r"Production Counters:\s*"
        r"Infeed\s*\(pcs\)\s+Outfeed\s*\(pcs\)\s+Reject\s*\(pcs\)\s*"
        r"(\d+)\s+(\d+)\s+(\d+)",
        _normalise_space(header_text),
        flags=re.IGNORECASE,
    )
    if counter_match:
        counters = {
            "Infeed": int(counter_match.group(1)),
            "Outfeed": int(counter_match.group(2)),
            "Reject": int(counter_match.group(3)),
        }

    return {
        "Batch": batch_number,
        "Report Type": REPORT_PFS_FILLING,
        "Rows": pd.DataFrame(rows, columns=OUTPUT_COLUMNS),
        "Counters": counters,
    }


def parse_batch_cycle_report(
    source: Any,
    file_name: str = "",
) -> dict[str, Any]:
    """Extract execution intervals from the Batch Cycle Process Overview."""
    with _open_pdf(source) as document:
        overview_text = _extract_pages_text(document, 4)

    batch_number = _extract_batch_number(overview_text, file_name)
    normalised_text = _normalise_space(overview_text)

    process_pattern = re.compile(
        rf"(API\s*Charge|Water\s*Charge|Wetting|Premixing|Mixing|Blending)\s+"
        rf"(API\s*Charge|Water\s*Charge|Mixing\s*Extruder\s*[123])\s+"
        rf"({DATE_TIME_PATTERN})\s+"
        rf"({DATE_TIME_PATTERN})\s+"
        rf"(\d+)\s+(\d+)",
        flags=re.IGNORECASE,
    )

    stage_names = {
        "api charge": "API charging",
        "water charge": "Water charging",
        "wetting": "Wetting",
        "premixing": "Pre-mixing",
        "mixing": "Mixing",
        "blending": "Blending",
    }

    extracted_rows: list[dict[str, Any]] = []
    for match in process_pattern.finditer(normalised_text):
        raw_process = _normalise_space(match.group(1)).lower()
        machine = _normalise_space(match.group(2)).title()

        extracted_rows.append(
            _build_row(
                stage=stage_names[raw_process],
                machine=machine,
                start_text=match.group(3),
                end_text=match.group(4),
                source="Batch Process Overview",
                execution_index=int(match.group(5)),
                execution_total=int(match.group(6)),
            )
        )

    rows: list[dict[str, Any]] = []

    # The reference workbook represents charging stages as elapsed windows,
    # from the earliest matching cycle start to the latest matching cycle end.
    for charging_stage in ("Water charging", "API charging"):
        matching_rows = [
            row for row in extracted_rows if row["Stage"] == charging_stage
        ]
        if matching_rows:
            start_time = min(row["Process Start"] for row in matching_rows)
            end_time = max(row["Process End"] for row in matching_rows)
            rows.append(
                {
                    "Stage": charging_stage,
                    "Machine": charging_stage,
                    "Process Start": start_time,
                    "Process End": end_time,
                    "Duration (hours)": round(
                        (end_time - start_time).total_seconds() / 3600,
                        4,
                    ),
                    "Index": None,
                    "Total": len(matching_rows),
                    "Source": "Minimum start to maximum end of Process Overview",
                    "Validation": "OK",
                }
            )

    rows.extend(
        row
        for row in extracted_rows
        if row["Stage"] not in {"Water charging", "API charging"}
    )

    return {
        "Batch": batch_number,
        "Report Type": REPORT_BATCH_CYCLE,
        "Rows": pd.DataFrame(rows, columns=OUTPUT_COLUMNS),
    }


def parse_pdf(source: Any, file_name: str = "") -> dict[str, Any]:
    """Detect the report type and route it to the correct parser."""
    report_type = detect_report_type(source)

    if report_type == REPORT_SYSTEM_INTEGRITY:
        return parse_system_integrity_report(source, file_name)

    if report_type == REPORT_BATCH_CYCLE:
        return parse_batch_cycle_report(source, file_name)

    if report_type == REPORT_PFS_FILLING:
        return parse_pfs_filling_report(source, file_name)

    return {
        "Batch": _extract_batch_number("", file_name),
        "Report Type": REPORT_UNKNOWN,
        "Rows": _empty_rows(),
        "Error": "Unsupported report layout.",
    }


def combine_reports(reports: list[dict[str, Any]]) -> pd.DataFrame:
    """Combine extracted records and calculate the Filling setup interval."""
    combined_batches: list[pd.DataFrame] = []
    batch_numbers = sorted({report["Batch"] for report in reports})

    for batch_number in batch_numbers:
        batch_reports = [
            report for report in reports if report["Batch"] == batch_number
        ]
        available_tables = [
            report["Rows"]
            for report in batch_reports
            if not report["Rows"].empty
        ]

        if not available_tables:
            continue

        batch_table = pd.concat(available_tables, ignore_index=True)

        cycle_reports = [
            report
            for report in batch_reports
            if report["Report Type"] == REPORT_BATCH_CYCLE
            and not report["Rows"].empty
        ]
        filling_reports = [
            report
            for report in batch_reports
            if report["Report Type"] == REPORT_PFS_FILLING
            and not report["Rows"].empty
        ]

        if cycle_reports and filling_reports:
            cycle_end = cycle_reports[0]["Rows"]["Process End"].max()
            filling_start = filling_reports[0]["Rows"]["Process Start"].min()

            if pd.notna(cycle_end) and pd.notna(filling_start):
                if filling_start >= cycle_end:
                    setup_row = {
                        "Stage": "Filling setup",
                        "Machine": "Inter-report gap",
                        "Process Start": cycle_end,
                        "Process End": filling_start,
                        "Duration (hours)": round(
                            (filling_start - cycle_end).total_seconds() / 3600,
                            4,
                        ),
                        "Index": None,
                        "Total": None,
                        "Source": "Batch-cycle end to PFS filling start",
                        "Validation": "OK",
                    }
                    batch_table = pd.concat(
                        [batch_table, pd.DataFrame([setup_row])],
                        ignore_index=True,
                    )

        batch_table.insert(0, "Batch", batch_number)
        combined_batches.append(batch_table)

    if not combined_batches:
        return pd.DataFrame(columns=["Batch", *OUTPUT_COLUMNS])

    return (
        pd.concat(combined_batches, ignore_index=True)
        .sort_values(
            by=["Batch", "Process Start", "Stage", "Machine"],
            na_position="last",
        )
        .reset_index(drop=True)
    )
