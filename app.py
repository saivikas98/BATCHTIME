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

APP_VERSION = "2026.10.21-multireport-v4"

st.markdown(
    """
    <style>
    .stApp {background-color: #f5f7fb;}
    div[data-testid="stMetric"] {background:#ffffff; border:1px solid #d7deea; padding:14px; border-radius:10px;}
    div[data-testid="stDataFrame"] {background:#ffffff; border:1px solid #d7deea; border-radius:10px; padding:4px;}
    .block-container {padding-top: 1.5rem; max-width: 1500px;}
    h1, h2, h3 {color:#17365d;}
    </style>
    """,
    unsafe_allow_html=True,
)

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



def classify_merged_datalog(events: pd.DataFrame) -> pd.DataFrame:
    """Add phase, duplicate, and boundary labels to merged datalog records."""
    if events.empty:
        return pd.DataFrame(
            columns=[
                "Batch", "Source Report", "Time", "Step", "Cycle", "Page",
                "Phase classification", "Boundary role", "Duplicate status",
            ]
        )

    result = events.copy().sort_values(["Time", "Source Report", "Page"])
    result["Duplicate status"] = "Unique"
    duplicate_mask = result.duplicated(
        subset=["Time", "Step", "Cycle"], keep=False
    )
    result.loc[duplicate_mask, "Duplicate status"] = "Overlap across reports"

    result["Phase classification"] = "Unclassified blending"
    result["Boundary role"] = "Process record"

    samples = result[result["Step"].isin(["sample: ips ph", "sample ips ph"])]
    adjusts = result[result["Step"].eq("adjust ph")]
    blending = result[result["Step"].str.startswith("blending to accumulator")]

    first_sample = samples["Time"].min() if not samples.empty else pd.NaT
    first_adjust = adjusts["Time"].min() if not adjusts.empty else pd.NaT
    last_adjust = adjusts["Time"].max() if not adjusts.empty else pd.NaT
    post_start_candidates = blending[
        blending["Time"] > last_adjust
    ] if pd.notna(last_adjust) else blending.iloc[0:0]
    post_start = (
        post_start_candidates["Time"].min()
        if not post_start_candidates.empty else pd.NaT
    )
    sample2_candidates = samples[
        samples["Time"] > post_start
    ] if pd.notna(post_start) else samples.iloc[0:0]
    first_sample2 = (
        sample2_candidates["Time"].min()
        if not sample2_candidates.empty else pd.NaT
    )

    if pd.notna(first_sample):
        result.loc[result["Time"] < first_sample, "Phase classification"] = "Pre-pH blending"
    if pd.notna(first_sample) and pd.notna(first_adjust):
        result.loc[
            (result["Time"] >= first_sample) & (result["Time"] < first_adjust),
            "Phase classification",
        ] = "pH sample 1"
    if pd.notna(first_adjust) and pd.notna(post_start):
        result.loc[
            (result["Time"] >= first_adjust) & (result["Time"] < post_start),
            "Phase classification",
        ] = "pH adjustment"
    if pd.notna(post_start) and pd.notna(first_sample2):
        result.loc[
            (result["Time"] >= post_start) & (result["Time"] < first_sample2),
            "Phase classification",
        ] = "Post-pH blending"
    if pd.notna(first_sample2):
        result.loc[result["Time"] >= first_sample2, "Phase classification"] = "pH sample 2"

    # Mark the decisive transition rows used by the validator.
    for cycle_value, label in ((60, "Pre-pH cycle completion"), (100, "Post-pH cycle completion")):
        cycle_rows = result[
            result["Step"].str.startswith("blending to accumulator")
            & pd.to_numeric(result["Cycle"], errors="coerce").eq(cycle_value)
        ]
        if not cycle_rows.empty:
            last_cycle_time = cycle_rows["Time"].max()
            close_candidates = result[
                (result["Step"] == "close 2mm orifice")
                & (result["Time"] >= last_cycle_time)
            ]
            if not close_candidates.empty:
                close_index = close_candidates.index[0]
                result.loc[close_index, "Boundary role"] = label

    if pd.notna(first_adjust):
        first_adjust_index = result[
            (result["Step"] == "adjust ph") & (result["Time"] == first_adjust)
        ].index
        if len(first_adjust_index):
            result.loc[first_adjust_index[0], "Boundary role"] = "Adjust PH start"
    if pd.notna(last_adjust):
        last_adjust_index = result[
            (result["Step"] == "adjust ph") & (result["Time"] == last_adjust)
        ].index
        if len(last_adjust_index):
            result.loc[last_adjust_index[-1], "Boundary role"] = "Adjust PH end"

    return result.reset_index(drop=True)


