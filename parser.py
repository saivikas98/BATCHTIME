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


def _extract_blending_events(reader: PdfReader) -> pd.DataFrame:
    """Extract only boundary-relevant rows from BLENDING DATALOG pages."""
    events: list[dict[str, Any]] = []
    date_re = re.compile(DT_PATTERN)
    cycle_re = re.compile(r"\b(\d{1,3})\b")
    known_steps = (
        "close 2mm orifice",
        "sample: ips ph",
        "sample ips ph",
        "adjust ph",
        "blending to accumulator left",
        "blending to accumulator right",
    )

    for page_index in range(len(reader.pages)):
        text = _page_text(reader, page_index)
        if "BLENDING DATALOG" not in text.upper():
            continue

        # pypdf normally preserves each datalog record on a line. A fallback
        # split at each date handles pages whose rows are concatenated.
        lines = text.splitlines()
        if not any(date_re.search(line) for line in lines):
            lines = re.split(rf"(?={DT_PATTERN})", _clean(text))

        for line in lines:
            date_match = date_re.search(line)
            if not date_match:
                continue
            lower = _clean(line).casefold()
            step = next((item for item in known_steps if item in lower), None)
            if step is None:
                continue

            after_step = lower.split(step, 1)[1]
            cycle_match = cycle_re.search(after_step)
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



def _validate_cycle_60(events: pd.DataFrame, blending_start: datetime, blending_end: datetime) -> dict[str, Any]:
    """Validate that pre-pH cycle 60 and its completion marker are present.

    Returns a structured result used both for extraction control and for
    user-visible validation messages. Missing cycle 60 is treated as a hard
    validation failure because the Pre-pH Blending endpoint cannot be proven.
    """
    window = events[
        (events["Time"] >= blending_start) & (events["Time"] <= blending_end)
    ].copy()
    if window.empty:
        return {
            "ok": False,
            "code": "BLEND_NO_DATALOG",
            "message": "No Blending Datalog boundary rows were extracted.",
            "cycle_60_end": None,
        }

    cycle = pd.to_numeric(window["Cycle"], errors="coerce")
    cycle_60 = window[cycle.eq(60)].copy()
    if cycle_60.empty:
        return {
            "ok": False,
            "code": "BLEND_CYCLE_60_MISSING",
            "message": (
                "Cycle 60 is missing from the Blending Datalog. "
                "Pre-pH Blending and downstream pH-phase durations were not calculated."
            ),
            "cycle_60_end": None,
        }

    close_60 = cycle_60[cycle_60["Step"].eq("close 2mm orifice")]
    if not close_60.empty:
        return {
            "ok": True,
            "code": "OK",
            "message": "Cycle 60 completion validated using Close 2mm Orifice.",
            "cycle_60_end": close_60["Time"].max(),
        }

    # Cycle 60 exists, but the preferred completion marker is absent. Use the
    # final cycle-60 blending record as a transparent fallback and flag it.
    blending_60 = cycle_60[
        cycle_60["Step"].str.startswith("blending to accumulator")
    ]
    if not blending_60.empty:
        return {
            "ok": True,
            "code": "BLEND_CYCLE_60_CLOSE_MISSING",
            "message": (
                "Cycle 60 exists, but Close 2mm Orifice is missing. "
                "The final cycle-60 blending timestamp was used as a fallback."
            ),
            "cycle_60_end": blending_60["Time"].max(),
        }

    return {
        "ok": False,
        "code": "BLEND_CYCLE_60_COMPLETION_MISSING",
        "message": (
            "Cycle 60 was detected, but no valid cycle-60 completion row was found. "
            "Pre-pH Blending and downstream pH-phase durations were not calculated."
        ),
        "cycle_60_end": None,
    }

