import io, pandas as pd, plotly.express as px, streamlit as st
from parser import parse_pdf,combine
st.set_page_config(page_title='EB Execution Date Extractor',layout='wide')
st.title('EB Execution Date Extractor')
st.caption('Upload System Integrity, Batch Cycle and PFS Filling PDFs. Files are automatically classified and grouped by batch number.')
fs=st.file_uploader('PDF reports',type='pdf',accept_multiple_files=True)
if not fs:st.info('Upload all available reports for one or more batches.');st.stop()
reports=[]
for i,f in enumerate(fs):
 try:reports.append(parse_pdf(f,f.name))
 except Exception as e:st.error(f'{f.name}: {e}')
log=pd.DataFrame([{'File':f.name,'Report type':r['Report Type'],'Batch':r['Batch'],'Rows':len(r['Rows'])} for f,r in zip(fs,reports)])
st.subheader('Extraction log');st.dataframe(log,use_container_width=True,hide_index=True)
df=combine(reports)
if df.empty:st.warning('No execution records were detected.');st.stop()
b=st.selectbox('Batch',df.Batch.unique());v=df[df.Batch==b].copy()
show=v.copy()
for c in ['Process Start','Process End']:show[c]=pd.to_datetime(show[c]).dt.strftime('%d/%m/%Y %H:%M:%S')
st.subheader('Actual Process Time');st.dataframe(show,use_container_width=True,hide_index=True)
# Workbook summary: max actual cycle duration for integrity/extruder parallel stages, sum for sequential stages.
max_st={'Seal dry run','Leak test','Vacuum test (Max)','Wetting','Pre-mixing','Mixing'}
s=[]
for stage,g in v.groupby('Stage',sort=False):
 val=g['Duration (hours)'].max() if stage in max_st else g['Duration (hours)'].sum()
 s.append({'Stage':stage,'Process time (hours)':round(float(val),3)})
sumdf=pd.DataFrame(s)
st.subheader('Process Time Summary');st.dataframe(sumdf,use_container_width=True,hide_index=True)
fig=px.timeline(v,x_start='Process Start',x_end='Process End',y='Stage',color='Machine',hover_data=['Duration (hours)','Source','Validation']);fig.update_yaxes(autorange='reversed');st.plotly_chart(fig,use_container_width=True)
buf=io.BytesIO()
with pd.ExcelWriter(buf,engine='openpyxl',datetime_format='dd/mm/yyyy hh:mm:ss') as w:
 df.to_excel(w,index=False,sheet_name='Actual Process Time');sumdf.to_excel(w,index=False,sheet_name='Process Time Summary');log.to_excel(w,index=False,sheet_name='Extraction Log')
st.download_button('Download extracted workbook',buf.getvalue(),f'{b}_execution_dates.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
