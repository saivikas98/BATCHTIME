"""PDF parsers for the EB Execution Date Streamlit application.

Blending phase rules
--------------------
Pre-pH Blending:
    Batch Process Overview blending start -> completion of cycle 60.
    Completion is the cycle-60 ``Close 2mm Orifice`` timestamp. If that event
    is absent, the final cycle-60 blending row before the first pH sample is
    used.
pH sample 1:
    cycle-60 completion -> final ``Sample: IPS PH`` row before ``Adjust PH``.
pH adjustment:
    first ``Adjust PH`` row -> final ``Adjust PH`` row before post-pH blending.
Post-pH blending:
    first blending row after pH adjustment -> completion of the post-pH cycle
    (prefer ``Close 2mm Orifice`` with cycle greater than 60).
pH sample 2:
    post-pH cycle completion -> final pH-sample row / blending process end.
"""
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
    for source in (file_name, text):
        match = re.search(BATCH_PATTERN, source, re.IGNORECASE)
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
    if "PROCESS OVERVIEW" in probe and any(
        marker in probe for marker in ("WETTING", "PREMIXING", "MIXING EXTRUDER")
    ):
        return BATCH
    return UNKNOWN


def parse_system_integrity(source: Any, file_name: str = "") -> dict[str, Any]:
    reader = _reader(source)
    text = _first_pages_text(reader, 2)
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
    normal = _clean(text)
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
    for match in pattern.finditer(normal):
        process = _clean(match.group(1)).casefold()
        rows.append(
            _row(
                labels[process], _clean(match.group(2)).title(),
                match.group(3), match.group(4), "Batch Process Overview",
                int(match.group(5)), int(match.group(6)),
            )
        )
    return rows


def _find_blending_page_range(reader: PdfReader) -> list[int]:
    """Locate the contiguous Blending Datalog section efficiently.

    Large batch reports can exceed 2,000 pages. Scanning every page with
    pypdf is slow. Probe every tenth page first, then expand around all hits.
    """
    page_count = len(reader.pages)
    probe_step = 10
    hits: list[int] = []
    for page_index in range(0, page_count, probe_step):
        text = _page_text(reader, page_index)
        if "BLENDING DATALOG" in text.upper():
            hits.append(page_index)

    if not hits:
        # Fallback for unusually short blending sections located between probes.
        return list(range(page_count))

    first_probe = max(0, min(hits) - probe_step)
    last_probe = min(page_count - 1, max(hits) + probe_step)

    first_page = first_probe
    for page_index in range(first_probe, min(hits) + 1):
        if "BLENDING DATALOG" in _page_text(reader, page_index).upper():
            first_page = page_index
            break

    last_page = last_probe
    for page_index in range(last_probe, max(hits) - 1, -1):
        if "BLENDING DATALOG" in _page_text(reader, page_index).upper():
            last_page = page_index
            break

    return list(range(first_page, last_page + 1))


