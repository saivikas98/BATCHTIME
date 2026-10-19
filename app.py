"""Streamlit interface for EB execution-date extraction and process-time analytics."""
from __future__ import annotations

import io
from collections import OrderedDict
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from parser import UNKNOWN, combine_reports, parse_pdf

st.set_page_config(page_title="EB Execution Date Extractor", page_icon="📊", layout="wide")

DISPLAY_ORDER = [
    "Assembly - Manual",
    "SI elapsed - Seal dry run", "SI elapsed - Leak test", "SI elapsed - Vacuum test",
    "SI actual - Seal dry run", "SI actual - Leak test", "SI actual - Vacuum test",
    "Water charging", "API charging", "Wetting", "Pre-mixing", "Mixing",
    "Pre-pH Blending", "pH sample 1", "pH adjustment", "Post-pH blending",
    "pH sample 2", "Filling setup", "Filling",
]

STAGE_GROUPS = OrderedDict([
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
])

COLORS = {
    "Assembly": "#b9b28d",
    "System integrity": "#fb923c",
    "Gel manufacturing": "#9db7d5",
    "Filling": "#d9a3a5",
}


def display_dates(table: pd.DataFrame) -> pd.DataFrame:
    result = table.copy()
    for column in ("Process Start", "Process End"):
        result[column] = pd.to_datetime(result[column], errors="coerce").dt.strftime(
            "%d/%m/%Y %H:%M:%S"
        )
    return result


def elapsed_hours(group: pd.DataFrame) -> float | None:
    start = pd.to_datetime(group["Process Start"], errors="coerce").min()
    end = pd.to_datetime(group["Process End"], errors="coerce").max()
    if pd.isna(start) or pd.isna(end):
        return None
    return round((end - start).total_seconds() / 3600, 2)


def max_duration(group: pd.DataFrame) -> float | None:
    values = pd.to_numeric(group["Duration (hours)"], errors="coerce").dropna()
    return round(float(values.max()), 2) if not values.empty else None


def sum_duration(group: pd.DataFrame) -> float | None:
    values = pd.to_numeric(group["Duration (hours)"], errors="coerce").dropna()
    return round(float(values.sum()), 2) if not values.empty else None


def select_occurrence(table: pd.DataFrame, stage_name: str, occurrence: int) -> float | None:
    values = table.loc[table["Stage"] == stage_name, "Duration (hours)"]
    values = pd.to_numeric(values, errors="coerce").dropna().tolist()
    if not values:
        return None
    return round(float(values[min(occurrence, len(values) - 1)]), 2)


def batch_process_values(batch_table: pd.DataFrame, manual_assembly: float) -> dict[str, float | None]:
    """Calculate one batch column using the same logic as the reference summary."""
    values: dict[str, float | None] = {name: None for name in DISPLAY_ORDER}
    values["Assembly - Manual"] = round(manual_assembly, 2)

    integrity_map = {
        "Seal dry run": "Seal dry run",
        "Leak test": "Leak test",
        "Vacuum test": "Vacuum test (Max)",
    }
    for label, raw_stage in integrity_map.items():
        group = batch_table[batch_table["Stage"] == raw_stage]
        if not group.empty:
            values[f"SI elapsed - {label}"] = elapsed_hours(group)
            values[f"SI actual - {label}"] = max_duration(group)

    for stage in ("Wetting", "Pre-mixing", "Mixing"):
        group = batch_table[batch_table["Stage"] == stage]
        if not group.empty:
            values[stage] = max_duration(group)

    for stage in ("Water charging", "API charging", "Filling setup", "Filling"):
        group = batch_table[batch_table["Stage"] == stage]
        if not group.empty:
            values[stage] = sum_duration(group)

    # Accept both current parser labels and future detailed blending labels.
    alias_map = {
        "Pre-pH Blending": ["Pre-pH Blending", "Pre-pH blending"],
        "pH adjustment": ["pH adjustment", "PH adjustment"],
        "Post-pH blending": ["Post-pH blending", "Post-pH Blending"],
    }
    for output_name, aliases in alias_map.items():
        group = batch_table[batch_table["Stage"].isin(aliases)]
        if not group.empty:
            values[output_name] = sum_duration(group)

    ph_group = batch_table[batch_table["Stage"].str.lower() == "ph sample"]
    if not ph_group.empty:
        ordered = ph_group.sort_values("Process Start")
        durations = pd.to_numeric(ordered["Duration (hours)"], errors="coerce").dropna().tolist()
        if durations:
            values["pH sample 1"] = round(float(durations[0]), 2)
            values["pH sample 2"] = round(float(durations[-1]), 2) if len(durations) > 1 else None

    # If the batch parser still reports a single overall Blending record, retain it visibly.
    blending = batch_table[batch_table["Stage"] == "Blending"]
    if not blending.empty and values["Pre-pH Blending"] is None:
        values["Pre-pH Blending"] = sum_duration(blending)

    return values


