"""EB Execution Date Extractor with multi-report batch stitching."""
from __future__ import annotations

import io
from collections import OrderedDict
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from parser import combine_reports, parse_pdf

st.set_page_config(page_title="EB Execution Date Extractor", page_icon="📊", layout="wide")

MAX_DURATION_STAGES = {
    "Seal dry run", "Leak test", "Vacuum test (Max)",
    "Wetting", "Pre-mixing", "Mixing",
}


def format_dates(table: pd.DataFrame) -> pd.DataFrame:
    result = table.copy()
    for column in ("Process Start", "Process End"):
        if column in result.columns:
            result[column] = pd.to_datetime(result[column], errors="coerce").dt.strftime(
                "%d/%m/%Y %H:%M:%S"
            )
    return result


def process_summary(table: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for stage, group in table.groupby("Stage", sort=False):
        valid = group[group["Validation"].astype(str).eq("OK")]
        durations = pd.to_numeric(valid["Duration (hours)"], errors="coerce").dropna()
        if durations.empty:
            continue
        value = durations.max() if stage in MAX_DURATION_STAGES else durations.sum()
        rows.append({
            "Stage": stage,
            "Process time (hours)": round(float(value), 3),
            "Calculation": "Maximum parallel duration" if stage in MAX_DURATION_STAGES else "Sequential duration",
        })
    return pd.DataFrame(rows)


def excel_output(actual: pd.DataFrame, summaries: dict[str, pd.DataFrame], log: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl", datetime_format="dd/mm/yyyy hh:mm:ss") as writer:
        actual.to_excel(writer, index=False, sheet_name="Actual Process Time")
        all_summaries = []
        for batch, summary in summaries.items():
            item = summary.copy()
            item.insert(0, "Batch", batch)
            all_summaries.append(item)
        if all_summaries:
            pd.concat(all_summaries, ignore_index=True).to_excel(
                writer, index=False, sheet_name="Process Time Summary"
            )
        log.to_excel(writer, index=False, sheet_name="Extraction Log")
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cells in sheet.columns:
                width = min(max(len(str(cell.value or "")) for cell in cells) + 2, 45)
                sheet.column_dimensions[cells[0].column_letter].width = width
    return buffer.getvalue()


def main() -> None:
    st.title("EB Execution Date Extractor")
    st.caption(
        "Upload all report parts together. Files are grouped by the batch number inside each report; "
        "split operations are merged chronologically before phase validation."
    )

    files = st.file_uploader(
        "Upload System Integrity, Batch Cycle, continuation, and PFS Filling PDFs",
        type=["pdf"],
        accept_multiple_files=True,
    )
    if not files:
        st.info("Select all available PDF parts for the required batches.")
        return

    total_bytes = sum(int(getattr(file, "size", 0) or 0) for file in files)
    size_text = f" ({total_bytes / 1024 / 1024:.1f} MB)" if total_bytes else ""
    st.write(f"**{len(files)} document(s) received{size_text}.**")

    progress = st.progress(0, text="Preparing uploaded documents...")
    status = st.empty()
    reports: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []

    # Sort input names for deterministic processing. Final grouping is based on
    # batch number extracted from report content, not on upload order.
    sorted_files = sorted(files, key=lambda item: item.name.casefold())
    for index, uploaded in enumerate(sorted_files, start=1):
        percent = (index - 1) / len(sorted_files)
        progress.progress(percent, text=f"Reading {uploaded.name} ({index}/{len(sorted_files)})")
        status.info(f"Extracting report structure and datalog: {uploaded.name}")
        try:
            report = parse_pdf(uploaded, uploaded.name)
            reports.append(report)
            warning_list = report.get("Warnings", [])
            error_message = report.get("Error", "")
            log_rows.append({
                "Batch": report.get("Batch", "UNKNOWN"),
                "File": uploaded.name,
                "Report type": report.get("Report Type", "Unknown"),
                "Rows extracted before merge": len(report.get("Rows", [])),
                "Blending events": len(report.get("Blending Events", [])),
                "Status": "Error" if error_message else "Part loaded",
                "Message": error_message or "; ".join(map(str, warning_list)),
            })
        except Exception as error:
            log_rows.append({
                "Batch": "UNKNOWN", "File": uploaded.name, "Report type": "Error",
                "Rows extracted before merge": 0, "Blending events": 0,
                "Status": "Failed", "Message": str(error),
            })
        progress.progress(index / len(sorted_files), text=f"Processed {index}/{len(sorted_files)} documents")

    status.info("Grouping report parts by batch number and merging operations...")
    log = pd.DataFrame(log_rows).sort_values(["Batch", "File"]).reset_index(drop=True)
    actual = combine_reports(reports)
    progress.progress(1.0, text="Upload processing and batch merge complete")
    status.empty()

    st.subheader("Batch-wise Report Grouping")
    grouped = (
        log.groupby("Batch", dropna=False)
        .agg(
            Report_parts=("File", "count"),
            Files=("File", lambda values: " | ".join(values)),
            Report_types=("Report type", lambda values: " | ".join(sorted(set(values)))),
            Blending_events=("Blending events", "sum"),
        )
        .reset_index()
    )
    st.dataframe(grouped, use_container_width=True, hide_index=True)

    failed = log[log["Status"].eq("Failed")]
    if not failed.empty:
        st.error("One or more reports could not be processed. Review Extraction Log.")

    if actual.empty:
        st.error("No execution records were available after batch-wise merging.")
        st.dataframe(log, use_container_width=True, hide_index=True)
        return

    batches = actual["Batch"].dropna().astype(str).drop_duplicates().tolist()
    selected_batch = st.selectbox("Select batch", batches)
    batch_table = actual[actual["Batch"].astype(str).eq(selected_batch)].copy()
    summary = process_summary(batch_table)

    warning_rows = batch_table[~batch_table["Validation"].astype(str).eq("OK")]
    if not warning_rows.empty:
        messages = warning_rows[["Stage", "Validation", "Source"]].drop_duplicates()
        st.warning(
            "Merged batch validation requires review:\n\n- "
            + "\n- ".join(
                f"{row.Stage}: {row.Validation} | {row.Source}"
                for row in messages.itertuples(index=False)
            )
        )

    tabs = st.tabs(["Actual Process Time", "Summary", "Plots", "Extraction Log"])
    with tabs[0]:
        st.dataframe(format_dates(batch_table), use_container_width=True, hide_index=True)

    with tabs[1]:
        st.dataframe(summary, use_container_width=True, hide_index=True)

    with tabs[2]:
        timeline = batch_table[
            batch_table["Validation"].astype(str).eq("OK")
        ].dropna(subset=["Process Start", "Process End"])
        if not timeline.empty:
            figure = px.timeline(
                timeline, x_start="Process Start", x_end="Process End",
                y="Stage", color="Machine",
                hover_data=["Duration (hours)", "Source"],
                title=f"{selected_batch} merged execution timeline",
            )
            figure.update_yaxes(autorange="reversed")
            st.plotly_chart(figure, use_container_width=True)
        if not summary.empty:
            summary_figure = px.bar(
                summary, x="Process time (hours)", y="Stage", orientation="h",
                color="Process time (hours)", color_continuous_scale="Blues",
                title=f"{selected_batch} process-time summary",
            )
            summary_figure.update_yaxes(categoryorder="total ascending")
            st.plotly_chart(summary_figure, use_container_width=True)

    with tabs[3]:
        st.dataframe(log, use_container_width=True, hide_index=True)

    summaries = {
        batch: process_summary(actual[actual["Batch"].astype(str).eq(batch)])
        for batch in batches
    }
    st.download_button(
        "Download merged execution workbook",
        excel_output(actual, summaries, log),
        file_name="EB_execution_dates_merged_by_batch.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )


if __name__ == "__main__":
    main()