def _blending_phase_rows(
    events: pd.DataFrame,
    blending_start: datetime,
    blending_end: datetime,
) -> list[dict[str, Any]]:
    """Convert ordered datalog boundaries into the five blending phases."""
    if events.empty:
        return []

    events = events[
        (events["Time"] >= blending_start) & (events["Time"] <= blending_end)
    ].copy()
    if events.empty:
        return []

    step = events["Step"]
    cycle = pd.to_numeric(events["Cycle"], errors="coerce")

    # 1) Pre-pH blending ends only after cycle 60 has been validated.
    cycle_60_validation = _validate_cycle_60(
        events, blending_start, blending_end
    )
    if not cycle_60_validation["ok"]:
        return []
    pre_end = cycle_60_validation["cycle_60_end"]

    # 2) pH sample 1 runs from pre-pH completion through the final sampling
    # row immediately before the first Adjust PH row.
    first_adjust = _first_time(
        events,
        (events["Time"] > pre_end) & step.eq("adjust ph"),
    )
    if first_adjust is None:
        return []
    sample1_end = _last_time(
        events,
        (events["Time"] >= pre_end)
        & (events["Time"] < first_adjust)
        & step.isin(["sample: ips ph", "sample ips ph"]),
    )
    sample1_end = sample1_end or pre_end

    # 3) pH adjustment ends at the final Adjust PH row before post-pH blending.
    first_post_blend = _first_time(
        events,
        (events["Time"] > first_adjust)
        & step.str.startswith("blending to accumulator"),
    )
    if first_post_blend is None:
        return []
    adjustment_end = _last_time(
        events,
        (events["Time"] >= first_adjust)
        & (events["Time"] < first_post_blend)
        & step.eq("adjust ph"),
    )
    adjustment_end = adjustment_end or first_adjust

    # 4) Post-pH blending ends at the closing event for the post-pH cycle.
    # Prefer a Close 2mm Orifice event with cycle > 60 and before sample 2.
    post_close_candidates = events[
        (events["Time"] >= first_post_blend)
        & step.eq("close 2mm orifice")
        & cycle.gt(60)
    ]
    if post_close_candidates.empty:
        post_close_candidates = events[
            (events["Time"] >= first_post_blend)
            & step.eq("close 2mm orifice")
        ]
    post_end = (
        post_close_candidates["Time"].max()
        if not post_close_candidates.empty
        else None
    )
    if post_end is None:
        return []

    # 5) pH sample 2 begins at post-pH completion and ends at the final sample
    # row. If the report's Process Overview end is later, retain that official
    # process end so the phase reconciles to the complete Blending interval.
    sample2_last = _last_time(
        events,
        (events["Time"] >= post_end)
        & step.isin(["sample: ips ph", "sample ips ph"]),
    )
    sample2_end = blending_end if blending_end >= (sample2_last or post_end) else sample2_last
    sample2_end = sample2_end or blending_end

    boundaries = [
        ("Pre-pH Blending", blending_start, pre_end, "Cycle 60 completion"),
        ("pH sample", pre_end, sample1_end, "Last sample before Adjust PH"),
        ("pH adjustment", first_adjust, adjustment_end, "Adjust PH interval"),
        ("Post-pH blending", first_post_blend, post_end, "Post-pH cycle completion"),
        ("pH sample", post_end, sample2_end, "Final pH sample interval"),
    ]

    return [
        _row(stage, "Blending", start, end, f"Blending Datalog - {rule}")
        for stage, start, end, rule in boundaries
        if start is not None and end is not None and end >= start
    ]


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
        cycle_60_validation = _validate_cycle_60(
            events, blending_start, blending_end
        )
        phase_rows = _blending_phase_rows(events, blending_start, blending_end)
        if phase_rows:
            # Preserve a warning when the final cycle-60 blending row had to be
            # used because Close 2mm Orifice was absent.
            if cycle_60_validation["code"] != "OK":
                for phase_row in phase_rows:
                    if phase_row["Stage"] == "Pre-pH Blending":
                        phase_row["Validation"] = cycle_60_validation["code"]
                        phase_row["Source"] += " - fallback used"
            rows.extend(phase_rows)
        else:
            # Preserve the overall interval but mark it invalid for phase-level
            # calculations. The app will display this in the extraction log.
            diagnostic = _row(
                "Blending", "Blending", blending_start, blending_end,
                f"Validation failure - {cycle_60_validation['message']}",
            )
            diagnostic["Validation"] = cycle_60_validation["code"]
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
