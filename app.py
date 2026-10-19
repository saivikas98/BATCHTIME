import io, zipfile
import pandas as pd
import plotly.express as px
import streamlit as st
from parser import parse_pdf, build_execution_table

st.set_page_config(page_title='EB Execution Date Extractor',layout='wide')
st.title('EB Execution Date Extractor')
st.caption('Upload Batch Cycle, PFS Filling and (when available) System Integrity PDF reports. Multiple batches are supported.')
files=st.file_uploader('PDF reports',type=['pdf'],accept_multiple_files=True)
if not files:
    st.info('Upload both reports for each batch. System-integrity timestamps require the corresponding integrity report because they are not present in the supplied EG26061 Batch Cycle or PFS Filling PDFs.')
    st.stop()

parsed=[]
progress=st.progress(0,text='Reading reports...')
for i,f in enumerate(files):
    try:
        r=parse_pdf(f,f.name); r['file_name']=f.name; parsed.append(r)
    except Exception as e: st.error(f'{f.name}: {e}')
    progress.progress((i+1)/len(files),text=f'Processed {i+1}/{len(files)}')
progress.empty()

if not parsed: st.stop()
summary=pd.DataFrame([{'File':x['file_name'],'Detected report':x['report_type'],'Batch':x['batch_no'],'Rows extracted':len(x['rows'])} for x in parsed])
st.subheader('Report recognition'); st.dataframe(summary,use_container_width=True,hide_index=True)
actual=build_execution_table(parsed)
if actual.empty: st.warning('No execution-date rows were found.'); st.stop()

batch=st.selectbox('Batch',sorted(actual.Batch.unique()))
view=actual[actual.Batch==batch].copy()
view['Start']=pd.to_datetime(view['Start']); view['End']=pd.to_datetime(view['End'])
fmt=view.copy(); fmt['Start']=fmt['Start'].dt.strftime('%d/%m/%Y %H:%M:%S'); fmt['End']=fmt['End'].dt.strftime('%d/%m/%Y %H:%M:%S')
st.subheader('Actual Process Time'); st.dataframe(fmt,use_container_width=True,hide_index=True)

# Summary follows workbook logic while preserving machine-level source rows.
def stage_duration(stage,mode='max'):
    x=view[view.Stage==stage].Duration_hours.dropna()
    if x.empty:return None
    return float(x.max() if mode=='max' else x.sum())
summary_rows=[]
for s in ['Seal dry run','Leak test','Vacuum test (Max)','Water charging','API charging','Wetting','Pre-mixing','Mixing','Pre-pH Blending','pH sample','pH adjustment','Post-pH blending','Filling setup','Filling']:
    x=view[view.Stage==s]
    if x.empty: continue
    val=float(x.Duration_hours.max()) if s in ['Seal dry run','Leak test','Vacuum test (Max)','Wetting','Pre-mixing','Mixing'] else float(x.Duration_hours.sum())
    summary_rows.append({'Stage':s,'Process time (hours)':round(val,3)})
sdf=pd.DataFrame(summary_rows)
st.subheader('Process Time Summary'); st.dataframe(sdf,use_container_width=True,hide_index=True)

plot=view.dropna(subset=['Start','End']).copy()
if not plot.empty:
    fig=px.timeline(plot,x_start='Start',x_end='End',y='Stage',color='Machine',hover_data=['Duration_hours','Source','Confidence'])
    fig.update_yaxes(autorange='reversed'); st.plotly_chart(fig,use_container_width=True)

# Quality checks eliminate silently mixed start/end times across extruders.
checks=[]
for _,r in view.iterrows():
    status='OK'
    if pd.isna(r.Start) or pd.isna(r.End): status='Missing timestamp'
    elif r.End<r.Start: status='End before start'
    checks.append({**r.to_dict(),'Validation':status})
q=pd.DataFrame(checks)
with st.expander('Extraction trace and validation'):
    st.dataframe(q,use_container_width=True,hide_index=True)

buf=io.BytesIO()
with pd.ExcelWriter(buf,engine='openpyxl',datetime_format='dd/mm/yyyy hh:mm:ss') as w:
    actual.to_excel(w,index=False,sheet_name='Actual Process Time')
    sdf.assign(Batch=batch).to_excel(w,index=False,sheet_name='Process Time Summary')
    summary.to_excel(w,index=False,sheet_name='Extraction Log')
st.download_button('Download extracted workbook',buf.getvalue(),file_name=f'{batch}_execution_dates.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
