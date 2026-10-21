"""PDF parser for the EB Execution Date Streamlit app.

This version validates blending completion from the last actual
"Blending to accumulator" cycle before "Close 2mm Orifice". It accepts the
controller behaviour where the close row resets its cycle value to 1.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pypdf import PdfReader

# PyMuPDF changed its preferred import name from ``fitz`` to ``pymupdf``.
# Keep both paths and do not crash the Streamlit app if the optional fast
# backend is temporarily unavailable during deployment.
try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None  # type: ignore

DT_FORMAT = "%d/%m/%Y %H:%M:%S"
DT_PATTERN = r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}"
BATCH_PATTERN = r"[A-Z]{2}\d{5}"

SYSTEM = "System Integrity Report"
BATCH = "Batch Cycle Report"
PFS = "PFS Filling Report"
UNKNOWN = "Unknown Report"
PARSER_VERSION = "2026.10.21-multireport-v3"

COLUMNS = [
    "Stage", "Machine", "Process Start", "Process End",
    "Duration (hours)", "Index", "Total", "Source", "Validation",
]


def _read_bytes(source: Any) -> bytes:
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
    raise TypeError("Unsupported PDF input type.")


def _reader(source: Any) -> PdfReader:
    return PdfReader(io.BytesIO(_read_bytes(source)))


def _page_text(reader: PdfReader, index: int) -> str:
    return reader.pages[index].extract_text() or ""


def _first_pages_text(reader: PdfReader, count: int) -> str:
    return "\n".join(
        _page_text(reader, index)
        for index in range(min(count, len(reader.pages)))
    )


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _date(value: str) -> datetime:
    return datetime.strptime(value.strip(), DT_FORMAT)


def _batch_number(text: str, file_name: str = "") -> str:
    labelled = re.search(
        rf"Batch\s*(?:no\.?|number)?\s*[:.]?\s*({BATCH_PATTERN})",
        text,
        re.IGNORECASE,
    )
    if labelled:
        return labelled.group(1).upper()
    for candidate in (file_name, text):
        match = re.search(BATCH_PATTERN, candidate, re.IGNORECASE)
        if match:
            return match.group(0).upper()
    return "UNKNOWN"


def _row(
    stage: str,
    machine: str,
    start: datetime | str,
    end: datetime | str,
    source: str,
    index: int | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    start_dt = _date(start) if isinstance(start, str) else start
    end_dt = _date(end) if isinstance(end, str) else end
    duration = (end_dt - start_dt).total_seconds() / 3600
    return {
        "Stage": stage,
        "Machine": machine,
        "Process Start": start_dt,
        "Process End": end_dt,
        "Duration (hours)": round(duration, 4),
        "Index": index,
        "Total": total,
        "Source": source,
        "Validation": "OK" if duration >= 0 else "End before start",
    }


def detect_report_type(source: Any) -> str:
    reader = _reader(source)
    probe = _first_pages_text(reader, 8).upper()
    if "SYSTEM INTEGRITY REPORT" in probe:
        return SYSTEM
    if "BATCH START TIME" in probe and (
        "PRODUCTION COUNTERS" in probe or "EQUIPMENT ID" in probe
    ):
        return PFS
    # Full reports normally contain Process Overview. Split continuation
    # reports may begin directly at BLENDING DATALOG / Adjust PH and therefore
    # must still be classified as Batch Cycle reports.
    if (
        "BATCH CYCLE REPORT" in probe
        and (
            "PROCESS OVERVIEW" in probe
            or "BLENDING DATALOG" in probe
            or "ADJUST PH" in probe
            or "SAMPLE: IPS PH" in probe
        )
    ):
        return BATCH
    return UNKNOWN


def parse_system_integrity(source: Any, file_name: str = "") -> dict[str, Any]:
    reader = _reader(source)
    text = _first_pages_text(reader, 2)
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
    for match in pattern.finditer(_clean(text)):
        process = _clean(match.group(1)).casefold()
        rows.append(
            _row(
                labels[process], _clean(match.group(2)).title(),
                match.group(3), match.group(4),
                "System Integrity Process Overview",
                int(match.group(5)), int(match.group(6)),
            )
        )
    return {
        "Batch": _batch_number(text, file_name),
        "Report Type": SYSTEM,
        "Rows": pd.DataFrame(rows, columns=COLUMNS),
    }


def _overview_rows(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        rf"(API\s*Charge|Water\s*Charge|Wetting|Premixing|Mixing|Blending)\s+"
        rf"(API\s*Charge|Water\s*Charge|Mixing\s*Extruder\s*[123])\s+"
        rf"({DT_PATTERN})\s+({DT_PATTERN})\s+(\d+)\s+(\d+)",
        re.IGNORECASE,
    )
    labels = {
        "api charge": "API charging",
        "water charge": "Water charging",
        "wetting": "Wetting",
        "premixing": "Pre-mixing",
        "mixing": "Mixing",
        "blending": "Blending",
    }
    rows = []
    for match in pattern.finditer(_clean(text)):
        process = _clean(match.group(1)).casefold()
        rows.append(
            _row(
                labels[process], _clean(match.group(2)).title(),
                match.group(3), match.group(4), "Batch Process Overview",
                int(match.group(5)), int(match.group(6)),
            )
        )
    return rows


def _find_blending_pages(reader: PdfReader) -> list[int]:
    """Locate the Blending Datalog section without parsing every PDF page."""
    page_count = len(reader.pages)
    step = 10
    hits = []
    for index in range(0, page_count, step):
        text = _page_text(reader, index).upper()
        if "BLENDING DATALOG" in text:
            hits.append(index)
    if not hits:
        return list(range(page_count))

    low = max(0, min(hits) - step)
    high = min(page_count - 1, max(hits) + step)
    return list(range(low, high + 1))


def _extract_blending_events(reader: PdfReader) -> pd.DataFrame:
    """Extract timestamp, boundary step and cycle from complete record blocks."""
    date_re = re.compile(DT_PATTERN)
    known_steps = (
        "close 2mm orifice",
        "sample: ips ph",
        "sample ips ph",
        "adjust ph",
        "blending to accumulator left",
        "blending to accumulator right",
    )
    events: list[dict[str, Any]] = []

    for page_index in _find_blending_pages(reader):
        text = _page_text(reader, page_index)
        upper = text.upper()
        if not date_re.search(text):
            continue
        if not any(step.upper() in upper for step in known_steps):
            continue

        timestamps = list(date_re.finditer(text))
        for position, timestamp in enumerate(timestamps):
            end = (
                timestamps[position + 1].start()
                if position + 1 < len(timestamps)
                else len(text)
            )
            record = _clean(text[timestamp.end():end])
            lower = record.casefold()
            step = next((item for item in known_steps if item in lower), None)
            if step is None:
                continue
            step_position = lower.find(step)
            after_step = record[step_position + len(step):]
            cycle_match = re.search(r"(?:^|\s)(\d{1,3})(?=\s|$)", after_step)
            cycle = int(cycle_match.group(1)) if cycle_match else None
            events.append(
                {
                    "Time": _date(timestamp.group(0)),
                    "Step": step,
                    "Cycle": cycle,
                    "Page": page_index + 1,
                }
            )

    if not events:
        return pd.DataFrame(columns=["Time", "Step", "Cycle", "Page"])
    return (
        pd.DataFrame(events)
        .drop_duplicates(subset=["Time", "Step", "Cycle"])
        .sort_values(["Time", "Page"])
        .reset_index(drop=True)
    )


def _extract_blending_events_fast(source: Any) -> pd.DataFrame:
    """Extract blending boundaries quickly with PyMuPDF.

    The previous pypdf page scan could exceed Streamlit Cloud execution limits
    on 2,000+ page reports. PyMuPDF scans the same pages in seconds and returns
    line-preserved text suitable for the report tables.
    """
    if fitz is None:
        # Safe fallback. This is slower for very large reports, but it keeps
        # the application operational if PyMuPDF is not installed yet.
        return _extract_blending_events(_reader(source))

    pdf_bytes = _read_bytes(source)
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    row_pattern = re.compile(
        rf"(?m)^({DT_PATTERN})\s+"
        rf"(Blending to accumulator (?:left|right)|Close 2mm Orifice|"
        rf"Sample: IPS PH|Adjust PH)\s+(\d{{1,3}})\b",
        re.IGNORECASE,
    )
    events: list[dict[str, Any]] = []
    section_started = False
    empty_after_section = 0

    try:
        for page_index in range(len(document)):
            text = document.load_page(page_index).get_text("text", sort=True)
            upper = text.upper()

            if "BLENDING DATALOG" in upper:
                section_started = True
                empty_after_section = 0
            elif section_started:
                # Continuation pages may omit the repeated heading. Keep pages
                # with known blending steps; stop after a sustained run of
                # unrelated pages after the section.
                has_step = any(
                    token in upper
                    for token in (
                        "BLENDING TO ACCUMULATOR",
                        "CLOSE 2MM ORIFICE",
                        "SAMPLE: IPS PH",
                        "ADJUST PH",
                    )
                )
                empty_after_section = 0 if has_step else empty_after_section + 1
                if empty_after_section >= 15:
                    break

            if not section_started:
                continue

            for match in row_pattern.finditer(text):
                events.append(
                    {
                        "Time": _date(match.group(1)),
                        "Step": _clean(match.group(2)).casefold(),
                        "Cycle": int(match.group(3)),
                        "Page": page_index + 1,
                    }
                )
    finally:
        document.close()

    if not events:
        return pd.DataFrame(columns=["Time", "Step", "Cycle", "Page"])
    return (
        pd.DataFrame(events)
        .drop_duplicates(subset=["Time", "Step", "Cycle"])
        .sort_values(["Time", "Page"])
        .reset_index(drop=True)
    )


def _validate_blending_sequence(
    events: pd.DataFrame,
    blending_start: datetime,
    blending_end: datetime,
) -> dict[str, Any]:
    """Validate cycle 60/sample/adjust/cycle 100/sample transitions.

    A Close 2mm Orifice row may reset its cycle to 1. The completed cycle is
    therefore read from the last preceding Blending to accumulator record,
    while the close-row timestamp is used as the phase end.
    """
    window = events[
        (events["Time"] >= blending_start) & (events["Time"] <= blending_end)
    ].copy().sort_values(["Time", "Page"]).reset_index(drop=True)

    result: dict[str, Any] = {
        "ok": False, "code": "BLEND_SEQUENCE_INVALID",
        "message": "Blending phase sequence could not be validated.",
        "pre_end": None, "sample1_end": None,
        "adjust_start": None, "adjust_end": None,
        "post_start": None, "post_end": None, "sample2_end": None,
    }
    if window.empty:
        result.update(
            code="BLEND_NO_DATALOG",
            message="No Blending Datalog boundary rows were extracted.",
        )
        return result

    sample_steps = {"sample: ips ph", "sample ips ph"}
    is_sample = window["Step"].isin(sample_steps)
    is_adjust = window["Step"].eq("adjust ph")
    is_blending = window["Step"].str.startswith("blending to accumulator")

    def completion(
        phase_rows: pd.DataFrame,
        required_cycle: int,
        error_code: str,
        phase_name: str,
    ) -> tuple[datetime | None, dict[str, Any] | None]:
        close_rows = phase_rows[phase_rows["Step"].eq("close 2mm orifice")]
        if not close_rows.empty:
            close_row = close_rows.iloc[-1]
            blending_rows = phase_rows[
                (phase_rows.index < close_row.name)
                & phase_rows["Step"].str.startswith("blending to accumulator")
            ]
        else:
            close_row = None
            blending_rows = phase_rows[
                phase_rows["Step"].str.startswith("blending to accumulator")
            ]
        if blending_rows.empty:
            failure = result.copy()
            failure.update(
                code=f"{error_code}_CYCLE_MISSING",
                message=f"No blending cycle was found before {phase_name} completion.",
            )
            return None, failure

        last_blending = blending_rows.iloc[-1]
        cycle_value = pd.to_numeric(
            pd.Series([last_blending["Cycle"]]), errors="coerce"
        ).iloc[0]
        if pd.isna(cycle_value) or int(cycle_value) != required_cycle:
            failure = result.copy()
            failure.update(
                code=error_code,
                message=(
                    f"{phase_name} did not stop at cycle {required_cycle}. "
                    f"Last Blending to accumulator cycle before closing/sampling: "
                    f"{cycle_value}."
                ),
            )
            return None, failure

        end_time = close_row["Time"] if close_row is not None else last_blending["Time"]
        return end_time, None

    first_sample_indices = window.index[is_sample]
    if len(first_sample_indices) == 0:
        result.update(code="BLEND_SAMPLE1_MISSING", message="pH sample 1 was not found.")
        return result
    first_sample_index = int(first_sample_indices.min())

    pre_end, failure = completion(
        window.loc[window.index < first_sample_index],
        60, "BLEND_DID_NOT_STOP_AT_60", "Pre-pH blending",
    )
    if failure:
        return failure

    adjust_indices = window.index[(window.index > first_sample_index) & is_adjust]
    if len(adjust_indices) == 0:
        result.update(code="BLEND_ADJUST_PH_MISSING", message="Adjust PH was not found.")
        return result
    first_adjust_index = int(adjust_indices.min())
    adjust_start = window.loc[first_adjust_index, "Time"]

    sample1_rows = window[
        (window.index >= first_sample_index)
        & (window.index < first_adjust_index)
        & is_sample
    ]
    sample1_end = sample1_rows["Time"].max()

    post_start_indices = window.index[
        (window.index > first_adjust_index) & is_blending
    ]
    if len(post_start_indices) == 0:
        result.update(code="BLEND_POST_START_MISSING", message="Post-pH blending was not found.")
        return result
    post_start_index = int(post_start_indices.min())
    post_start = window.loc[post_start_index, "Time"]

    adjust_rows = window[
        (window.index >= first_adjust_index)
        & (window.index < post_start_index)
        & is_adjust
    ]
    adjust_end = adjust_rows["Time"].max() if not adjust_rows.empty else adjust_start

    sample2_indices = window.index[(window.index > post_start_index) & is_sample]
    if len(sample2_indices) == 0:
        result.update(code="BLEND_SAMPLE2_MISSING", message="pH sample 2 was not found.")
        return result
    first_sample2_index = int(sample2_indices.min())

    post_end, failure = completion(
        window[
            (window.index >= post_start_index)
            & (window.index < first_sample2_index)
        ],
        100, "BLEND_DID_NOT_STOP_AT_100", "Post-pH blending",
    )
    if failure:
        return failure

    sample2_rows = window[(window.index >= first_sample2_index) & is_sample]
    sample2_last = sample2_rows["Time"].max() if not sample2_rows.empty else post_end
    sample2_end = max(sample2_last, blending_end)

    result.update(
        ok=True, code="OK",
        message="Validated cycles 60 and 100 using the last blending row before closing.",
        pre_end=pre_end, sample1_end=sample1_end,
        adjust_start=adjust_start, adjust_end=adjust_end,
        post_start=post_start, post_end=post_end, sample2_end=sample2_end,
    )
    return result


def _blending_phase_rows(
    events: pd.DataFrame,
    blending_start: datetime,
    blending_end: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validation = _validate_blending_sequence(events, blending_start, blending_end)
    if not validation["ok"]:
        return [], validation
    boundaries = [
        ("Pre-pH Blending", blending_start, validation["pre_end"], "Cycle 60 completion"),
        ("pH sample", validation["pre_end"], validation["sample1_end"], "pH sample 1"),
        ("pH adjustment", validation["adjust_start"], validation["adjust_end"], "Adjust PH"),
        ("Post-pH blending", validation["post_start"], validation["post_end"], "Cycle 100 completion"),
        ("pH sample", validation["post_end"], validation["sample2_end"], "pH sample 2"),
    ]
    rows = [
        _row(stage, "Blending", start, end, f"Blending Datalog - {rule}")
        for stage, start, end, rule in boundaries
        if start is not None and end is not None and end >= start
    ]
    return rows, validation


def parse_batch_cycle(source: Any, file_name: str = "") -> dict[str, Any]:
    """Parse one Batch Cycle PDF without assuming it contains all operations.

    Blending validation is deliberately deferred to combine_reports(), where
    events from every PDF belonging to the same batch are merged and sorted.
    This supports split reports such as EG26050 part 1 (through pH sample 1)
    and part 2 (Adjust PH, post-pH blending, and pH sample 2).
    """
    reader = _reader(source)
    overview_text = _first_pages_text(reader, 4)
    raw_rows = _overview_rows(overview_text)
    rows: list[dict[str, Any]] = []

    for stage in ("Water charging", "API charging"):
        matching = [item for item in raw_rows if item["Stage"] == stage]
        if matching:
            start_time = min(item["Process Start"] for item in matching)
            end_time = max(item["Process End"] for item in matching)
            rows.append(
                _row(
                    stage,
                    stage,
                    start_time,
                    end_time,
                    "Minimum start to maximum end of Process Overview",
                    total=len(matching),
                )
            )

    rows.extend(
        item
        for item in raw_rows
        if item["Stage"] not in {"Water charging", "API charging", "Blending"}
    )

    overview_blending = [item for item in raw_rows if item["Stage"] == "Blending"]
    overview_start = (
        min(item["Process Start"] for item in overview_blending)
        if overview_blending else None
    )
    overview_end = (
        max(item["Process End"] for item in overview_blending)
        if overview_blending else None
    )

    # Each report may hold only part of the blending sequence. Preserve raw
    # boundary events and allow combine_reports() to stitch all report parts.
    blending_events = _extract_blending_events_fast(source)
    if not blending_events.empty:
        blending_events = blending_events.copy()
        blending_events["Source Report"] = file_name
        blending_events["Batch"] = _batch_number(overview_text, file_name)

    return {
        "Batch": _batch_number(overview_text, file_name),
        "Report Type": BATCH,
        "Rows": pd.DataFrame(rows, columns=COLUMNS),
        "Blending Events": blending_events,
        "Blending Overview Start": overview_start,
        "Blending Overview End": overview_end,
        "Source File": file_name,
        "Warnings": [],
    }

def parse_pfs(source: Any, file_name: str = "") -> dict[str, Any]:
    reader = _reader(source)
    text = _first_pages_text(reader, 8)
    start = re.search(rf"Batch Start Time\s*:\s*({DT_PATTERN})", text, re.I)
    end = re.search(rf"Batch End Time\s*:\s*({DT_PATTERN})", text, re.I)
    rows = []
    if start and end:
        rows.append(
            _row(
                "Filling", "PFS Filling Line", start.group(1), end.group(1),
                "PFS batch header",
            )
        )
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
    """Merge all reports batch-wise, including split blending reports."""
    combined_batches: list[pd.DataFrame] = []

    for batch in sorted({report["Batch"] for report in reports}):
        group = [report for report in reports if report["Batch"] == batch]
        tables = [report["Rows"] for report in group if not report["Rows"].empty]
        table = (
            pd.concat(tables, ignore_index=True)
            if tables else pd.DataFrame(columns=COLUMNS)
        )

        cycle_reports = [
            report for report in group if report["Report Type"] == BATCH
        ]
        if cycle_reports:
            event_tables = [
                report.get("Blending Events", pd.DataFrame())
                for report in cycle_reports
                if not report.get("Blending Events", pd.DataFrame()).empty
            ]
            merged_events = (
                pd.concat(event_tables, ignore_index=True)
                .drop_duplicates(subset=["Time", "Step", "Cycle"])
                .sort_values(["Time", "Page"])
                .reset_index(drop=True)
                if event_tables
                else pd.DataFrame(columns=["Time", "Step", "Cycle", "Page"])
            )

            overview_starts = [
                report.get("Blending Overview Start")
                for report in cycle_reports
                if report.get("Blending Overview Start") is not None
            ]
            overview_ends = [
                report.get("Blending Overview End")
                for report in cycle_reports
                if report.get("Blending Overview End") is not None
            ]

            # A split continuation report may not repeat Process Overview.
            # In that case use the event range to extend the overall interval.
            event_start = merged_events["Time"].min() if not merged_events.empty else None
            event_end = merged_events["Time"].max() if not merged_events.empty else None
            blending_start = min(overview_starts + ([event_start] if event_start is not None else [])) if (overview_starts or event_start is not None) else None
            blending_end = max(overview_ends + ([event_end] if event_end is not None else [])) if (overview_ends or event_end is not None) else None

            if blending_start is not None and blending_end is not None:
                phases, validation = _blending_phase_rows(
                    merged_events, blending_start, blending_end
                )
                if phases:
                    table = pd.concat(
                        [table, pd.DataFrame(phases, columns=COLUMNS)],
                        ignore_index=True,
                    )
                else:
                    diagnostic = _row(
                        "Blending",
                        "Blending",
                        blending_start,
                        blending_end,
                        f"Validation failure after merging {len(cycle_reports)} report(s): "
                        f"{validation['message']}",
                    )
                    diagnostic["Validation"] = validation["code"]
                    table = pd.concat(
                        [table, pd.DataFrame([diagnostic])],
                        ignore_index=True,
                    )

        filling_reports = [
            report for report in group
            if report["Report Type"] == PFS and not report["Rows"].empty
        ]
        if cycle_reports and filling_reports and not table.empty:
            cycle_rows = table[table["Stage"] != "Filling"]
            cycle_end = cycle_rows["Process End"].max() if not cycle_rows.empty else None
            filling_start = min(
                report["Rows"]["Process Start"].min()
                for report in filling_reports
            )
            if (
                cycle_end is not None
                and pd.notna(cycle_end)
                and pd.notna(filling_start)
                and filling_start >= cycle_end
                and not (table["Stage"] == "Filling setup").any()
            ):
                table = pd.concat(
                    [
                        table,
                        pd.DataFrame(
                            [_row(
                                "Filling setup",
                                "Inter-report gap",
                                cycle_end,
                                filling_start,
                                "Merged batch-cycle end to PFS filling start",
                            )]
                        ),
                    ],
                    ignore_index=True,
                )

        if table.empty:
            continue
        table.insert(0, "Batch", batch)
        combined_batches.append(table)

    if not combined_batches:
        return pd.DataFrame(columns=["Batch", *COLUMNS])
    return (
        pd.concat(combined_batches, ignore_index=True)
        .drop_duplicates(
            subset=["Batch", "Stage", "Machine", "Process Start", "Process End"]
        )
        .sort_values(["Batch", "Process Start", "Stage", "Machine"])
        .reset_index(drop=True)
    )

