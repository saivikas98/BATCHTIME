from __future__ import annotations
import io, re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
import fitz
import pandas as pd

DT=r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}"
DT_FMT="%d/%m/%Y %H:%M:%S"

def _dt(x):
    try: return datetime.strptime(x.strip(), DT_FMT)
    except Exception: return pd.NaT

def _open_pdf(data):
    if isinstance(data,(str,Path)): return fitz.open(str(data))
    if hasattr(data,'getvalue'): data=data.getvalue()
    if hasattr(data,'read'): data=data.read()
    return fitz.open(stream=data,filetype='pdf')

def _page_text(doc,n): return doc.load_page(n).get_text('text',sort=True)

def detect_report(text):
    u=text.upper()
    if 'BATCH CYCLE REPORT' in u: return 'Batch Cycle Report'
    if 'EQUIPMENT ID:' in u and ('PRODUCTION COUNTERS' in u or 'ACTIVE RECIPE' in u): return 'PFS Filling Report'
    if any(k in u for k in ['SEAL DRY RUN','LEAK TEST','VACUUM TEST']): return 'System Integrity Report'
    return 'Unknown'

def extract_batch_no(text,name=''):
    pats=[r'Batch\s*(?:no\.?|number)\s*[:.]?\s*([A-Z]{2}\d{5})',r'\b([A-Z]{2}\d{5})\b']
    for p in pats:
        m=re.search(p,text,re.I)
        if m:return m.group(1).upper()
    m=re.search(r'([A-Z]{2}\d{5})',name,re.I)
    return m.group(1).upper() if m else 'UNKNOWN'

def _pair(label,start,end,machine='',source='',confidence='High'):
    s,e=_dt(start),_dt(end)
    return {'Stage':label,'Machine':machine,'Start':s,'End':e,'Duration_hours':round((e-s).total_seconds()/3600,4) if pd.notna(s) and pd.notna(e) else None,'Source':source,'Confidence':confidence}

def parse_overview(text):
    rows=[]
    # Matches the report's Process Overview lines, including machine names with spaces and extruder number.
    p=re.compile(r'(?m)^\s*(API Charge|Water Charge|Wetting|Premixing|Mixing|Blending)\s+(API Charge|Water Charge|Mixing Extruder\s*[123])\s+('+DT+r')\s+('+DT+r')\s+\d+\s+\d+\s*$' ,re.I)
    # Python regex cannot accept the accidental markdown-style terminator above; built below explicitly.
    for m in p.finditer(text):
        proc,mach,st,en=m.groups()
        rows.append((proc.title().replace('Premixing','Pre-mixing'),re.sub(r'\s+',' ',mach).title(),st,en))
    return rows

def parse_cycle(data,name=''):
    doc=_open_pdf(data)
    first='\n'.join(_page_text(doc,i) for i in range(min(3,len(doc))))
    batch=extract_batch_no(first,name)
    product=''
    m=re.search(r'Product name\s*[:*]*\s*([^\n*]+)',first,re.I)
    if m: product=m.group(1).strip()
    overview=parse_overview(first)
    out=[]
    charge_groups={'Water charging':[],'API charging':[]}
    for proc,mach,st,en in overview:
        stage={'Water Charge':'Water charging','Api Charge':'API charging','Wetting':'Wetting','Pre-Mixing':'Pre-mixing','Mixing':'Mixing','Blending':'Blending'}.get(proc,proc)
        if stage in charge_groups:
            charge_groups[stage].append((_dt(st),_dt(en)))
        else:
            out.append(_pair(stage,st,en,mach,'Process Overview'))
    for stage,pairs in charge_groups.items():
        if pairs:
            s0=min(x[0] for x in pairs); e0=max(x[1] for x in pairs)
            out.append({'Stage':stage,'Machine':stage,'Start':s0,'End':e0,'Duration_hours':round((e0-s0).total_seconds()/3600,4),'Source':'Process Overview - min start/max end','Confidence':'High'})
    # Blending phase boundaries from the authoritative BLENDING DATALOG step transitions.
    blend_rows=[]
    dtre=re.compile(r'^\s*('+DT+r')\s+(.+?)\s{2,}',re.M)
    for i in range(2,len(doc)):
        t=_page_text(doc,i)
        if 'BLENDING DATALOG' not in t.upper(): continue
        for m in dtre.finditer(t):
            step=re.sub(r'\s+',' ',m.group(2)).strip()
            blend_rows.append((_dt(m.group(1)),step))
    blend_rows=[x for x in blend_rows if pd.notna(x[0])]
    blend_rows.sort(key=lambda x:x[0])
    b=[r for r in out if r['Stage']=='Blending']
    if b and blend_rows:
        bs,be=b[0]['Start'],b[0]['End']
        def last_of(term,lo=None,hi=None):
            xs=[d for d,s in blend_rows if term.lower() in s.lower() and (lo is None or d>=lo) and (hi is None or d<=hi)]
            return max(xs) if xs else None
        first_sample=min([d for d,s in blend_rows if 'sample: ips ph' in s.lower()],default=None)
        pre_end=last_of('Blending to accumulator',bs,first_sample) if first_sample else None
        # First sample block ends immediately before first Adjust PH row.
        first_adjust=min([d for d,s in blend_rows if 'adjust ph' in s.lower()],default=None)
        sample1_end=last_of('Sample: IPS PH',pre_end,first_adjust) if pre_end else None
        adjust_end=last_of('Adjust PH',first_adjust,be) if first_adjust else None
        # Second sample starts after final blending-to-accumulator block.
        second_blend_end=last_of('Blending to accumulator',adjust_end,be) if adjust_end else None
        phases=[('Pre-pH Blending',bs,pre_end),('pH sample',pre_end,sample1_end),('pH adjustment',sample1_end,adjust_end),('Post-pH blending',adjust_end,second_blend_end),('pH sample',second_blend_end,be)]
        out=[r for r in out if r['Stage']!='Blending']
        for label,s,e in phases:
            if s and e: out.append({'Stage':label,'Machine':'Mixing Extruder 1','Start':s,'End':e,'Duration_hours':round((e-s).total_seconds()/3600,4),'Source':'Blending Datalog','Confidence':'High'})
    doc.close()
    return {'batch_no':batch,'product':product,'report_type':'Batch Cycle Report','rows':pd.DataFrame(out)}

