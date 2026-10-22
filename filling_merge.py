"""Consolidate multiple PFS filling reports belonging to the same base batch.

Examples
--------
EG26063 and EG26063-01 are treated as the same base batch EG26063.
The combined Filling interval is:
    start = minimum report start
    end   = maximum report end
"""
from __future__ import annotations

import re
from typing import Any

import pandas as pd


def normalize_base_batch(value: Any) -> str:
    """Return the base EG batch, removing continuation suffixes such as -01."""
    text = str(value or "").strip().upper()
    match = re.search(r"\b([A-Z]{2}\d{5})(?:[-_/ ]?\d+)?\b", text)
    return match.group(1) if match else text


def consolidate_filling_reports(actual: pd.DataFrame) -> pd.DataFrame:
    """Replace multiple Filling rows per base batch with one elapsed interval.

    Rules:
    - group by normalized base batch number;
    - Process Start = MIN(Process Start) across all Filling reports;
    - Process End = MAX(Process End) across all Filling reports;
    - Duration = Process End - Process Start, including gaps between reports;
    - preserve all non-Filling rows;
    - keep traceability in Source and Total.
    """
    if actual is None or actual.empty:
        return actual

    required = {"Batch", "Stage", "Process Start", "Process End"}
    missing = required.difference(actual.columns)
    if missing:
        raise ValueError(
            "Cannot consolidate Filling reports. Missing columns: "
            + ", ".join(sorted(missing))
        )

    output = actual.copy()
    output["Batch"] = output["Batch"].map(normalize_base_batch)
    output["Process Start"] = pd.to_datetime(
        output["Process Start"], errors="coerce"
    )
    output["Process End"] = pd.to_datetime(
        output["Process End"], errors="coerce"
    )

    filling_mask = output["Stage"].astype(str).str.strip().str.casefold().eq(
        "filling"
    )
    non_filling = output.loc[~filling_mask].copy()
    filling = output.loc[filling_mask].copy()

    if filling.empty:
        return output.sort_values(
            ["Batch", "Process Start", "Stage"], na_position="last"
        ).reset_index(drop=True)

    consolidated_rows: list[dict[str, Any]] = []
    for batch, group in filling.groupby("Batch", sort=False):
        starts = group["Process Start"].dropna()
        ends = group["Process End"].dropna()
        if starts.empty or ends.empty:
            consolidated_rows.extend(group.to_dict("records"))
            continue

        start_time = starts.min()
        end_time = ends.max()
        duration_hours = (end_time - start_time).total_seconds() / 3600

        record = {column: None for column in output.columns}
        record.update(
            {
                "Batch": batch,
                "Stage": "Filling",
                "Machine": "PFS Filling Line",
                "Process Start": start_time,
                "Process End": end_time,
                "Duration (hours)": round(duration_hours, 4),
                "Index": None,
                "Total": int(len(group)),
                "Source": (
                    f"Merged {len(group)} PFS Filling report(s): "
                    "MIN start to MAX end"
                ),
                "Validation": "OK" if end_time >= start_time else "End before start",
            }
        )
        consolidated_rows.append(record)

    consolidated = pd.DataFrame(consolidated_rows, columns=output.columns)
    return (
        pd.concat([non_filling, consolidated], ignore_index=True)
        .sort_values(
            ["Batch", "Process Start", "Stage"], na_position="last"
        )
        .reset_index(drop=True)
    )
