"""EB Execution Date Extractor with multi-report batch stitching."""
from __future__ import annotations

import io
import hashlib
import gc
from collections import OrderedDict
from typing import Any

import streamlit as st

st.set_page_config(page_title="EB Execution Date Extractor", page_icon="📊", layout="wide")

APP_VERSION = "2026.10.22-fast-launch-v9"

st.markdown(
    """
    <style>
    .stApp {background-color: #f5f7fb;}
    div[data-testid="stMetric"] {background:#ffffff; border:1px solid #d7deea; padding:14px; border-radius:10px;}
    div[data-testid="stDataFrame"] {background:#ffffff; border:1px solid #d7deea; border-radius:10px; padding:4px;}
    .block-container {padding-top: 1.5rem; max-width: 1500px;}
    h1, h2, h3 {color:#17365d;}
    .matrix-wrap {overflow-x:auto; border:1px solid #1f2937; background:white;}
    .process-matrix {border-collapse:collapse; min-width:1450px; width:100%; font-size:15px; color:#111827;}
    .process-matrix th, .process-matrix td {border:1px solid #1f2937; padding:6px 7px; text-align:center; white-space:nowrap;}
    .process-matrix thead th {background:#ffffff; font-weight:800; font-size:16px;}
    .process-matrix .section-cell {white-space:normal; min-width:170px; font-weight:800; vertical-align:middle;}
    .process-matrix .stage-cell {text-align:left; min-width:175px; font-weight:800;}
    .process-matrix .avg-cell {font-weight:900;}
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


BATCH_STRENGTH_DEFAULTS = {
    "EG26040": "120 mg", "EG26042": "120 mg", "EG26050": "120 mg",
    "EG26051": "120 mg", "EG26054": "120 mg", "EG26058": "120 mg",
    "EG26060": "120 mg", "EG26061": "90 mg", "EG26063": "90 mg",
    "EG26065": "90 mg", "EG26067": "60 mg", "EG26069": "60 mg",
    "EG26070": "60 mg",
}

MATRIX_ROWS = [
    ("Assembly -Manual", "Assembly", "Assembly -Manual"),
    ("Seal dry run", "System integrity - Time from strt to end of the cycle", "SI elapsed Seal dry run"),
    ("Leak test", "System integrity - Time from strt to end of the cycle", "SI elapsed Leak test"),
    ("Vacuum test", "System integrity - Time from strt to end of the cycle", "SI elapsed Vacuum test"),
    ("Seal dry run", "System integrity - (actual cycle time)", "SI actual Seal dry run"),
    ("Leak test", "System integrity - (actual cycle time)", "SI actual Leak test"),
    ("Vacuum test", "System integrity - (actual cycle time)", "SI actual Vacuum test"),
    ("Water charging", "Gel manufacturing", "Water charging"),
    ("API charging", "Gel manufacturing", "API charging"),
    ("Wetting", "Gel manufacturing", "Wetting"),
    ("Pre-mixing", "Gel manufacturing", "Pre-mixing"),
    ("Mixing", "Gel manufacturing", "Mixing"),
    ("Pre-pH Blending", "Gel manufacturing", "Pre-pH Blending"),
    ("pH sample", "Gel manufacturing", "pH sample 1"),
    ("pH adjustment", "Gel manufacturing", "pH adjustment"),
    ("Post-pH blending", "Gel manufacturing", "Post-pH blending"),
    ("pH sample", "Gel manufacturing", "pH sample 2"),
    ("Filling setup", "Filling", "Filling setup"),
    ("Filling", "Filling", "Filling"),
]


def _hours_between(start, end):
    if pd.isna(start) or pd.isna(end):
        return None
    return round((end - start).total_seconds() / 3600, 2)


def _batch_matrix_values(batch_table: pd.DataFrame, manual_hours: float) -> dict[str, float | None]:
    values = {key: None for _, _, key in MATRIX_ROWS}
    values["Assembly -Manual"] = round(float(manual_hours), 2)
    valid = batch_table[batch_table["Validation"].astype(str).eq("OK")].copy()
    valid["Process Start"] = pd.to_datetime(valid["Process Start"], errors="coerce")
    valid["Process End"] = pd.to_datetime(valid["Process End"], errors="coerce")

    for label, raw_stage in (
        ("Seal dry run", "Seal dry run"),
        ("Leak test", "Leak test"),
        ("Vacuum test", "Vacuum test (Max)"),
    ):
        group = valid[valid["Stage"].eq(raw_stage)]
        if not group.empty:
            values[f"SI elapsed {label}"] = _hours_between(
                group["Process Start"].min(), group["Process End"].max()
            )
            machine_cycles = []
            for _, machine_group in group.groupby("Machine", dropna=False):
                duration = _hours_between(
                    machine_group["Process Start"].min(),
                    machine_group["Process End"].max(),
                )
                if duration is not None:
                    machine_cycles.append(duration)
            values[f"SI actual {label}"] = max(machine_cycles) if machine_cycles else None

    for stage in ("Wetting", "Pre-mixing", "Mixing"):
        group = valid[valid["Stage"].eq(stage)]
        durations = pd.to_numeric(group["Duration (hours)"], errors="coerce").dropna()
        values[stage] = round(float(durations.max()), 2) if not durations.empty else None

    for stage in ("Water charging", "API charging", "Pre-pH Blending", "pH adjustment",
                  "Post-pH blending", "Filling setup", "Filling"):
        group = valid[valid["Stage"].eq(stage)]
        durations = pd.to_numeric(group["Duration (hours)"], errors="coerce").dropna()
        values[stage] = round(float(durations.sum()), 2) if not durations.empty else None

    ph = valid[valid["Stage"].eq("pH sample")].sort_values("Process Start")
    ph_durations = pd.to_numeric(ph["Duration (hours)"], errors="coerce").dropna().tolist()
    if ph_durations:
        values["pH sample 1"] = round(float(ph_durations[0]), 2)
    if len(ph_durations) > 1:
        values["pH sample 2"] = round(float(ph_durations[-1]), 2)
    return values


def build_reference_matrix(actual: pd.DataFrame, batches: list[str], manual_hours: float) -> pd.DataFrame:
    data = {}
    for batch in batches:
        data[batch] = _batch_matrix_values(
            actual[actual["Batch"].astype(str).eq(batch)], manual_hours
        )
    rows = []
    for display_stage, section, key in MATRIX_ROWS:
        row = {"Section": section, "Stage": display_stage, "Key": key}
        for batch in batches:
            row[batch] = data[batch].get(key)
        present = [row[b] for b in batches if row[b] is not None and not pd.isna(row[b])]
        row["Average"] = round(sum(present) / len(present), 2) if present else None
        rows.append(row)
    return pd.DataFrame(rows)


def render_reference_matrix(
    matrix: pd.DataFrame,
    batches: list[str],
    strengths: dict[str, str],
) -> None:
    """Render a native, filterable Streamlit process-time table.

    Unlike the former pasted-looking HTML table, this uses Streamlit's native
    dataframe surface, so users can sort columns, resize them, search, and open
    the table in full-screen mode.
    """
    section_options = matrix["Section"].drop_duplicates().tolist()
    available_strengths = [
        strength
        for strength in ("120 mg", "90 mg", "60 mg", "Other")
        if any(strengths.get(batch) == strength for batch in batches)
    ]

    control_1, control_2, control_3 = st.columns([1.15, 2.2, 1.0])
    selected_strength = control_1.selectbox(
        "Strength view",
        ["All strengths", *available_strengths],
        key="summary_strength_view",
    )
    selected_sections = control_2.multiselect(
        "Process sections",
        section_options,
        default=section_options,
        key="summary_section_filter",
    )
    show_average = control_3.toggle(
        "Show average",
        value=True,
        key="summary_show_average",
    )

    if selected_strength == "All strengths":
        visible_batches = [
            batch
            for strength in ("120 mg", "90 mg", "60 mg", "Other")
            for batch in batches
            if strengths.get(batch) == strength
        ]
        visible_batches += [batch for batch in batches if batch not in visible_batches]
    else:
        visible_batches = [
            batch for batch in batches
            if strengths.get(batch) == selected_strength
        ]

    filtered = matrix[matrix["Section"].isin(selected_sections)].copy()
    display_columns = ["Section", "Stage", *visible_batches]
    if show_average:
        display_columns.append("Average")
    display = filtered[display_columns].reset_index(drop=True)

    colors = {
        "Assembly": "#d7d1ad",
        "System integrity - Time from strt to end of the cycle": "#ffd1a8",
        "System integrity - (actual cycle time)": "#ffad63",
        "Gel manufacturing": "#c4d7eb",
        "Filling": "#edc1c3",
    }

    numeric_columns = [
        column for column in display.columns
        if column not in {"Section", "Stage"}
    ]

    def color_row(row: pd.Series) -> list[str]:
        color = colors.get(str(row["Section"]), "#ffffff")
        return [f"background-color: {color}; color: #172033"] * len(row)

    styled = (
        display.style
        .format({column: "{:.2f}" for column in numeric_columns}, na_rep="-")
        .apply(color_row, axis=1)
        .set_properties(subset=["Section", "Stage"], **{"font-weight": "600"})
    )

    st.caption(
        f"Showing {len(display)} process rows across {len(visible_batches)} batch(es). "
        "Use the column headers to sort, drag column boundaries to resize, or "
        "open full screen from the table toolbar."
    )
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        height=min(720, 92 + 36 * max(len(display), 1)),
        column_config={
            "Section": st.column_config.TextColumn("Process section", width="large"),
            "Stage": st.column_config.TextColumn("Stage", width="medium"),
            **{
                column: st.column_config.NumberColumn(
                    column,
                    help=f"Process time for {column}, hours",
                    format="%.2f",
                    width="small",
                )
                for column in numeric_columns
            },
        },
    )

    # Compact visual comparison for the currently filtered table.
    if visible_batches and not filtered.empty:
        chart_data = filtered.set_index("Stage")[visible_batches].copy()
        chart_data = chart_data.loc[chart_data.notna().any(axis=1)]
        if not chart_data.empty:
            with st.expander("Quick visual comparison", expanded=False):
                long_data = (
                    chart_data.reset_index()
                    .melt(id_vars="Stage", var_name="Batch", value_name="Hours")
                    .dropna(subset=["Hours"])
                )
                comparison = px.bar(
                    long_data,
                    x="Stage",
                    y="Hours",
                    color="Batch",
                    barmode="group",
                    title="Selected process rows by batch",
                )
                comparison.update_layout(xaxis_tickangle=-35, height=460)
                st.plotly_chart(comparison, use_container_width=True)

def build_major_totals(matrix: pd.DataFrame, batches: list[str]) -> pd.DataFrame:
    result = pd.DataFrame(index=["Assembly -Mannual", "System integrity", "Gel manufacturing", "Filling"], columns=batches, dtype=float)
    for batch in batches:
        result.loc["Assembly -Mannual", batch] = matrix.loc[matrix["Key"].eq("Assembly -Manual"), batch].sum(min_count=1)
        result.loc["System integrity", batch] = matrix.loc[matrix["Key"].str.startswith("SI actual"), batch].sum(min_count=1)
        result.loc["Gel manufacturing", batch] = matrix.loc[matrix["Section"].eq("Gel manufacturing"), batch].sum(min_count=1)
        result.loc["Filling", batch] = matrix.loc[matrix["Section"].eq("Filling"), batch].sum(min_count=1)
    result.loc["Total time"] = result.sum(axis=0, min_count=1)
    result["Average"] = result[batches].mean(axis=1).round(2)
    result.index.name = "Stage"
    return result.round(2)


def upload_fingerprint(files) -> str:
    """Create a lightweight key that changes when uploaded documents change."""
    digest = hashlib.sha256()
    for uploaded in sorted(files, key=lambda item: item.name.casefold()):
        size = int(getattr(uploaded, "size", 0) or 0)
        digest.update(uploaded.name.encode("utf-8", errors="ignore"))
        digest.update(size.to_bytes(8, "big"))
        # Hash only edge samples. Avoid making and hashing another full-size
        # copy of every large PDF during every Streamlit rerun.
        uploaded.seek(0)
        digest.update(uploaded.read(min(size, 65536)))
        if size > 65536:
            uploaded.seek(max(0, size - 65536))
            digest.update(uploaded.read(65536))
        uploaded.seek(0)
    return digest.hexdigest()


def reference_summary_excel(
    matrix: pd.DataFrame,
    totals: pd.DataFrame,
    batches: list[str],
    strengths: dict[str, str],
) -> bytes:
    """Export the strength-and-batch matrix and total-process-time table."""
    output = io.BytesIO()
    matrix_export = matrix.drop(columns=["Key"]).copy()
    strength_row = pd.DataFrame(
        [{
            "Section": "Strength",
            "Stage": "Strength",
            **{batch: strengths.get(batch, "Other") for batch in batches},
            "Average": "All batches",
        }]
    )
    matrix_export = pd.concat([strength_row, matrix_export], ignore_index=True)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        matrix_export.to_excel(
            writer, index=False, sheet_name="Process Time Summary"
        )
        totals.reset_index().to_excel(
            writer, index=False, sheet_name="Total Process Time"
        )
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "C2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for column_cells in worksheet.columns:
                width = min(
                    max(len(str(cell.value or "")) for cell in column_cells) + 2,
                    42,
                )
                worksheet.column_dimensions[column_cells[0].column_letter].width = width
    return output.getvalue()

def main() -> None:
    st.title("EB Execution Date Extractor")
    st.caption(f"Build: {APP_VERSION}")
    st.caption(
        "Upload all report parts together. Files are grouped by the batch number inside each report; "
        "split operations are merged chronologically before phase validation."
    )

    action_1, action_2 = st.columns([4, 1])
    with action_2:
        if st.button("Clear session", use_container_width=True):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()

    files = st.file_uploader(
        "Upload System Integrity, Batch Cycle, continuation, and PFS Filling PDFs",
        type=["pdf"],
        accept_multiple_files=True,
        key="batch_pdf_uploads",
    )
    if not files:
        st.info("Select all available PDF parts for the required batches.")
        st.caption(
            "The PDF engine, analytics libraries, and charts are loaded only after "
            "documents are selected. This keeps the app landing page lightweight."
        )
        return

    # Heavy libraries are imported only after documents have been selected.
    # This significantly reduces cold-start time when opening the app.
    global pd, px
    import pandas as pd
    import plotly.express as px
    from parser import parse_pdf, combine_reports

    # Streamlit retains UploadedFile objects while they remain selected. Keep
    # only compact extracted results in session state, never raw PDF bytes or
    # duplicated parsed-report structures.
    document_key = upload_fingerprint(files)
    total_bytes = sum(int(getattr(item, "size", 0) or 0) for item in files)
    size_text = f" ({total_bytes / 1024 / 1024:.1f} MB)" if total_bytes else ""
    st.write(f"**{len(files)} document(s) received{size_text}.**")

    cached_key = st.session_state.get("processed_document_key")
    required_keys = {
        "processed_log", "processed_actual", "processed_merged_datalog"
    }
    if cached_key == document_key and required_keys.issubset(st.session_state):
        log = st.session_state["processed_log"]
        actual = st.session_state["processed_actual"]
        merged_datalog = st.session_state["processed_merged_datalog"]
        st.success("Using extracted results from this session. No PDF reprocessing was performed.")
    else:
        progress = st.progress(0, text="Preparing uploaded documents...")
        status = st.empty()
        reports: list[dict[str, Any]] = []
        log_rows: list[dict[str, Any]] = []
        sorted_files = sorted(files, key=lambda item: item.name.casefold())

        for index, uploaded in enumerate(sorted_files, start=1):
            progress.progress(
                (index - 1) / len(sorted_files),
                text=f"Extracting {uploaded.name} ({index}/{len(sorted_files)})",
            )
            status.info(f"Reading report structure and datalog: {uploaded.name}")
            try:
                uploaded.seek(0)
                report = parse_pdf(uploaded, uploaded.name)
                uploaded.seek(0)
                reports.append(report)
                error_message = report.get("Error", "")
                log_rows.append({
                    "Batch": report.get("Batch", "UNKNOWN"),
                    "File": uploaded.name,
                    "Report type": report.get("Report Type", "Unknown"),
                    "Rows extracted before merge": len(report.get("Rows", [])),
                    "Blending events": len(report.get("Blending Events", [])),
                    "Status": "Error" if error_message else "Part loaded",
                    "Message": error_message,
                })
            except Exception as error:
                log_rows.append({
                    "Batch": "UNKNOWN", "File": uploaded.name,
                    "Report type": "Error", "Rows extracted before merge": 0,
                    "Blending events": 0, "Status": "Failed",
                    "Message": str(error),
                })
            progress.progress(
                index / len(sorted_files),
                text=f"Processed {index}/{len(sorted_files)} documents",
            )

        status.info("Grouping report parts by batch number and merging operations...")
        log = pd.DataFrame(log_rows).sort_values(["Batch", "File"]).reset_index(drop=True)
        actual = combine_reports(reports)
        merged_datalog = build_merged_datalog_preview(reports)

        # Store only the merged compact outputs. Do not store raw PDFs and do
        # not keep the per-report event tables a second time.
        st.session_state["processed_document_key"] = document_key
        st.session_state["processed_log"] = log
        st.session_state["processed_actual"] = actual
        st.session_state["processed_merged_datalog"] = merged_datalog
        st.session_state.pop("processed_reports", None)
        del reports
        gc.collect()

        progress.progress(1.0, text="Extraction and batch merge complete")
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
    with st.sidebar:
        st.header("Reference Summary Settings")
        manual_hours = st.number_input(
            "Assembly - Manual (hours)", min_value=0.0, max_value=100.0,
            value=16.0, step=0.5,
        )
        strengths = {}
        for batch in batches:
            options = ["120 mg", "90 mg", "60 mg", "Other"]
            default = BATCH_STRENGTH_DEFAULTS.get(batch, "Other")
            strengths[batch] = st.selectbox(
                f"{batch} strength", options, index=options.index(default),
                key=f"strength_{batch}",
            )

    reference_matrix = build_reference_matrix(actual, batches, manual_hours)
    major_totals = build_major_totals(reference_matrix, batches)
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
        "Reference Process-Time Summary",
        "Batch Comparison Plots",
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
                on_click="ignore",
            )

    with tabs[2]:
        st.subheader("Process Time Summary by Strength and Batch")
        st.caption(
            "This table matches the original layout: 120 mg, 90 mg, and 60 mg batch groups, "
            "system-integrity elapsed and actual cycle times, gel-manufacturing stages, filling stages, and averages."
        )
        render_reference_matrix(reference_matrix, batches, strengths)

        summary_download_1, summary_download_2 = st.columns(2)
        summary_flat = reference_matrix.drop(columns=["Key"]).copy()
        with summary_download_1:
            st.download_button(
                "Download Process Time Summary - Excel",
                data=reference_summary_excel(
                    reference_matrix, major_totals, batches, strengths
                ),
                file_name="Process_Time_Summary_by_Strength_and_Batch.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                on_click="ignore",
            )
        with summary_download_2:
            st.download_button(
                "Download Process Time Summary - CSV",
                data=summary_flat.to_csv(index=False).encode("utf-8"),
                file_name="Process_Time_Summary_by_Strength_and_Batch.csv",
                mime="text/csv",
                use_container_width=True,
                on_click="ignore",
            )

        st.subheader("Total Process Time")
        total_strength = st.radio(
            "Total table strength",
            ["All strengths", "120 mg", "90 mg", "60 mg"],
            horizontal=True,
            key="total_strength_filter",
        )
        total_batches = (
            batches
            if total_strength == "All strengths"
            else [batch for batch in batches if strengths.get(batch) == total_strength]
        )
        total_columns = [*total_batches, "Average"]
        total_display = major_totals[total_columns].reset_index()

        def total_row_style(row: pd.Series) -> list[str]:
            stage = str(row["Stage"])
            color = {
                "Assembly -Mannual": "#d7d1ad",
                "System integrity": "#ffad63",
                "Gel manufacturing": "#c4d7eb",
                "Filling": "#edc1c3",
                "Total time": "#dbeafe",
            }.get(stage, "#ffffff")
            weight = "700" if stage == "Total time" else "500"
            return [f"background-color:{color}; font-weight:{weight}; color:#172033"] * len(row)

        total_style = (
            total_display.style
            .format({column: "{:.2f}" for column in total_columns}, na_rep="-")
            .apply(total_row_style, axis=1)
        )
        st.dataframe(
            total_style,
            use_container_width=True,
            hide_index=True,
            height=248,
            column_config={
                "Stage": st.column_config.TextColumn("Stage", width="medium"),
                **{
                    column: st.column_config.NumberColumn(
                        column, format="%.2f", width="small"
                    )
                    for column in total_columns
                },
            },
        )

    with tabs[3]:
        st.subheader("Batch Total Process-Time Comparison")
        stage_totals = major_totals.drop(index="Total time")[batches].T.reset_index(names="Batch")
        stage_totals["Strength"] = stage_totals["Batch"].map(strengths)
        stacked = stage_totals.melt(
            id_vars=["Batch", "Strength"],
            value_vars=["Assembly -Mannual", "System integrity", "Gel manufacturing", "Filling"],
            var_name="Stage", value_name="Hours",
        )
        stacked_fig = px.bar(
            stacked, x="Batch", y="Hours", color="Stage", barmode="stack",
            hover_data=["Strength"], text_auto=".1f",
            color_discrete_map={
                "Assembly -Mannual":"#c8c19b", "System integrity":"#f5a25a",
                "Gel manufacturing":"#7fa7d1", "Filling":"#cf777b",
            },
            title="Stage contribution to total process time",
        )
        st.plotly_chart(stacked_fig, use_container_width=True)

        left, right = st.columns(2)
        with left:
            heat = reference_matrix.set_index("Stage")[batches].copy()
            heat.index = [f"{stage} ({key})" if list(reference_matrix["Stage"]).count(stage) > 1 else stage
                          for stage, key in zip(reference_matrix["Stage"], reference_matrix["Key"])]
            heat_fig = px.imshow(
                heat, text_auto=".1f", aspect="auto", color_continuous_scale="RdYlBu_r",
                labels={"x":"Batch", "y":"Process stage", "color":"Hours"},
                title="Batch-to-batch process-time heatmap",
            )
            st.plotly_chart(heat_fig, use_container_width=True)
        with right:
            totals_plot = pd.DataFrame({
                "Batch": batches,
                "Total hours": major_totals.loc["Total time", batches].values,
                "Strength": [strengths[b] for b in batches],
            })
            trend = px.line(
                totals_plot, x="Batch", y="Total hours", color="Strength", markers=True,
                title="Total process time by batch and strength",
            )
            trend.add_hline(
                y=float(totals_plot["Total hours"].mean()), line_dash="dash",
                annotation_text=f"Average {totals_plot['Total hours'].mean():.2f} h",
            )
            st.plotly_chart(trend, use_container_width=True)

        timeline = batch_table[
            batch_table["Validation"].astype(str).eq("OK")
        ].dropna(subset=["Process Start", "Process End"])
        if not timeline.empty:
            timeline_fig = px.timeline(
                timeline, x_start="Process Start", x_end="Process End", y="Stage",
                color="Machine", hover_data=["Duration (hours)", "Source"],
                title=f"{selected_batch} merged execution timeline",
            )
            timeline_fig.update_yaxes(autorange="reversed")
            st.plotly_chart(timeline_fig, use_container_width=True)

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
        on_click="ignore",
    )


if __name__ == "__main__":
    main()