def parse_pfs(data,name=''):
    doc=_open_pdf(data); text='\n'.join(_page_text(doc,i) for i in range(min(8,len(doc))))
    batch=extract_batch_no(text,name)
    def one(label):
        m=re.search(label+r'\s*:\s*('+DT+r')',text,re.I); return _dt(m.group(1)) if m else pd.NaT
    s,e=one('Batch Start Time'),one('Batch End Time')
    product=''; m=re.search(r'Product Name\s*:\s*([^\n]+)',text,re.I)
    if m: product=m.group(1).strip()
    counters={}
    m=re.search(r'Production Counters:\s*\n?\s*Infeed \(pcs\)\s+Outfeed \(pcs\)\s+Reject \(pcs\)\s*\n?\s*(\d+)\s+(\d+)\s+(\d+)',text,re.I)
    if m: counters=dict(Infeed=int(m.group(1)),Outfeed=int(m.group(2)),Reject=int(m.group(3)))
    doc.close()
    rows=pd.DataFrame([{'Stage':'Filling','Machine':'PFS Filling Line','Start':s,'End':e,'Duration_hours':round((e-s).total_seconds()/3600,4) if pd.notna(s) and pd.notna(e) else None,'Source':'PFS Batch Header','Confidence':'High'}])
    return {'batch_no':batch,'product':product,'report_type':'PFS Filling Report','rows':rows,'counters':counters}

def parse_integrity(data,name=''):
    doc=_open_pdf(data); batch='UNKNOWN'; rows=[]
    keys=[('Seal dry run','seal dry run'),('Leak test','leak test'),('Vacuum test (Max)','vacuum test')]
    for i in range(len(doc)):
        t=_page_text(doc,i); batch=extract_batch_no(t,name) if batch=='UNKNOWN' else batch
        for label,key in keys:
            if key not in t.lower(): continue
            dts=re.findall(DT,t)
            if len(dts)>=2: rows.append(_pair(label,dts[0],dts[-1],f'Integrity unit {len([r for r in rows if r["Stage"]==label])+1}',f'Page {i+1}','Medium'))
    doc.close(); return {'batch_no':batch,'report_type':'System Integrity Report','rows':pd.DataFrame(rows)}

def parse_pdf(data,name=''):
    doc=_open_pdf(data); probe='\n'.join(_page_text(doc,i) for i in range(min(8,len(doc)))); doc.close()
    k=detect_report(probe)
    if k=='Batch Cycle Report': return parse_cycle(data,name)
    if k=='PFS Filling Report': return parse_pfs(data,name)
    return parse_integrity(data,name)

def build_execution_table(parsed):
    by={}
    for x in parsed: by.setdefault(x['batch_no'],[]).append(x)
    outputs=[]
    for batch,reps in by.items():
        chunks=[r['rows'] for r in reps if not r['rows'].empty]
        if not chunks: continue
        df=pd.concat(chunks,ignore_index=True)
        # Filling setup is the gap from batch-cycle end to PFS start.
        cycle=[r for r in reps if r['report_type']=='Batch Cycle Report']
        pfs=[r for r in reps if r['report_type']=='PFS Filling Report']
        if cycle and pfs:
            cdf,pdf=cycle[0]['rows'],pfs[0]['rows']
            cend=cdf['End'].max(); pstart=pdf.iloc[0]['Start']
            if pd.notna(cend) and pd.notna(pstart):
                gap=pd.DataFrame([{'Stage':'Filling setup','Machine':'Inter-report gap','Start':cend,'End':pstart,'Duration_hours':round((pstart-cend).total_seconds()/3600,4),'Source':'Calculated: cycle end to PFS start','Confidence':'High'}])
                df=pd.concat([df,gap],ignore_index=True)
        df.insert(0,'Batch',batch); outputs.append(df.sort_values(['Start','Stage'],na_position='last'))
    return pd.concat(outputs,ignore_index=True) if outputs else pd.DataFrame()
