"""EB Execution Date Extractor and Process-Time Dashboard.

Place this file beside parser.py in the Streamlit repository root.
The parser module must expose parse_pdf() and combine_reports().
"""
from __future__ import annotations

import io
from collections import OrderedDict
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from parser import combine_reports, parse_pdf


st.set_page_config(
    page_title="EB Execution Date Extractor",
    page_icon="📊",
    layout="wide",
)

PROCESS_ROWS = [
    "Assembly - Manual",
    "SI elapsed - Seal dry run",
    "SI elapsed - Leak test",
    "SI elapsed - Vacuum test",
    "SI actual - Seal dry run",
    "SI actual - Leak test",
    "SI actual - Vacuum test",
    "Water charging",
    "API charging",
    "Wetting",
    "Pre-mixing",
    "Mixing",
    "Pre-pH Blending",
    "pH sample 1",
    "pH adjustment",
    "Post-pH blending",
    "pH sample 2",
    "Filling setup",
    "Filling",
]

STAGE_GROUPS = OrderedDict(
    [
        ("Assembly - Manual", "Assembly"),
        ("SI actual - Seal dry run", "System integrity"),
        ("SI actual - Leak test", "System integrity"),
        ("SI actual - Vacuum test", "System integrity"),
        ("Water charging", "Gel manufacturing"),
        ("API charging", "Gel manufacturing"),
        ("Wetting", "Gel manufacturing"),
        ("Pre-mixing", "Gel manufacturing"),
        ("Mixing", "Gel manufacturing"),
        ("Pre-pH Blending", "Gel manufacturing"),
        ("pH sample 1", "Gel manufacturing"),
        ("pH adjustment", "Gel manufacturing"),
        ("Post-pH blending", "Gel manufacturing"),
        ("pH sample 2", "Gel manufacturing"),
        ("Filling setup", "Filling"),
        ("Filling", "Filling"),
    ]
)

TOTAL_STAGE_COLUMNS = [
    "Assembly - Manual",
    "System integrity",
    "Gel manufacturing",
    "Filling",
]

GROUP_COLORS = {
    "Assembly - Manual": "#b9b28d",
    "System integrity": "#fb923c",
    "Gel manufacturing": "#9db7d5",
    "Filling": "#d9a3a5",
}


def _format_dates(table: pd.DataFrame) -> pd.DataFrame:
    """Return a display copy with report-style date formatting."""
    result = table.copy()
    for column in ("Process Start", "Process End"):
        if column in result.columns:
            result[column] = pd.to_datetime(
                result[column], errors="coerce"
            ).dt.strftime("%d/%m/%Y %H:%M:%S")
    return result


def _elapsed_hours(group: pd.DataFrame) -> float | None:
    """Calculate earliest start to latest end for a group of executions."""
    if group.empty:
        return None
    start = pd.to_datetime(group["Process Start"], errors="coerce").min()
    end = pd.to_datetime(group["Process End"], errors="coerce").max()
    if pd.isna(start) or pd.isna(end):
        return None
    return round((end - start).total_seconds() / 3600, 2)


def _maximum_duration(group: pd.DataFrame) -> float | None:
    """Return the longest valid cycle duration in a group."""
    if group.empty:
        return None
    durations = pd.to_numeric(
        group["Duration (hours)"], errors="coerce"
    ).dropna()
    return round(float(durations.max()), 2) if not durations.empty else None


def _sum_duration(group: pd.DataFrame) -> float | None:
    """Return the sum of valid sequential execution durations."""
    if group.empty:
        return None
    durations = pd.to_numeric(
        group["Duration (hours)"], errors="coerce"
    ).dropna()
    return round(float(durations.sum()), 2) if not durations.empty else None


def _matching_rows(
    table: pd.DataFrame,
    names: list[str],
) -> pd.DataFrame:
    """Select rows matching any stage alias, case-insensitively."""
    normalised_names = {name.strip().casefold() for name in names}
    stage_text = table["Stage"].astype(str).str.strip().str.casefold()
    return table[stage_text.isin(normalised_names)]