def _extract_blending_events(reader: PdfReader) -> pd.DataFrame:
    """Extract boundary-relevant rows from Blending Datalog pages.

    pypdf often extracts each table cell on a separate line, for example:
    date, step, cycle, and measurements are separate newline tokens. Parsing
    line-by-line therefore loses the cycle number. This parser splits the page
    into complete records at each timestamp and reads the step and cycle from
    the record block.
    """
    events: list[dict[str, Any]] = []
    date_re = re.compile(DT_PATTERN)
    known_steps = (
        "close 2mm orifice",
        "sample: ips ph",
        "sample ips ph",
        "adjust ph",
        "blending to accumulator left",
        "blending to accumulator right",
    )

    for page_index in _find_blending_page_range(reader):
        text = _page_text(reader, page_index)
        upper_text = text.upper()

        # Do not require the literal page heading. Some PDF extraction engines
        # omit the repeated "BLENDING DATALOG" header on continuation pages.
        # A page is accepted when it contains a blending boundary step and a
        # report timestamp. This also handles EG26070 reliably.
        has_timestamp = bool(date_re.search(text))
        has_boundary_step = any(
            candidate.upper() in upper_text for candidate in known_steps
        )
        if not has_timestamp or not has_boundary_step:
            continue

        matches = list(date_re.finditer(text))
        for record_index, date_match in enumerate(matches):
            record_end = (
                matches[record_index + 1].start()
                if record_index + 1 < len(matches)
                else len(text)
            )
            record = text[date_match.end():record_end]
            clean_record = _clean(record)
            lower_record = clean_record.casefold()
            step = next(
                (candidate for candidate in known_steps if candidate in lower_record),
                None,
            )
            if step is None:
                continue

            # The cycle is the first integer token after the matched step name.
            step_position = lower_record.find(step)
            after_step = clean_record[step_position + len(step):]
            cycle_match = re.search(r"(?:^|\s)(\d{1,3})(?=\s|$)", after_step)
            cycle = int(cycle_match.group(1)) if cycle_match else None

            events.append(
                {
                    "Time": _date(date_match.group(0)),
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

def _last_time(events: pd.DataFrame, mask: pd.Series) -> datetime | None:
    selected = events.loc[mask, "Time"]
    return selected.max() if not selected.empty else None


def _first_time(events: pd.DataFrame, mask: pd.Series) -> datetime | None:
    selected = events.loc[mask, "Time"]
    return selected.min() if not selected.empty else None



def _validate_blending_sequence(
    events: pd.DataFrame,
    blending_start: datetime,
    blending_end: datetime,
) -> dict[str, Any]:
    """Validate the pre-pH, adjustment, and post-pH blending sequence.

    The decision is transition-based, not based only on the presence of cycle
    numbers somewhere in the report:

    * Pre-pH blending is accepted only when blending stops at cycle 60 and the
      next phase is pH sampling.
    * pH sample 1 ends at the final sample row immediately before Adjust PH.
    * pH adjustment ends at the final Adjust PH row before blending resumes.
    * Post-pH blending is accepted only when resumed blending stops at cycle
      100 and the next phase is pH sampling.
    * pH sample 2 begins at cycle-100 completion.
    """
    window = events[
        (events["Time"] >= blending_start) & (events["Time"] <= blending_end)
    ].copy().sort_values(["Time", "Page"]).reset_index(drop=True)

    result: dict[str, Any] = {
        "ok": False,
        "code": "BLEND_SEQUENCE_INVALID",
        "message": "Blending phase sequence could not be validated.",
        "pre_end": None,
        "sample1_end": None,
        "adjust_start": None,
        "adjust_end": None,
        "post_start": None,
        "post_end": None,
        "sample2_end": None,
    }
    if window.empty:
        if events.empty:
            result.update(
                code="BLEND_NO_DATALOG",
                message=(
                    "No Blending Datalog boundary rows were extracted from the PDF. "
                    "The parser did not detect timestamped blending, sampling, or Adjust PH records."
                ),
            )
        else:
            result.update(
                code="BLEND_DATALOG_OUTSIDE_OVERVIEW",
                message=(
                    "Blending Datalog rows were extracted, but none fell within the "
                    f"Process Overview interval {blending_start:%d/%m/%Y %H:%M:%S} to "
                    f"{blending_end:%d/%m/%Y %H:%M:%S}. Extracted event range: "
                    f"{events['Time'].min():%d/%m/%Y %H:%M:%S} to "
                    f"{events['Time'].max():%d/%m/%Y %H:%M:%S}."
                ),
            )
        return result

    sample_steps = {"sample: ips ph", "sample ips ph"}
    is_sample = window["Step"].isin(sample_steps)
    is_adjust = window["Step"].eq("adjust ph")
    is_blending = window["Step"].str.startswith("blending to accumulator")
    is_close = window["Step"].eq("close 2mm orifice")
    is_blend_boundary = is_blending | is_close
    cycle = pd.to_numeric(window["Cycle"], errors="coerce")

    # PRE-PH: identify the first sampling block. The immediately preceding
    # blending boundary must terminate at cycle 60. If blending continues to
    # cycle 61 or higher before sampling, cycle 60 is not a phase endpoint.
    first_sample_index = window.index[is_sample].min() if is_sample.any() else None
    if first_sample_index is None or pd.isna(first_sample_index):
        result.update(
            code="BLEND_SAMPLE1_MISSING",
            message="No first pH-sampling block was found after pre-pH blending.",
        )
        return result

    before_sample1 = window.loc[window.index < int(first_sample_index)].copy()
    boundary_before_sample1 = before_sample1[
        before_sample1["Step"].str.startswith("blending to accumulator")
        | before_sample1["Step"].eq("close 2mm orifice")
    ]
    if boundary_before_sample1.empty:
        result.update(
            code="BLEND_PRE_BOUNDARY_MISSING",
            message="No blending completion row was found before pH sample 1.",
        )
        return result

    final_pre_boundary = boundary_before_sample1.iloc[-1]
    final_pre_cycle = pd.to_numeric(
        pd.Series([final_pre_boundary["Cycle"]]), errors="coerce"
    ).iloc[0]
    if pd.isna(final_pre_cycle) or int(final_pre_cycle) != 60:
        max_pre_cycle = pd.to_numeric(
            boundary_before_sample1["Cycle"], errors="coerce"
        ).max()
        result.update(
            code="BLEND_DID_NOT_STOP_AT_60",
            message=(
                "Blending did not stop at cycle 60 before pH sample 1. "
                f"The final cycle before sampling was {max_pre_cycle}."
            ),
        )
        return result

    pre_end = final_pre_boundary["Time"]
    pre_completion_rule = (
        "Close 2mm Orifice at cycle 60"
        if final_pre_boundary["Step"] == "close 2mm orifice"
        else "final blending row at cycle 60"
    )

    # SAMPLE 1: final sample timestamp before the first Adjust PH timestamp.
    adjust_indices = window.index[(window.index > int(first_sample_index)) & is_adjust]
    if len(adjust_indices) == 0:
        result.update(
            code="BLEND_ADJUST_PH_MISSING",
            message="Adjust PH was not found after pH sample 1.",
        )
        return result
    first_adjust_index = int(adjust_indices.min())
    first_adjust_time = window.loc[first_adjust_index, "Time"]
    sample1_rows = window[
        (window.index >= int(first_sample_index))
        & (window.index < first_adjust_index)
        & is_sample
    ]
    if sample1_rows.empty:
        result.update(
            code="BLEND_SAMPLE1_END_MISSING",
            message="No pH sample 1 row was found before Adjust PH.",
        )
        return result
    sample1_end = sample1_rows["Time"].max()

    # ADJUSTMENT: adjustment ends immediately before blending resumes.
    resumed_blend_indices = window.index[
        (window.index > first_adjust_index) & is_blending
    ]
    if len(resumed_blend_indices) == 0:
        result.update(
            code="BLEND_POST_START_MISSING",
            message="Blending did not resume after Adjust PH.",
        )
        return result
    post_start_index = int(resumed_blend_indices.min())
    post_start = window.loc[post_start_index, "Time"]
    adjustment_rows = window[
        (window.index >= first_adjust_index)
        & (window.index < post_start_index)
        & is_adjust
    ]
    adjust_end = (
        adjustment_rows["Time"].max()
        if not adjustment_rows.empty
        else first_adjust_time
    )

    # POST-PH: identify the first sampling row after blending resumes. The
    # immediately preceding blending boundary must terminate at cycle 100.
    sample2_indices = window.index[(window.index > post_start_index) & is_sample]
    if len(sample2_indices) == 0:
        result.update(
            code="BLEND_SAMPLE2_MISSING",
            message="No pH sample 2 block was found after post-pH blending.",
        )
        return result
    first_sample2_index = int(sample2_indices.min())
    post_boundaries = window[
        (window.index >= post_start_index)
        & (window.index < first_sample2_index)
        & is_blend_boundary
    ]
    if post_boundaries.empty:
        result.update(
            code="BLEND_POST_BOUNDARY_MISSING",
            message="No post-pH blending completion row was found before pH sample 2.",
        )
        return result

    final_post_boundary = post_boundaries.iloc[-1]
    final_post_cycle = pd.to_numeric(
        pd.Series([final_post_boundary["Cycle"]]), errors="coerce"
    ).iloc[0]
    if pd.isna(final_post_cycle) or int(final_post_cycle) != 100:
        max_post_cycle = pd.to_numeric(
            post_boundaries["Cycle"], errors="coerce"
        ).max()
        result.update(
            code="BLEND_DID_NOT_STOP_AT_100",
            message=(
                "Post-pH blending did not stop at cycle 100 before pH sample 2. "
                f"The final cycle before sampling was {max_post_cycle}."
            ),
        )
        return result

    post_end = final_post_boundary["Time"]
    post_completion_rule = (
        "Close 2mm Orifice at cycle 100"
        if final_post_boundary["Step"] == "close 2mm orifice"
        else "final blending row at cycle 100"
    )

    sample2_rows = window[
        (window.index >= first_sample2_index) & is_sample
    ]
    sample2_last = sample2_rows["Time"].max() if not sample2_rows.empty else post_end
    sample2_end = max(sample2_last, blending_end)

    result.update(
        ok=True,
        code="OK",
        message=(
            f"Validated pre-pH completion from {pre_completion_rule}; "
            f"validated post-pH completion from {post_completion_rule}."
        ),
        pre_end=pre_end,
        sample1_end=sample1_end,
        adjust_start=first_adjust_time,
        adjust_end=adjust_end,
        post_start=post_start,
        post_end=post_end,
        sample2_end=sample2_end,
    )
    return result


def _blending_phase_rows(
    events: pd.DataFrame,
    blending_start: datetime,
    blending_end: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build blending phases only after the full 60/sample/adjust/100 sequence validates."""
    validation = _validate_blending_sequence(
        events, blending_start, blending_end
    )
    if not validation["ok"]:
        return [], validation

    boundaries = [
        (
            "Pre-pH Blending",
            blending_start,
            validation["pre_end"],
            "blending stopped at cycle 60 before sampling",
        ),
        (
            "pH sample",
            validation["pre_end"],
            validation["sample1_end"],
            "sample 1 ended immediately before Adjust PH",
        ),
        (
            "pH adjustment",
            validation["adjust_start"],
            validation["adjust_end"],
            "Adjust PH interval before blending resumed",
        ),
        (
            "Post-pH blending",
            validation["post_start"],
            validation["post_end"],
            "resumed blending stopped at cycle 100 before sampling",
        ),
        (
            "pH sample",
            validation["post_end"],
            validation["sample2_end"],
            "sample 2 interval after cycle 100",
        ),
    ]
    rows = [
        _row(stage, "Blending", phase_start, phase_end, f"Blending Datalog - {rule}")
        for stage, phase_start, phase_end, rule in boundaries
        if phase_start is not None and phase_end is not None and phase_end >= phase_start
    ]
    return rows, validation

def parse_batch_cycle(source: Any, file_name: str = "") -> dict[str, Any]:
    reader = _reader(source)
    overview_text = _first_pages_text(reader, 4)
    raw_rows = _overview_rows(overview_text)
    rows: list[dict[str, Any]] = []

    # Charging stages use the overall elapsed interval across repeated charges.
    for stage in ("Water charging", "API charging"):
        matching = [item for item in raw_rows if item["Stage"] == stage]
        if matching:
            start = min(item["Process Start"] for item in matching)
            end = max(item["Process End"] for item in matching)
            rows.append(
                _row(
                    stage, stage, start, end,
                    "Minimum start to maximum end of Process Overview",
                    total=len(matching),
                )
            )

    rows.extend(
        item
        for item in raw_rows
        if item["Stage"] not in {"Water charging", "API charging", "Blending"}
    )

    blending_overview = [item for item in raw_rows if item["Stage"] == "Blending"]
    if blending_overview:
        blending_start = min(item["Process Start"] for item in blending_overview)
        blending_end = max(item["Process End"] for item in blending_overview)
        events = _extract_blending_events(reader)
        phase_rows, blending_validation = _blending_phase_rows(
            events, blending_start, blending_end
        )
        if phase_rows:
            rows.extend(phase_rows)
        else:
            # Retain the overall interval only as a diagnostic record. Invalid
            # phase boundaries are excluded from the process-time summary.
            diagnostic = _row(
                "Blending", "Blending", blending_start, blending_end,
                f"Validation failure - {blending_validation['message']}",
            )
            diagnostic["Validation"] = blending_validation["code"]
            rows.append(diagnostic)

    warnings = [
        str(value)
        for value in pd.DataFrame(rows).get("Validation", pd.Series(dtype=str)).tolist()
        if value not in ("OK", None, "")
    ]
    return {
        "Batch": _batch_number(overview_text, file_name),
        "Report Type": BATCH,
        "Rows": pd.DataFrame(rows, columns=COLUMNS),
        "Warnings": sorted(set(warnings)),
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
    combined = []
    for batch in sorted({report["Batch"] for report in reports}):
        group = [report for report in reports if report["Batch"] == batch]
        tables = [report["Rows"] for report in group if not report["Rows"].empty]
        if not tables:
            continue
        table = pd.concat(tables, ignore_index=True)

        cycle_reports = [
            report for report in group
            if report["Report Type"] == BATCH and not report["Rows"].empty
        ]
        filling_reports = [
            report for report in group
            if report["Report Type"] == PFS and not report["Rows"].empty
        ]
        if cycle_reports and filling_reports:
            cycle_end = cycle_reports[0]["Rows"]["Process End"].max()
            filling_start = filling_reports[0]["Rows"]["Process Start"].min()
            if pd.notna(cycle_end) and pd.notna(filling_start) and filling_start >= cycle_end:
                table = pd.concat(
                    [
                        table,
                        pd.DataFrame(
                            [
                                _row(
                                    "Filling setup", "Inter-report gap",
                                    cycle_end, filling_start,
                                    "Batch-cycle end to PFS filling start",
                                )
                            ]
                        ),
                    ],
                    ignore_index=True,
                )

        table.insert(0, "Batch", batch)
        combined.append(table)

    if not combined:
        return pd.DataFrame(columns=["Batch", *COLUMNS])
    return (
        pd.concat(combined, ignore_index=True)
        .sort_values(["Batch", "Process Start", "Stage", "Machine"])
        .reset_index(drop=True)
    )