def build_merged_datalog_preview(reports: list[dict[str, Any]]) -> pd.DataFrame:
    """Merge raw datalog records batch-wise while retaining source reports."""
    event_tables = []
    for report in reports:
        events = report.get("Blending Events", pd.DataFrame())
        if events.empty:
            continue
        events = events.copy()
        if "Batch" not in events.columns:
            events["Batch"] = report.get("Batch", "UNKNOWN")
        if "Source Report" not in events.columns:
            events["Source Report"] = report.get("Source File", "Unknown file")
        event_tables.append(events)

    if not event_tables:
        return classify_merged_datalog(pd.DataFrame())

    merged = pd.concat(event_tables, ignore_index=True)
    # Keep overlapping records in the preview so the user can verify report
    # overlap. combine_reports() still removes duplicates for calculations.
    return classify_merged_datalog(merged)


def styled_cycle_summary(summary: pd.DataFrame):
    """Apply the original stage-color theme to the cycle-time summary."""
    def row_style(row: pd.Series) -> list[str]:
        stage = str(row.get("Stage", ""))
        if stage in {"Seal dry run", "Leak test", "Vacuum test (Max)"}:
            color = "#f7b177"
        elif stage in {"Filling setup", "Filling"}:
            color = "#dfacad"
        else:
            color = "#adc3dc"
        return [f"background-color: {color}; color: #172033"] * len(row)
    return (
        summary.style
        .format({"Process time (hours)": "{:.3f}"}, na_rep="-")
        .apply(row_style, axis=1)
        .set_properties(subset=["Stage"], **{"font-weight": "bold"})
    )

def main() -> None:
    st.title("EB Execution Date Extractor")
    st.caption(f"Build: {APP_VERSION}")
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
                # Partial reports are expected. Blending validation is performed
                # only after all report parts for the batch have been merged.
                "Message": error_message,
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
    merged_datalog = build_merged_datalog_preview(reports)
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

    tabs = st.tabs([
        "Actual Process Time",
        "Merged Datalog Preview",
        "Summary",
        "Plots",
        "Extraction Log",
    ])
    with tabs[0]:
        st.dataframe(format_dates(batch_table), use_container_width=True, hide_index=True)

    with tabs[1]:
        st.subheader(f"{selected_batch} merged Blending Datalog")
        batch_events = merged_datalog[
            merged_datalog["Batch"].astype(str).eq(selected_batch)
        ].copy()
        if batch_events.empty:
            st.info("No Blending Datalog records were extracted for this batch.")
        else:
            source_options = sorted(batch_events["Source Report"].dropna().unique().tolist())
            phase_options = sorted(batch_events["Phase classification"].dropna().unique().tolist())
            filter_1, filter_2, filter_3 = st.columns(3)
            selected_sources = filter_1.multiselect(
                "Source report", source_options, default=source_options,
                key="datalog_sources",
            )
            selected_phases = filter_2.multiselect(
                "Phase", phase_options, default=phase_options,
                key="datalog_phases",
            )
            boundaries_only = filter_3.checkbox(
                "Show boundary records only", value=False,
                key="datalog_boundaries_only",
            )

            preview = batch_events[
                batch_events["Source Report"].isin(selected_sources)
                & batch_events["Phase classification"].isin(selected_phases)
            ].copy()
            if boundaries_only:
                preview = preview[preview["Boundary role"] != "Process record"]

            preview_display = preview.copy()
            preview_display["Time"] = pd.to_datetime(
                preview_display["Time"], errors="coerce"
            ).dt.strftime("%d/%m/%Y %H:%M:%S")
            preview_display = preview_display[
                [
                    "Batch", "Source Report", "Time", "Step", "Cycle", "Page",
                    "Phase classification", "Boundary role", "Duplicate status",
                ]
            ]
            st.caption(
                f"Showing {len(preview_display):,} of {len(batch_events):,} merged records. "
                "Overlap records are retained in this preview for traceability but "
                "deduplicated before duration calculations."
            )
            st.dataframe(
                preview_display,
                use_container_width=True,
                hide_index=True,
                height=600,
            )
            st.download_button(
                "Download merged datalog preview CSV",
                preview_display.to_csv(index=False).encode("utf-8"),
                file_name=f"{selected_batch}_merged_blending_datalog.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with tabs[2]:
        st.subheader("Cycle-Time Summary")
        if summary.empty:
            st.info("No validated cycle-time rows are available for this batch.")
        else:
            total_hours = float(summary["Process time (hours)"].sum())
            longest = summary.loc[summary["Process time (hours)"].idxmax()]
            c1, c2, c3 = st.columns(3)
            c1.metric("Total validated cycle time", f"{total_hours:.2f} h")
            c2.metric("Longest operation", str(longest["Stage"]))
            c3.metric("Longest duration", f"{float(longest['Process time (hours)']):.2f} h")
            st.dataframe(
                styled_cycle_summary(summary),
                use_container_width=True,
                hide_index=True,
            )

    with tabs[3]:
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

    with tabs[4]:
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