def calculate_batch_values(
    batch_table: pd.DataFrame,
    manual_assembly_hours: float,
) -> dict[str, float | None]:
    """Calculate all summary rows for one batch."""
    values: dict[str, float | None] = {
        process: None for process in PROCESS_ROWS
    }
    values["Assembly - Manual"] = round(float(manual_assembly_hours), 2)

    integrity_mapping = {
        "Seal dry run": ["Seal dry run", "Dryrun"],
        "Leak test": ["Leak test"],
        "Vacuum test": ["Vacuum test", "Vacuum test (Max)"],
    }
    for display_name, source_names in integrity_mapping.items():
        group = _matching_rows(batch_table, source_names)
        values[f"SI elapsed - {display_name}"] = _elapsed_hours(group)
        values[f"SI actual - {display_name}"] = _maximum_duration(group)

    # These extruder operations run in parallel. The batch duration is the
    # maximum duration among Extruders 1, 2, and 3.
    for stage in ("Wetting", "Pre-mixing", "Mixing"):
        values[stage] = _maximum_duration(_matching_rows(batch_table, [stage]))

    # These rows represent sequential durations in the summary.
    for stage in ("Water charging", "API charging", "Filling setup", "Filling"):
        values[stage] = _sum_duration(_matching_rows(batch_table, [stage]))

    values["Pre-pH Blending"] = _sum_duration(
        _matching_rows(
            batch_table,
            ["Pre-pH Blending", "Pre-pH blending", "Pre PH Blending"],
        )
    )
    values["pH adjustment"] = _sum_duration(
        _matching_rows(batch_table, ["pH adjustment", "PH adjustment", "Adjust PH"])
    )
    values["Post-pH blending"] = _sum_duration(
        _matching_rows(
            batch_table,
            ["Post-pH blending", "Post-pH Blending", "Post PH Blending"],
        )
    )

    ph_samples = _matching_rows(
        batch_table,
        ["pH sample", "PH sample", "Sample: IPS PH"],
    ).sort_values("Process Start")
    sample_durations = pd.to_numeric(
        ph_samples["Duration (hours)"], errors="coerce"
    ).dropna().tolist()
    if sample_durations:
        values["pH sample 1"] = round(float(sample_durations[0]), 2)
        if len(sample_durations) > 1:
            values["pH sample 2"] = round(float(sample_durations[-1]), 2)

    # Backward compatibility for parser versions that expose one overall
    # Blending row instead of separate pH-phase rows.
    if values["Pre-pH Blending"] is None:
        values["Pre-pH Blending"] = _sum_duration(
            _matching_rows(batch_table, ["Blending"])
        )

    return values


