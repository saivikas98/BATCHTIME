"""Streamlit user interface for the EB Execution Date Extractor."""

from __future__ import annotations

import io
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from parser import (
    REPORT_UNKNOWN,
    combine_reports,
    parse_pdf,
)


st.set_page_config(
    page_title="EB Execution Date Extractor",
    page_icon="📄",
    layout="wide",
)

MAX_DURATION_STAGES = {
    "Seal dry run",
    "Leak test",
    "Vacuum test (Max)",
    "Wetting",
    "Pre-mixing",
    "Mixing",
}


def format_datetime_columns(table: pd.DataFrame) -> pd.DataFrame:
    """Format datetime values for clear on-screen display."""
    formatted_table = table.copy()
    for column in ("Process Start", "Process End"):
        formatted_table[column] = pd.to_datetime(
            formatted_table[column],
            errors="coerce",
        ).dt.strftime("%d/%m/%Y %H:%M:%S")
    return formatted_table


def build_process_summary(batch_table: pd.DataFrame) -> pd.DataFrame:
    """Build the Process Time Summary using the workbook aggregation logic."""
    summary_rows: list[dict[str, Any]] = []

    for stage, stage_table in batch_table.groupby("Stage", sort=False):
        durations = pd.to_numeric(
            stage_table["Duration (hours)"],
            errors="coerce",
        ).dropna()

        if durations.empty:
            continue

        if stage in MAX_DURATION_STAGES:
            process_time = durations.max()
            calculation = "Maximum cycle duration"
        else:
            process_time = durations.sum()
            calculation = "Sum of sequential durations"

        summary_rows.append(
            {
                "Stage": stage,
                "Process time (hours)": round(float(process_time), 3),
                "Calculation": calculation,
            }
        )

    return pd.DataFrame(summary_rows)


def build_excel_output(
    actual_process_time: pd.DataFrame,
    process_summary: pd.DataFrame,
    extraction_log: pd.DataFrame,
) -> bytes:
    """Create the downloadable Excel workbook in memory."""
    output = io.BytesIO()

    with pd.ExcelWriter(
        output,
        engine="openpyxl",
        datetime_format="dd/mm/yyyy hh:mm:ss",
    ) as writer:
        actual_process_time.to_excel(
            writer,
            index=False,
            sheet_name="Actual Process Time",
        )
        process_summary.to_excel(
            writer,
            index=False,
            sheet_name="Process Time Summary",
        )
        extraction_log.to_excel(
            writer,
            index=False,
            sheet_name="Extraction Log",
        )

        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions

            for column_cells in worksheet.columns:
                maximum_length = max(
                    len(str(cell.value)) if cell.value is not None else 0
                    for cell in column_cells
                )
                column_letter = column_cells[0].column_letter
                worksheet.column_dimensions[column_letter].width = min(
                    maximum_length + 2,
                    45,
                )

    return output.getvalue()


def main() -> None:
    """Render and run the Streamlit application."""
    st.title("EB Execution Date Extractor")
    st.write(
        "Upload System Integrity, Batch Cycle, and PFS Filling PDF reports. "
        "The application detects the report type, groups files by batch, "
        "extracts execution dates, calculates process durations, and creates "
        "an Excel output matching the two-sheet reporting structure."
    )

    uploaded_files = st.file_uploader(
        "Upload PDF reports",
        type=["pdf"],
        accept_multiple_files=True,
        help="You may upload reports from one or more batches together.",
    )

    if not uploaded_files:
        st.info("Upload at least one PDF report to begin extraction.")
        return

    parsed_reports: list[dict[str, Any]] = []
    extraction_log_rows: list[dict[str, Any]] = []

    progress_bar = st.progress(0, text="Reading reports...")

    for file_index, uploaded_file in enumerate(uploaded_files, start=1):
        try:
            parsed_report = parse_pdf(uploaded_file, uploaded_file.name)
            parsed_reports.append(parsed_report)

            error_message = parsed_report.get("Error", "")
            extraction_log_rows.append(
                {
                    "File": uploaded_file.name,
                    "Detected report type": parsed_report["Report Type"],
                    "Batch": parsed_report["Batch"],
                    "Rows extracted": len(parsed_report["Rows"]),
                    "Status": "Unsupported" if error_message else "Success",
                    "Message": error_message,
                }
            )
        except Exception as error:
            extraction_log_rows.append(
                {
                    "File": uploaded_file.name,
                    "Detected report type": "Error",
                    "Batch": "UNKNOWN",
                    "Rows extracted": 0,
                    "Status": "Failed",
                    "Message": str(error),
                }
            )

        progress_bar.progress(
            file_index / len(uploaded_files),
            text=f"Processed {file_index} of {len(uploaded_files)} report(s)",
        )

    progress_bar.empty()
    extraction_log = pd.DataFrame(extraction_log_rows)

    st.subheader("Extraction Log")
    st.dataframe(
        extraction_log,
        use_container_width=True,
        hide_index=True,
    )

    unknown_reports = extraction_log[
        extraction_log["Detected report type"] == REPORT_UNKNOWN
    ]
    if not unknown_reports.empty:
        st.warning(
            "One or more PDFs could not be recognized. Check the Extraction "
            "Log before using the results."
        )

    combined_table = combine_reports(parsed_reports)
    if combined_table.empty:
        st.error("No execution-date records were extracted from the PDFs.")
        return

    available_batches = combined_table["Batch"].dropna().unique().tolist()
    selected_batch = st.selectbox("Select batch", available_batches)

    batch_table = combined_table[
        combined_table["Batch"] == selected_batch
    ].copy()
    process_summary = build_process_summary(batch_table)

    metric_columns = st.columns(4)
    metric_columns[0].metric("Batch", selected_batch)
    metric_columns[1].metric("Uploaded reports", len(uploaded_files))
    metric_columns[2].metric("Extracted records", len(batch_table))
    metric_columns[3].metric(
        "Validation issues",
        int((batch_table["Validation"] != "OK").sum()),
    )

    st.subheader("Actual Process Time")
    st.dataframe(
        format_datetime_columns(batch_table),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Process Time Summary")
    st.dataframe(
        process_summary,
        use_container_width=True,
        hide_index=True,
    )

    timeline_table = batch_table.dropna(
        subset=["Process Start", "Process End"]
    ).copy()
    if not timeline_table.empty:
        st.subheader("Process Timeline")
        timeline_figure = px.timeline(
            timeline_table,
            x_start="Process Start",
            x_end="Process End",
            y="Stage",
            color="Machine",
            hover_data=[
                "Duration (hours)",
                "Index",
                "Total",
                "Source",
                "Validation",
            ],
        )
        timeline_figure.update_yaxes(autorange="reversed")
        timeline_figure.update_layout(height=650)
        st.plotly_chart(timeline_figure, use_container_width=True)

    with st.expander("Validation and Traceability"):
        st.dataframe(
            format_datetime_columns(
                batch_table[
                    [
                        "Batch",
                        "Stage",
                        "Machine",
                        "Process Start",
                        "Process End",
                        "Source",
                        "Validation",
                    ]
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

    excel_output = build_excel_output(
        actual_process_time=combined_table,
        process_summary=process_summary.assign(Batch=selected_batch),
        extraction_log=extraction_log,
    )

    st.download_button(
        label="Download extracted Excel workbook",
        data=excel_output,
        file_name=f"{selected_batch}_execution_dates.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        use_container_width=True,
    )


if __name__ == "__main__":
    main()