def build_summary_matrix(
    actual: pd.DataFrame,
    strengths: dict[str, str],
    manual_assembly: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    batches = actual["Batch"].dropna().drop_duplicates().tolist()
    values_by_batch = {
        batch: batch_process_values(actual[actual["Batch"] == batch], manual_assembly)
        for batch in batches
    }

    matrix = pd.DataFrame(values_by_batch).reindex(DISPLAY_ORDER)
    matrix.insert(0, "Category", [
        "Assembly" if stage == "Assembly - Manual" else
        "System integrity - elapsed" if stage.startswith("SI elapsed") else
        "System integrity - actual" if stage.startswith("SI actual") else
        "Filling" if stage in {"Filling setup", "Filling"} else
        "Gel manufacturing"
        for stage in matrix.index
    ])
    matrix.index.name = "Stage"
    matrix["Average"] = matrix[batches].mean(axis=1, skipna=True).round(2)

    totals = pd.DataFrame(index=["Assembly - Manual", "System integrity", "Gel manufacturing", "Filling"])
    for batch in batches:
        batch_values = values_by_batch[batch]
        totals.loc["Assembly - Manual", batch] = batch_values["Assembly - Manual"]
        totals.loc["System integrity", batch] = sum(
            batch_values[name] or 0 for name in STAGE_GROUPS
            if STAGE_GROUPS[name] == "System integrity"
        )
        totals.loc["Gel manufacturing", batch] = sum(
            batch_values[name] or 0 for name in STAGE_GROUPS
            if STAGE_GROUPS[name] == "Gel manufacturing"
        )
        totals.loc["Filling", batch] = sum(
            batch_values[name] or 0 for name in STAGE_GROUPS
            if STAGE_GROUPS[name] == "Filling"
        )
    totals.loc["Total time"] = totals.sum(axis=0)
    totals["Average"] = totals[batches].mean(axis=1).round(2)
    totals.index.name = "Stage"

    strength_row = pd.DataFrame(
        [[strengths.get(batch, "Not set") for batch in batches] + ["All batches"]],
        columns=[*batches, "Average"], index=["Strength"],
    )
    matrix.attrs["strength_row"] = strength_row
    totals.attrs["strength_row"] = strength_row
    return matrix.round(2), totals.round(2)


def render_summary_table(matrix: pd.DataFrame) -> None:
    display = matrix.reset_index()
    numeric_columns = [c for c in display.columns if c not in {"Stage", "Category"}]
    st.dataframe(
        display.style.format({c: "{:.2f}" for c in numeric_columns}, na_rep="-").apply(
            lambda row: [
                f"background-color: {COLORS.get('Assembly', '#fff')}" if row["Category"] == "Assembly" else
                "background-color: #f7b177" if row["Category"].startswith("System integrity") else
                f"background-color: {COLORS['Gel manufacturing']}" if row["Category"] == "Gel manufacturing" else
                f"background-color: {COLORS['Filling']}"
            ] * len(row), axis=1
        ),
        use_container_width=True,
        hide_index=True,
        height=735,
    )


def build_excel(
    actual: pd.DataFrame,
    matrix: pd.DataFrame,
    totals: pd.DataFrame,
    extraction_log: pd.DataFrame,
) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl", datetime_format="dd/mm/yyyy hh:mm:ss") as writer:
        actual.to_excel(writer, index=False, sheet_name="Actual Process Time")
        matrix.reset_index().to_excel(writer, index=False, sheet_name="Process Time Summary", startrow=2)
        totals.reset_index().to_excel(writer, index=False, sheet_name="Total Process Time", startrow=2)
        extraction_log.to_excel(writer, index=False, sheet_name="Extraction Log")

        thin = Side(style="thin", color="000000")
        fills = {
            "Assembly": PatternFill("solid", fgColor="B9B28D"),
            "System integrity": PatternFill("solid", fgColor="FB923C"),
            "Gel manufacturing": PatternFill("solid", fgColor="9DB7D5"),
            "Filling": PatternFill("solid", fgColor="D9A3A5"),
        }
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A4" if "Time" in ws.title else "A2"
            for row in ws.iter_rows():
                for cell in row:
                    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
                    cell.alignment = Alignment(vertical="center")
            for column in ws.columns:
                letter = column[0].column_letter
                ws.column_dimensions[letter].width = min(
                    max(len(str(cell.value or "")) for cell in column) + 2, 38
                )
        writer.book["Process Time Summary"]["A1"] = "Process time (hours)"
        writer.book["Total Process Time"]["A1"] = "Total process time (hours)"
        for title in ("Process Time Summary", "Total Process Time"):
            ws = writer.book[title]
            ws["A1"].font = Font(bold=True, size=14)

    return output.getvalue()


def main() -> None:
    st.title("EB Execution Date Extractor and Process-Time Dashboard")
    st.caption("Upload System Integrity, Batch Cycle, and PFS Filling reports for one or more batches.")

    files = st.file_uploader("PDF reports", type=["pdf"], accept_multiple_files=True)
    if not files:
        st.info("Upload the three report types for each batch to generate the complete summary and plots.")
        return

    reports, log_rows = [], []
    progress = st.progress(0, text="Reading reports...")
    for number, uploaded in enumerate(files, 1):
        try:
            report = parse_pdf(uploaded, uploaded.name)
            reports.append(report)
            message = report.get("Error", "")
            log_rows.append({
                "File": uploaded.name, "Report type": report["Report Type"],
                "Batch": report["Batch"], "Rows": len(report["Rows"]),
                "Status": "Unsupported" if message else "Success", "Message": message,
            })
        except Exception as error:
            log_rows.append({
                "File": uploaded.name, "Report type": "Error", "Batch": "UNKNOWN",
                "Rows": 0, "Status": "Failed", "Message": str(error),
            })
        progress.progress(number / len(files), text=f"Processed {number} of {len(files)}")
    progress.empty()

    extraction_log = pd.DataFrame(log_rows)
    actual = combine_reports(reports)
    if actual.empty:
        st.error("No execution-date rows were extracted. Review the Extraction Log below.")
        st.dataframe(extraction_log, use_container_width=True, hide_index=True)
        return

    batches = actual["Batch"].dropna().drop_duplicates().tolist()
    with st.sidebar:
        st.header("Summary settings")
        manual_assembly = st.number_input("Manual assembly time (hours)", 0.0, 100.0, 16.0, 0.5)
        st.caption("Assign each uploaded batch to its product strength.")
        strengths = {
            batch: st.selectbox(
                f"{batch} strength", ["120 mg", "90 mg", "60 mg", "Not set"],
                index=3, key=f"strength_{batch}",
            ) for batch in batches
        }

    matrix, totals = build_summary_matrix(actual, strengths, manual_assembly)

    tabs = st.tabs([
        "Executive Summary", "Plots", "Actual Process Time", "Extraction Log",
    ])

    with tabs[0]:
        st.subheader("Process Time Summary")
        st.caption("System-integrity elapsed rows use earliest start to latest end. Actual rows use the maximum machine cycle duration. Parallel Wetting, Pre-mixing, and Mixing use the maximum extruder duration.")
        render_summary_table(matrix)
        st.subheader("Total Process Time")
        st.dataframe(
            totals.reset_index().style.format(
                {c: "{:.2f}" for c in totals.columns}, na_rep="-"
            ),
            use_container_width=True, hide_index=True,
        )

        overall = totals.loc["Total time", batches]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Average total", f"{overall.mean():.2f} h")
        c2.metric("Minimum batch", f"{overall.min():.2f} h", overall.idxmin())
        c3.metric("Maximum batch", f"{overall.max():.2f} h", overall.idxmax())
        c4.metric("Batch range", f"{overall.max() - overall.min():.2f} h")

    with tabs[1]:
        st.subheader("Batch total process time")
        total_long = totals.drop(index="Total time")[batches].T.reset_index().rename(columns={"index": "Batch"})
        total_long["Strength"] = total_long["Batch"].map(strengths)
        stacked = total_long.melt(id_vars=["Batch", "Strength"], var_name="Stage", value_name="Hours")
        fig = px.bar(
            stacked, x="Batch", y="Hours", color="Stage", barmode="stack",
            color_discrete_map=COLORS, text_auto=".1f", hover_data=["Strength"],
            title="Stage contribution to total processing time",
        )
        st.plotly_chart(fig, use_container_width=True)

        left, right = st.columns(2)
        with left:
            st.subheader("Average process time by activity")
            averages = matrix["Average"].dropna().reset_index()
            averages = averages[~averages["Stage"].str.startswith("SI elapsed")]
            avg_fig = px.bar(
                averages, x="Average", y="Stage", orientation="h",
                color="Average", color_continuous_scale="Blues",
                title="Average activity duration",
            )
            avg_fig.update_yaxes(categoryorder="total ascending")
            st.plotly_chart(avg_fig, use_container_width=True)

        with right:
            st.subheader("Process-time heatmap")
            heat = matrix[batches].copy()
            heat = heat.loc[heat.notna().any(axis=1)]
            heat_fig = px.imshow(
                heat, text_auto=".1f", aspect="auto", color_continuous_scale="RdYlBu_r",
                labels={"x": "Batch", "y": "Activity", "color": "Hours"},
                title="Batch-to-batch duration variation",
            )
            st.plotly_chart(heat_fig, use_container_width=True)

        st.subheader("Total-time trend")
        trend = pd.DataFrame({
            "Batch": batches,
            "Total hours": totals.loc["Total time", batches].values,
            "Strength": [strengths.get(batch, "Not set") for batch in batches],
        })
        trend_fig = px.line(
            trend, x="Batch", y="Total hours", color="Strength", markers=True,
            title="Overall processing time by batch and strength",
        )
        trend_fig.add_hline(
            y=trend["Total hours"].mean(), line_dash="dash",
            annotation_text=f"Average {trend['Total hours'].mean():.2f} h",
        )
        st.plotly_chart(trend_fig, use_container_width=True)

    with tabs[2]:
        selected_batch = st.selectbox("Select batch", batches)
        selected = actual[actual["Batch"] == selected_batch]
        st.dataframe(display_dates(selected), use_container_width=True, hide_index=True)
        timeline = selected.dropna(subset=["Process Start", "Process End"])
        if not timeline.empty:
            timeline_fig = px.timeline(
                timeline, x_start="Process Start", x_end="Process End",
                y="Stage", color="Machine", hover_data=["Duration (hours)", "Source"],
            )
            timeline_fig.update_yaxes(autorange="reversed")
            st.plotly_chart(timeline_fig, use_container_width=True)

    with tabs[3]:
        st.dataframe(extraction_log, use_container_width=True, hide_index=True)

    st.download_button(
        "Download complete summary workbook",
        data=build_excel(actual, matrix, totals, extraction_log),
        file_name="EB_execution_dates_and_process_summary.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )


if __name__ == "__main__":
    main()