def build_summary_tables(
    actual: pd.DataFrame,
    manual_assembly_hours: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the activity matrix and total-process-time table."""
    batches = actual["Batch"].dropna().astype(str).drop_duplicates().tolist()
    batch_values = {
        batch: calculate_batch_values(
            actual[actual["Batch"].astype(str) == batch],
            manual_assembly_hours,
        )
        for batch in batches
    }

    matrix = pd.DataFrame(batch_values).reindex(PROCESS_ROWS)
    matrix.index.name = "Stage"
    matrix["Average"] = matrix[batches].mean(axis=1, skipna=True).round(2)

    categories = []
    for stage in matrix.index:
        if stage == "Assembly - Manual":
            categories.append("Assembly")
        elif stage.startswith("SI elapsed"):
            categories.append("System integrity - elapsed")
        elif stage.startswith("SI actual"):
            categories.append("System integrity - actual")
        elif stage in {"Filling setup", "Filling"}:
            categories.append("Filling")
        else:
            categories.append("Gel manufacturing")
    matrix.insert(0, "Category", categories)

    totals = pd.DataFrame(index=TOTAL_STAGE_COLUMNS, columns=batches, dtype=float)
    for batch in batches:
        values = batch_values[batch]
        totals.loc["Assembly - Manual", batch] = values["Assembly - Manual"] or 0.0
        totals.loc["System integrity", batch] = sum(
            (values[stage] or 0.0)
            for stage, group in STAGE_GROUPS.items()
            if group == "System integrity"
        )
        totals.loc["Gel manufacturing", batch] = sum(
            (values[stage] or 0.0)
            for stage, group in STAGE_GROUPS.items()
            if group == "Gel manufacturing"
        )
        totals.loc["Filling", batch] = sum(
            (values[stage] or 0.0)
            for stage, group in STAGE_GROUPS.items()
            if group == "Filling"
        )

    totals.loc["Total time"] = totals.sum(axis=0)
    totals["Average"] = totals[batches].mean(axis=1, skipna=True).round(2)
    totals.index.name = "Stage"

    # Important: keep attrs empty. A DataFrame stored inside attrs causes
    # pandas melt/concat to raise an ambiguous truth-value ValueError.
    matrix.attrs.clear()
    totals.attrs.clear()
    return matrix.round(2), totals.round(2)


def style_process_matrix(matrix: pd.DataFrame) -> pd.io.formats.style.Styler:
    """Apply reference-style colors to the process summary."""
    display = matrix.reset_index()
    numeric_columns = [
        column
        for column in display.columns
        if column not in {"Stage", "Category"}
    ]

    def color_row(row: pd.Series) -> list[str]:
        category = str(row["Category"])
        if category == "Assembly":
            color = "#c8c19b"
        elif category.startswith("System integrity"):
            color = "#f7b177"
        elif category == "Filling":
            color = "#dfacad"
        else:
            color = "#adc3dc"
        return [f"background-color: {color}"] * len(row)

    return (
        display.style
        .format({column: "{:.2f}" for column in numeric_columns}, na_rep="-")
        .apply(color_row, axis=1)
        .set_properties(subset=["Stage"], **{"font-weight": "bold"})
        .set_properties(subset=["Average"], **{"font-weight": "bold"})
    )


def build_stacked_plot_table(
    totals: pd.DataFrame,
    batches: list[str],
    strengths: dict[str, str],
) -> pd.DataFrame:
    """Create a clean, metadata-free long table for the stacked bar plot."""
    stage_totals = totals.loc[TOTAL_STAGE_COLUMNS, batches].copy()
    stage_totals.attrs.clear()

    total_long = pd.DataFrame(
        data=stage_totals.T.to_numpy(copy=True),
        index=batches,
        columns=TOTAL_STAGE_COLUMNS,
    )
    total_long.index.name = "Batch"
    total_long = total_long.reset_index()
    total_long["Strength"] = (
        total_long["Batch"].map(strengths).fillna("Not set")
    )
    total_long.attrs.clear()

    return pd.melt(
        frame=total_long,
        id_vars=["Batch", "Strength"],
        value_vars=TOTAL_STAGE_COLUMNS,
        var_name="Stage",
        value_name="Hours",
        ignore_index=True,
    )


def build_excel_output(
    actual: pd.DataFrame,
    matrix: pd.DataFrame,
    totals: pd.DataFrame,
    extraction_log: pd.DataFrame,
    strengths: dict[str, str],
) -> bytes:
    """Create a formatted Excel workbook in memory."""
    output = io.BytesIO()
    batches = actual["Batch"].dropna().astype(str).drop_duplicates().tolist()

    matrix_export = matrix.reset_index()
    totals_export = totals.reset_index()
    strength_export = pd.DataFrame(
        {
            "Batch": batches,
            "Strength": [strengths.get(batch, "Not set") for batch in batches],
        }
    )

    with pd.ExcelWriter(
        output,
        engine="openpyxl",
        datetime_format="dd/mm/yyyy hh:mm:ss",
    ) as writer:
        actual.to_excel(writer, index=False, sheet_name="Actual Process Time")
        matrix_export.to_excel(
            writer, index=False, sheet_name="Process Time Summary", startrow=2
        )
        totals_export.to_excel(
            writer, index=False, sheet_name="Total Process Time", startrow=2
        )
        strength_export.to_excel(writer, index=False, sheet_name="Batch Strength")
        extraction_log.to_excel(writer, index=False, sheet_name="Extraction Log")

        thin = Side(style="thin", color="000000")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        header_fill = PatternFill("solid", fgColor="D9EAF7")

        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A4" if "Process Time" in worksheet.title else "A2"
            for row in worksheet.iter_rows():
                for cell in row:
                    cell.border = border
                    cell.alignment = Alignment(vertical="center")
            for column_cells in worksheet.columns:
                column_letter = column_cells[0].column_letter
                maximum_length = max(
                    len(str(cell.value)) if cell.value is not None else 0
                    for cell in column_cells
                )
                worksheet.column_dimensions[column_letter].width = min(
                    maximum_length + 2, 42
                )

        for sheet_name, title in (
            ("Process Time Summary", "Process time (hours)"),
            ("Total Process Time", "Total process time (hours)"),
        ):
            worksheet = writer.book[sheet_name]
            worksheet["A1"] = title
            worksheet["A1"].font = Font(bold=True, size=14)
            worksheet["A3"].fill = header_fill

    return output.getvalue()


def main() -> None:
    """Run the Streamlit application."""
    st.title("EB Execution Date Extractor and Process-Time Dashboard")
    st.caption(
        "Upload System Integrity, Batch Cycle, and PFS Filling reports for "
        "one or more batches."
    )

    uploaded_files = st.file_uploader(
        "Upload PDF reports",
        type=["pdf"],
        accept_multiple_files=True,
    )
    if not uploaded_files:
        st.info(
            "Upload the available reports for each batch to generate the "
            "summary and interactive plots."
        )
        return

    parsed_reports: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []
    progress = st.progress(0, text="Reading reports...")

    for position, uploaded_file in enumerate(uploaded_files, start=1):
        try:
            report = parse_pdf(uploaded_file, uploaded_file.name)
            parsed_reports.append(report)
            message = report.get("Error", "")
            log_rows.append(
                {
                    "File": uploaded_file.name,
                    "Report type": report["Report Type"],
                    "Batch": report["Batch"],
                    "Rows": len(report["Rows"]),
                    "Status": "Unsupported" if message else "Success",
                    "Message": message,
                }
            )
        except Exception as error:
            log_rows.append(
                {
                    "File": uploaded_file.name,
                    "Report type": "Error",
                    "Batch": "UNKNOWN",
                    "Rows": 0,
                    "Status": "Failed",
                    "Message": str(error),
                }
            )

        progress.progress(
            position / len(uploaded_files),
            text=f"Processed {position} of {len(uploaded_files)} report(s)",
        )
    progress.empty()

    extraction_log = pd.DataFrame(log_rows)
    actual = combine_reports(parsed_reports)

    if actual.empty:
        st.error("No execution-date rows were extracted.")
        st.dataframe(extraction_log, use_container_width=True, hide_index=True)
        return

    batches = actual["Batch"].dropna().astype(str).drop_duplicates().tolist()

    with st.sidebar:
        st.header("Summary settings")
        manual_assembly_hours = st.number_input(
            "Manual assembly time (hours)",
            min_value=0.0,
            max_value=100.0,
            value=16.0,
            step=0.5,
        )
        st.caption("Assign each batch to the applicable product strength.")
        strengths = {
            batch: st.selectbox(
                f"{batch} strength",
                ["120 mg", "90 mg", "60 mg", "Not set"],
                index=3,
                key=f"strength_{batch}",
            )
            for batch in batches
        }

    matrix, totals = build_summary_tables(actual, manual_assembly_hours)

    summary_tab, plots_tab, actual_tab, log_tab = st.tabs(
        ["Executive Summary", "Plots", "Actual Process Time", "Extraction Log"]
    )

    with summary_tab:
        st.subheader("Process Time Summary")
        st.caption(
            "Elapsed system-integrity time uses earliest start to latest end. "
            "Actual system-integrity time uses the maximum machine cycle. "
            "Wetting, Pre-mixing, and Mixing use the maximum extruder duration."
        )
        st.dataframe(
            style_process_matrix(matrix),
            use_container_width=True,
            hide_index=True,
            height=735,
        )

        st.subheader("Total Process Time")
        total_display = totals.reset_index()
        total_numeric_columns = [
            column for column in total_display.columns if column != "Stage"
        ]
        st.dataframe(
            total_display.style.format(
                {column: "{:.2f}" for column in total_numeric_columns},
                na_rep="-",
            ),
            use_container_width=True,
            hide_index=True,
        )

        overall = pd.to_numeric(
            totals.loc["Total time", batches], errors="coerce"
        ).dropna()
        if not overall.empty:
            metric_1, metric_2, metric_3, metric_4 = st.columns(4)
            metric_1.metric("Average total", f"{overall.mean():.2f} h")
            metric_2.metric(
                "Minimum batch",
                f"{overall.min():.2f} h",
                str(overall.idxmin()),
            )
            metric_3.metric(
                "Maximum batch",
                f"{overall.max():.2f} h",
                str(overall.idxmax()),
            )
            metric_4.metric(
                "Batch range",
                f"{overall.max() - overall.min():.2f} h",
            )

    with plots_tab:
        st.subheader("Stage Contribution to Total Processing Time")
        stacked = build_stacked_plot_table(totals, batches, strengths)
        stacked_figure = px.bar(
            stacked,
            x="Batch",
            y="Hours",
            color="Stage",
            barmode="stack",
            color_discrete_map=GROUP_COLORS,
            text_auto=".1f",
            hover_data=["Strength"],
            title="Batch total process time by major stage",
        )
        stacked_figure.update_layout(legend_title_text="Major stage")
        st.plotly_chart(stacked_figure, use_container_width=True)

        left_column, right_column = st.columns(2)

        with left_column:
            activity_averages = matrix["Average"].dropna().reset_index()
            activity_averages = activity_averages[
                ~activity_averages["Stage"].str.startswith("SI elapsed")
            ]
            average_figure = px.bar(
                activity_averages,
                x="Average",
                y="Stage",
                orientation="h",
                color="Average",
                color_continuous_scale="Blues",
                title="Average activity duration",
                labels={"Average": "Hours"},
            )
            average_figure.update_yaxes(categoryorder="total ascending")
            st.plotly_chart(average_figure, use_container_width=True)

        with right_column:
            heatmap_data = matrix[batches].copy()
            heatmap_data.attrs.clear()
            heatmap_data = heatmap_data.loc[
                heatmap_data.notna().any(axis=1)
            ]
            heatmap_figure = px.imshow(
                heatmap_data,
                text_auto=".1f",
                aspect="auto",
                color_continuous_scale="RdYlBu_r",
                labels={
                    "x": "Batch",
                    "y": "Activity",
                    "color": "Hours",
                },
                title="Batch-to-batch process-time variation",
            )
            st.plotly_chart(heatmap_figure, use_container_width=True)

        trend = pd.DataFrame(
            {
                "Batch": batches,
                "Total hours": totals.loc["Total time", batches].to_numpy(
                    copy=True
                ),
                "Strength": [strengths.get(batch, "Not set") for batch in batches],
            }
        )
        trend.attrs.clear()
        trend_figure = px.line(
            trend,
            x="Batch",
            y="Total hours",
            color="Strength",
            markers=True,
            title="Overall process time by batch and strength",
        )
        trend_average = pd.to_numeric(
            trend["Total hours"], errors="coerce"
        ).mean()
        if pd.notna(trend_average):
            trend_figure.add_hline(
                y=float(trend_average),
                line_dash="dash",
                annotation_text=f"Average {trend_average:.2f} h",
            )
        st.plotly_chart(trend_figure, use_container_width=True)

    with actual_tab:
        selected_batch = st.selectbox(
            "Select batch",
            batches,
            key="actual_batch_selector",
        )
        selected = actual[actual["Batch"].astype(str) == selected_batch].copy()
        st.dataframe(
            _format_dates(selected),
            use_container_width=True,
            hide_index=True,
        )

        timeline = selected.dropna(
            subset=["Process Start", "Process End"]
        ).copy()
        if not timeline.empty:
            timeline.attrs.clear()
            timeline_figure = px.timeline(
                timeline,
                x_start="Process Start",
                x_end="Process End",
                y="Stage",
                color="Machine",
                hover_data=["Duration (hours)", "Source", "Validation"],
                title=f"{selected_batch} execution timeline",
            )
            timeline_figure.update_yaxes(autorange="reversed")
            st.plotly_chart(timeline_figure, use_container_width=True)

    with log_tab:
        st.dataframe(
            extraction_log,
            use_container_width=True,
            hide_index=True,
        )

    excel_output = build_excel_output(
        actual=actual,
        matrix=matrix,
        totals=totals,
        extraction_log=extraction_log,
        strengths=strengths,
    )
    st.download_button(
        "Download complete summary workbook",
        data=excel_output,
        file_name="EB_execution_dates_and_process_summary.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )


if __name__ == "__main__":
    main()
