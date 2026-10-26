# -*- coding: utf-8 -*-
"""HTML + JS for the BOQ vs MIR panel (injected into the existing Documents view)."""

RECON_HTML = r'''
<style>
#reconPanel .rc-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
#reconPanel .rc-tabs button{background:#eef1f6;color:#223;border:1px solid #d5dbe6}
#reconPanel .rc-tabs button.on{background:#1f3a5f;color:#fff;border-color:#1f3a5f}
#reconPanel .rc-pane{display:none}#reconPanel .rc-pane.on{display:block}
#reconPanel table.rc{width:100%;border-collapse:collapse;font-size:12.5px}
#reconPanel table.rc th{background:#1f3a5f;color:#fff;padding:6px 8px;text-align:left;position:sticky;top:0;cursor:pointer;white-space:nowrap}
#reconPanel table.rc td{padding:5px 8px;border-bottom:1px solid #e6e9ef;vertical-align:top}
#reconPanel .num{text-align:right;font-variant-numeric:tabular-nums}
#reconPanel .rc-chip{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;margin:1px 2px 1px 0;background:#e8ecf3;color:#223}
#reconPanel .c-ok{background:#d4edda;color:#155724}#reconPanel .c-warn{background:#fff3cd;color:#7a5b00}
#reconPanel .c-bad{background:#f8d7da;color:#7b1c26}#reconPanel .c-info{background:#dbe9ff;color:#143a73}#reconPanel .c-mute{background:#eee;color:#555}
#reconPanel .rc-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:10px 0}
#reconPanel .rc-card{background:#f6f8fb;border:1px solid #e1e6ee;border-radius:8px;padding:10px}
#reconPanel .rc-card b{display:block;font-size:19px;margin-top:2px}
#reconPanel .rc-rec{border:1px solid #dfe4ec;border-radius:8px;padding:10px;margin:10px 0;background:#fff}
#reconPanel .rc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-top:6px}
#reconPanel .rc-grid label{font-size:11px;color:#555;display:block}
#reconPanel .rc-grid input,#reconPanel .rc-grid select,#reconPanel .rc-grid textarea{width:100%;box-sizing:border-box}
#reconPanel .rc-ocr{font-size:11px;color:#777;margin-top:4px}
#reconPanel .rc-guess{background:#fff8e1;border-color:#e0a800!important}
#reconPanel .rc-scan img{max-width:100%;border:1px solid #cfd6e2;border-radius:6px;margin-top:8px}
#reconPanel .rc-scroll{overflow:auto;max-height:620px;border:1px solid #e1e6ee;border-radius:8px}
#reconPanel .rc-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
</style>
<div class="card" id="reconPanel" style="margin-top:16px">
 <div class="cardHead"><div><h3>BOQ vs. MIR reconciliation</h3><div class="sub">Compare contract BOQ quantities with MIR documents of this project. OCR is only a draft: nothing counts until you review, match and approve it.</div></div><span id="rcBadge" class="badge neutral">Loading</span></div>
 <div class="rc-tabs" id="rcTabs"><button data-t="src" class="on">1. Sources</button><button data-t="rev">2. Review MIR lines</button><button data-t="rec">3. Reconciliation</button><button data-t="brd">4. Brands</button><button data-t="exp">5. Export &amp; log</button></div>
 <div id="rcMsg" class="status" role="status">&nbsp;</div>
 <div class="rc-pane on" id="rcP-src">
  <div id="rcOcr" class="sub"></div>
  <div class="rc-bar"><b>BOQ workbook (.xlsx)</b><input id="rcBoqFile" type="file" accept=".xlsx,.xlsm"><button id="rcBoqUp">Upload BOQ</button></div>
  <div class="rc-bar"><b>MIR PDFs</b><input id="rcMirFiles" type="file" accept=".pdf" multiple><button id="rcMirUp">Upload MIR PDFs</button></div>
  <details class="advanced"><summary>Use files already saved in Documents</summary><div class="advInner"><div id="rcDocPick" class="sub">Loading...</div><div class="rc-bar"><button id="rcImport" class="alt">Import selected</button></div></div></details>
  <h4 style="margin:14px 0 6px">BOQ</h4><div id="rcBoqInfo" class="sub">No BOQ yet.</div>
  <h4 style="margin:14px 0 6px">MIR documents</h4><div id="rcDocs"></div>
 </div>
 <div class="rc-pane" id="rcP-rev">
  <div class="rc-bar"><button id="rcMatch">Run matching</button><button id="rcApproveExact" class="good">Approve all "exact" matches</button>
   <select id="rcRevFilter"><option value="all">All lines</option><option value="todo">Needs attention</option><option value="unreviewed">Not reviewed</option><option value="nomatch">No approved match</option><option value="amb">Ambiguous / unmatched</option><option value="dup">Duplicates</option></select>
   <input id="rcRevSearch" placeholder="Search description / file" style="min-width:200px"></div>
  <div class="sub">Edit the values to match the scanned page (open it with "View page"), set the MIR status, then <b>Save &amp; mark reviewed</b>. Then confirm the BOQ row and press <b>Approve match</b>.</div>
  <div id="rcRecs"></div>
 </div>
 <div class="rc-pane" id="rcP-rec">
  <div id="rcSummary"></div>
  <div class="rc-bar"><input id="rcSearch" placeholder="Search code / description / part" style="min-width:220px"><select id="rcBrandF"><option value="">All brands</option></select>
   <select id="rcStatF"><option value="">All statuses</option><option value="complete">Complete</option><option value="partial">Partial</option><option value="pending_review">Pending review</option><option value="no_mir">No MIR</option><option value="over_boq">Over BOQ</option><option value="no_confirmed">No confirmed qty</option></select></div>
  <div class="rc-scroll"><table class="rc" id="rcTable"></table></div>
  <div class="sub" id="rcNote" style="margin-top:8px"></div>
 </div>
 <div class="rc-pane" id="rcP-brd"><div id="rcBrands"></div></div>
 <div class="rc-pane" id="rcP-exp">
  <div class="rc-bar"><button id="rcExport" class="good">Generate Excel report</button><span id="rcExpMsg" class="sub"></span></div>
  <h4 style="margin:12px 0 6px">Exceptions &amp; warnings</h4><div id="rcExc"></div>
  <h4 style="margin:12px 0 6px">Recent review / approval log</h4><div id="rcAudit" class="small sub"></div>
 </div>
</div>
'''

RECON_JS = r'''
(function(){
const q=id=>document.getElementById(id);if(!q('reconPanel'))return;
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt=(v,d=2)=>v==null?'N/A':Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fq=v=>v==null?'N/A':Number(v).toLocaleString('en-US',{maximumFractionDigits:4});
const pc=v=>v==null?'N/A':Number(v).toFixed(1)+'%';
const role=()=>((window.currentUser&&window.currentUser.role)||'');
const canEdit=()=>['ADMIN','ENGINEER'].includes(role())||!role();
let D=null,sortKey='code',sortDir=1,dirty=false,autoMatchBusy=false;
const msg=(t,k)=>{const m=q('rcMsg');m.textContent=t;m.className='status '+(k||'')};
async function api(path,opt){const r=await fetch(path,Object.assign({credentials:'same-origin',cache:'no-store'},opt||{}));let j={};try{j=await r.json()}catch(e){}if(!r.ok||j.error)throw Error(j.error||('Request failed ('+r.status+')'));return j}
const act=(a,body)=>api('/api/recon/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign({action:a},body||{}))});
async function load(keep){try{D=await api('/api/recon/data');const pending=(D.boq&&D.comp&&D.comp.records||[]).some(r=>{const m=r.match||{};return !!(r.eff&&r.eff.description)&&m.state==null&&m.method!=='manual'&&m.state!=='approved'});if(pending&&!autoMatchBusy){autoMatchBusy=true;try{await act('match_run');D=await api('/api/recon/data')}finally{autoMatchBusy=false}}render();clearTimeout(window._rcPoll);if((D.docs||[]).some(d=>d.ocr_status==='running'))window._rcPoll=setTimeout(()=>load(true),4000)}catch(e){q('rcBadge').textContent='Unavailable';q('rcBadge').className='badge neutral';msg(e.message,'error')}}
const chip=(t,k)=>`<span class="rc-chip ${k||''}">${esc(t)}</span>`;
const FL={not_reviewed:['not reviewed','c-warn'],qty_missing:['qty missing','c-bad'],status_missing:['status missing','c-warn'],unit_missing:['unit missing','c-warn'],duplicate:['duplicate','c-bad'],unit_conflict:['unit conflict','c-bad'],over_boq:['over BOQ','c-bad'],unit_unverified:['BOQ unit unverified','c-warn'],ref_no_conflict:['MIR no. on 2 PDFs','c-warn'],low_ocr_confidence:['low OCR conf.','c-warn'],qty_total_differ:['Q.T ≠ TOTAL','c-warn'],qty_blank_total_only:['Q.T blank, TOTAL only','c-warn'],qty_not_read:['qty not read','c-warn'],ocr_no_lines:['OCR found no lines','c-warn'],unit_fuzzy:['unit guessed by OCR','c-warn'],manual_entry:['manual entry','c-info'],status_not_counted_C:['status C - not counted','c-mute'],status_not_counted_D:['status D - not counted','c-mute'],ref_no_missing:['MIR no. missing','c-warn'],sn_missing:['no S.N','c-mute'],price_missing:['no price','c-bad'],total_mismatch:['qty×price ≠ sheet total','c-warn'],duplicate_record_excluded:['duplicate excluded','c-mute'],ref_from_filename:['MIR no. taken from file name','c-warn'],qty_variants_disagree:['Q.T readings disagree','c-bad'],total_variants_disagree:['TOTAL readings disagree','c-bad'],qty_single_reading:['Q.T read once only','c-warn'],total_single_reading:['TOTAL read once only','c-warn'],description_unreliable:['description looks garbled','c-bad'],part_repeated:['part # repeated','c-mute'],brand_missing:['no brand','c-warn'],description_missing:['no description','c-warn'],date_uncertain:['date uncertain','c-warn'],status_from_ocr:['status read by OCR - confirm','c-warn'],status_unclear:['MIR status not read','c-warn'],old_form_partial:['old form: check every field','c-warn'],possible_resubmission:['same item on another MIR','c-warn'],rows_from_variant_vote:['rows from plain OCR','c-warn'],table_header_not_found:['table not located','c-warn']};
const flags=a=>(a||[]).map(f=>{const x=FL[f]||[f.replace(/_/g,' '),'c-mute'];return chip(x[0],x[1])}).join('');
const ST={complete:['Complete','c-ok'],partial:['Partial','c-info'],pending_review:['Pending review','c-warn'],no_mir:['No MIR','c-mute'],over_boq:['OVER BOQ','c-bad'],no_confirmed:['No confirmed qty','c-warn']};
const MS={exact:['exact','c-ok'],suggested:['suggested','c-info'],ambiguous:['ambiguous','c-warn'],unmatched:['unmatched','c-bad'],approved:['approved','c-ok'],rejected:['rejected','c-mute'],needs_rematch:['needs rematch','c-warn'],not_matched:['not matched','c-mute']};
function render(){
 const s=D.comp.summary;q('rcBadge').textContent=D.boq?`${s.boq_items} BOQ items · ${s.mir_docs} MIR PDFs`:'No BOQ';q('rcBadge').className='badge'+(D.boq?'':' neutral');
 q('rcOcr').innerHTML=D.ocr.available?chip('OCR: '+D.ocr.backend+' ('+(D.ocr.langs||[]).join(', ')+')','c-ok')+' <span class="small">OCR output is a draft only; handwriting and check-boxes are often misread.</span>':chip('OCR unavailable','c-bad')+' '+esc(D.ocr.message);
 const b=D.boq;q('rcBoqInfo').innerHTML=b?`<b>${esc(b.filename)}</b> · sheet “${esc(b.sheet)}” · header row ${b.header_row} · ${b.items} items<div class="small sub">Columns: ${Object.entries(b.columns).map(([k,v])=>esc(k)+' = '+esc(v)).join(' · ')}</div><div class="small sub">${(b.sheets_considered||[]).map(x=>esc(x.sheet)+(x.header_row?' (score '+x.score+')':' (no BOQ header)')).join(' · ')}</div>${b.warnings.filter(w=>w.severity!=='info').slice(0,6).map(w=>'<div class="small">⚠ '+esc(w.message)+'</div>').join('')}`:'<div class="sub">Upload the BOQ workbook first.</div>';
 q('rcDocs').innerHTML=D.docs.length?'<table class="rc"><thead><tr><th>PDF</th><th>Pages</th><th>OCR</th><th>Lines</th><th></th></tr></thead><tbody>'+D.docs.map(d=>{const n=D.comp.records.filter(r=>r.doc_id===d.id).length;return `<tr><td>${esc(d.filename)}<div class="small sub">sha ${esc(d.sha256.slice(0,10))} · by ${esc(d.uploaded_by)}</div></td><td>${d.pages}</td><td>${d.ocr_status==='done'?chip('done','c-ok'):d.ocr_status==='running'?chip('running '+((d.ocr_progress&&d.ocr_progress.page)||0)+'/'+d.pages,'c-info'):d.ocr_status==='error'?chip('failed','c-bad')+'<div class="small">'+esc(d.ocr_error||'')+'</div>':d.ocr_status==='interrupted'?chip('interrupted - run again','c-warn'):chip('not run','c-mute')}</td><td>${n}</td><td style="white-space:nowrap"><button class="alt" data-ocr="${d.id}">Run OCR</button> <button class="alt" data-addline="${d.id}">Add line</button> <a href="/api/recon/page?doc=${d.id}&page=1" target="_blank">View p1</a> <button class="alt" data-rmdoc="${d.id}">Remove</button></td></tr>`}).join('')+'</tbody></table>':'<div class="sub">No MIR PDFs yet.</div>';
 renderRecs();renderRec();renderBrands();renderExp();
 document.querySelectorAll('#rcDocs [data-ocr]').forEach(b=>b.onclick=async()=>{b.disabled=true;msg('Running OCR... (the server is busy until it finishes)','info');try{await act('ocr',{doc_id:b.dataset.ocr});msg('OCR started in the background (about 15 s per page). This list refreshes automatically.','info');await load()}catch(e){msg(e.message,'error');b.disabled=false}});
 document.querySelectorAll('#rcDocs [data-addline]').forEach(b=>b.onclick=async()=>{const p=prompt('PDF page number for the new line:','1');if(!p)return;try{await act('add_line',{doc_id:b.dataset.addline,page:+p});msg('Line added - fill it in under step 2.','success');await load();tab('rev')}catch(e){msg(e.message,'error')}});
 document.querySelectorAll('#rcDocs [data-rmdoc]').forEach(b=>b.onclick=async()=>{if(!confirm('Remove this PDF and its lines from the reconciliation? (the original file stays on disk)'))return;try{await act('remove_doc',{doc_id:b.dataset.rmdoc});await load()}catch(e){msg(e.message,'error')}});
}
function boqOpts(r){const m=r.match||{};const c=(m.candidates||[]);const sel=r.approved_boq_id||m.boq_id||'';const known=new Set(c.map(x=>x.boq_id));
 let h='<option value="">— choose BOQ row —</option>';
 if(c.length)h+='<optgroup label="Suggested">'+c.map(x=>{const it=D.boq_items.find(i=>i.id===x.boq_id);return `<option value="${esc(x.boq_id)}"${x.boq_id===sel?' selected':''}>${esc(it?it.label:x.boq_id)}  [${Math.round(x.score*100)}%${x.unit_state==='mismatch'?' · UNIT MISMATCH':''}]</option>`}).join('')+'</optgroup>';
 h+='<optgroup label="All BOQ rows">'+D.boq_items.filter(i=>!known.has(i.id)).map(i=>`<option value="${esc(i.id)}"${i.id===sel?' selected':''}>${esc(i.label)}</option>`).join('')+'</optgroup>';return h}
function needsAttention(r){return r.approved_boq_id==null||r.review_status!=='reviewed'||r.flags.includes('duplicate')}
function renderRecs(){
 const f=q('rcRevFilter').value,t=q('rcRevSearch').value.toLowerCase();let rows=D.comp.records.filter(r=>{
  if(t&&!((r.eff.description||'')+' '+r.filename).toLowerCase().includes(t))return false;
  if(f==='todo')return needsAttention(r);if(f==='unreviewed')return r.review_status!=='reviewed';if(f==='nomatch')return r.approved_boq_id==null;
  if(f==='amb')return ['ambiguous','unmatched','needs_rematch'].includes(r.match.state||'');if(f==='dup')return r.flags.includes('duplicate');return true});
 const dis=canEdit()?'':' disabled';
 const keep={};document.querySelectorAll('#rcRecs .rc-rec[data-dirty]').forEach(b=>{const o={};b.querySelectorAll('[data-f],[data-boq]').forEach(i=>o[i.dataset.f||'_boq']=i.value);keep[b.dataset.id]=o});
 q('rcRecs').innerHTML=rows.length?rows.map(r=>{const e=r.eff,m=r.match||{},o=r.ocr||{};const ms=MS[m.state||'not_matched']||MS.not_matched;
  const ocrTxt=r.origin==='ocr'?`OCR draft: ref ${esc(o.ref_no||'?')} · date ${esc(o.date||'?')} · “${esc(o.description||'(nothing read)')}” · unit ${esc(o.unit||'?')} · Q.T ${o.qty??'?'} · TOTAL ${o.total??'?'} · conf ${o.conf??'n/a'}${o.date_alternatives&&o.date_alternatives.length?' (other date readings: '+esc(o.date_alternatives.join(', '))+')':''}${o.mir_status?' · box ticked: '+esc(o.mir_status):''}${o.invoice_no?' · invoice '+esc(o.invoice_no):''}${o.extra_text?' · handwriting/extra: '+esc(o.extra_text):''}${o.alts&&Object.keys(o.alts).length?' · <b>other readings:</b> '+Object.entries(o.alts).map(([k,v])=>esc(k)+' '+v.join(' / ')).join('; '):''}`:'Manually entered line (no OCR data)';
  const gs=f=>(r.user&&f in r.user)?'':((o&&o[f]!=null)?' rc-guess':'');
  return `<div class="rc-rec" data-id="${r.id}" data-doc="${r.doc_id}" data-page="${r.page}"><div class="rc-bar"><b>${esc(r.filename)}</b> p${r.page} <button class="alt" data-scan>Show scan</button> <a href="/api/recon/page?doc=${r.doc_id}&page=${r.page}" target="_blank">open full ↗</a> ${r.review_status==='reviewed'?chip('reviewed','c-ok'):chip('not reviewed','c-warn')} ${chip('match: '+ms[0],ms[1])}${m.method==='manual'?chip('manual decision','c-info'):''} ${flags(r.flags)}</div>
  <div class="rc-ocr">${ocrTxt}</div><div class="rc-scan" hidden></div>
  <div class="rc-grid"><div><label>MIR ref no.</label><input data-f="ref_no" class="${gs('ref_no')}" value="${esc(e.ref_no||'')}"${dis}></div><div><label>Date (as written)</label><input data-f="date" class="${gs('date')}" value="${esc(e.date||'')}"${dis}></div>
  <div style="grid-column:span 2"><label>Material description</label><textarea data-f="description" rows="2" class="${gs('description')}"${dis}>${esc(e.description||'')}</textarea></div>
  <div><label>Unit</label><input data-f="unit" class="${gs('unit')}" value="${esc(e.unit||'')}"${dis}></div><div><label>Q.T (quantity)</label><input data-f="qty" class="${gs('qty')}" value="${e.qty??''}"${dis}></div><div><label>TOTAL (as written)</label><input data-f="total" class="${gs('total')}" value="${e.total??''}"${dis}></div>
  <div><label>MIR status (check-box)</label><select data-f="mir_status"${dis}><option value="">— set —</option>${['A','B','C','D'].map(s=>`<option value="${s}"${e.mir_status===s?' selected':''}>${s} – ${({A:'Approved',B:'Approved with note',C:'Not approved',D:'Rejected'})[s]}</option>`).join('')}</select></div>
  <div><label>Brand (optional)</label><input data-f="brand" value="${esc(e.brand||'')}"${dis}></div>
  <div style="grid-column:span 2"><label>Matched BOQ row</label><select data-boq${dis}>${boqOpts(r)}</select></div></div>
  <div class="rc-bar"><button data-save${dis}>Save</button><button data-rev class="good"${dis}>Save &amp; mark reviewed</button><button data-tot class="alt"${dis} title="Copy the TOTAL column value into Q.T after you checked the scan">Use TOTAL as Q.T</button><button data-appr class="good"${dis}>Approve match</button><button data-rej class="alt"${dis}>Reject match</button>${r.flags.includes('duplicate')?`<button data-notdup class="alt"${dis}>Not a duplicate</button>`:`<button data-isdup class="alt"${dis} title="Do not count this line (same material submitted twice)">Exclude as duplicate</button>`}${r.origin==='manual'?`<button data-del class="alt"${dis}>Delete line</button>`:''}</div></div>`}).join(''):'<div class="sub">No MIR lines match this filter. Run OCR or add lines in step 1.</div>';
 document.querySelectorAll('#rcRecs .rc-rec').forEach(box=>{const id=box.dataset.id;const get=()=>{const o={};box.querySelectorAll('[data-f]').forEach(i=>o[i.dataset.f]=i.value);return o};
  if(keep[id]){box.querySelectorAll('[data-f],[data-boq]').forEach(i=>{const k=i.dataset.f||'_boq';if(k in keep[id])i.value=keep[id][k]});box.dataset.dirty='1'}
  box.addEventListener('input',()=>box.dataset.dirty='1');box.addEventListener('change',()=>box.dataset.dirty='1');
  const run=async(fn,ok)=>{try{await fn();msg(ok,'success');box.removeAttribute('data-dirty');await load()}catch(e){msg(e.message,'error')}};
  box.querySelector('[data-scan]').onclick=e=>{const sc=box.querySelector('.rc-scan');if(sc.hidden){sc.innerHTML='<img loading="lazy" alt="scanned MIR page" src="/api/recon/page?doc='+box.dataset.doc+'&page='+box.dataset.page+'&w=1100">';sc.hidden=false;e.target.textContent='Hide scan'}else{sc.hidden=true;sc.innerHTML='';e.target.textContent='Show scan'}};
  box.querySelector('[data-tot]').onclick=()=>{const t=box.querySelector('[data-f=total]').value;if(t==='')return msg('TOTAL is empty - read it from the scan first.','error');const qf=box.querySelector('[data-f=qty]');qf.value=t;qf.classList.remove('rc-guess');box.dataset.dirty='1'};
  box.querySelector('[data-save]').onclick=()=>run(async()=>{await act('update_record',{id,fields:get(),mark_reviewed:false});await act('match_run')},'Saved and BOQ auto-match refreshed. Check the suggested row and score; it is not approved automatically.');
  box.querySelector('[data-rev]').onclick=()=>run(async()=>{await act('update_record',{id,fields:get(),mark_reviewed:true});await act('match_run')},'Reviewed and BOQ auto-match refreshed. Check the suggested row and score; it is not approved automatically.');
  box.querySelector('[data-appr]').onclick=()=>run(()=>act('match_decide',{id,decision:'approve',boq_id:box.querySelector('[data-boq]').value}),'Match approved.');
  box.querySelector('[data-rej]').onclick=()=>run(()=>act('match_decide',{id,decision:'reject',boq_id:box.querySelector('[data-boq]').value}),'Match rejected.');
  const idp=box.querySelector('[data-isdup]');if(idp)idp.onclick=()=>run(()=>act('dup',{id,value:'duplicate'}),'Line excluded as duplicate.');
  const nd=box.querySelector('[data-notdup]');if(nd)nd.onclick=()=>run(()=>act('dup',{id,value:'not_duplicate'}),'Marked as not a duplicate.');
  const dl=box.querySelector('[data-del]');if(dl)dl.onclick=()=>{if(confirm('Delete this manual line?'))run(()=>act('delete_record',{id}),'Line deleted.')}})}
function renderRec(){
 const s=D.comp.summary;const cur=s.currency_label;
 q('rcSummary').innerHTML=`<div class="rc-cards"><div class="rc-card">Original BOQ value<b>${fmt(s.boq_total_value)}</b><span class="small">${esc(cur)} · ${s.items_valued} valued / ${s.items_unvalued} without price</span></div><div class="rc-card">Confirmed MIR value<b>${fmt(s.mir_value)}</b></div><div class="rc-card">Remaining BOQ value<b>${fmt(s.remaining_value)}</b></div><div class="rc-card">Overall financial %<b>${pc(s.fin_pct)}</b></div>
 <div class="rc-card">BOQ items<b>${s.boq_items}</b><span class="small">${s.items_with_confirmed} with confirmed MIR · ${s.items_pending_review} pending · ${s.items_without_mir} none · ${s.items_over_boq} over BOQ</span></div><div class="rc-card">MIR lines<b>${s.mir_records}</b><span class="small">${s.records_reviewed} reviewed · ${s.unmatched_mir_records} not matched · ${s.mir_docs_ocr_done}/${s.mir_docs} PDFs OCR'd</span></div><div class="rc-card">Warnings<b>${s.warnings}</b><span class="small">see “Export &amp; log”</span></div></div>`;
 const bf=q('rcBrandF'),cv=bf.value;bf.innerHTML='<option value="">All brands</option>'+D.comp.brands.filter(b=>b.items).map(b=>`<option value="${esc(b.key)}">${esc(b.brand)}</option>`).join('');bf.value=cv;
 const t=q('rcSearch').value.toLowerCase(),sf=q('rcStatF').value;
 let rows=D.comp.items.filter(x=>(!t||(x.description+' '+x.code+' '+x.part+' '+x.row_ref).toLowerCase().includes(t))&&(!bf.value||x.brand_key===bf.value)&&(!sf||x.status===sf));
 rows.sort((a,b)=>{let va=a[sortKey],vb=b[sortKey];if(va==null)va=-Infinity;if(vb==null)vb=-Infinity;return (va>vb?1:va<vb?-1:0)*sortDir});
 const H=[['code','Code'],['description','Description'],['brand','Brand'],['unit','Unit'],['boq_qty','BOQ qty'],['confirmed_qty','Confirmed MIR'],['remaining_qty','Remaining'],['unit_price','Unit price'],['boq_value','BOQ value'],['mir_value','MIR value'],['remaining_value','Remaining value'],['qty_pct','Qty %'],['fin_pct','Fin %'],['status','Status'],['n_records','Sources']];
 q('rcTable').innerHTML='<thead><tr>'+H.map(h=>`<th data-s="${h[0]}">${h[1]}${sortKey===h[0]?(sortDir>0?' ▲':' ▼'):''}</th>`).join('')+'</tr></thead><tbody>'+rows.map(x=>{const st=ST[x.status]||[x.status,''];const un=x.unit_source==='user'?x.unit:(x.unit||'');
  const src=x.sources.map(z=>`<a href="/api/recon/page?doc=${esc(D.docs.find(d=>d.filename===z.file)?.id||'')}&page=${z.page}" target="_blank">${esc(z.file)} p${z.page}</a> <span class="small">(${esc(z.ref_no||'?')}: ${fq(z.qty)} ${esc(z.unit||'')}, ${esc(z.status||'?')}${z.reviewed?'':', unreviewed'})</span>`).join('<br>');
  const na=k=>x.unavailable[k]?` title="${esc(x.unavailable[k])}"`:'';
  return `<tr><td>${esc(x.code)}<div class="small sub">${esc(x.row_ref.split('!').pop())}</div></td><td style="min-width:230px">${esc(x.description)}<div class="small sub">${esc(x.part)}</div>${flags(x.flags)}</td><td>${esc(x.brand)}</td><td><input value="${esc(un)}" placeholder="set" data-unit="${esc(x.item_id)}" style="width:62px"${canEdit()?'':' disabled'}></td><td class="num">${fq(x.boq_qty)}</td><td class="num"><b>${fq(x.confirmed_qty)}</b>${x.pending_qty?`<div class="small sub">+${fq(x.pending_qty)} unreviewed</div>`:''}${x.not_approved_qty?`<div class="small sub">${fq(x.not_approved_qty)} status C/D</div>`:''}${x.unit_conflict_qty?`<div class="small" style="color:#a00">${fq(x.unit_conflict_qty)} unit conflict</div>`:''}</td><td class="num"${x.remaining_qty<0?' style="color:#a00;font-weight:600"':''}>${fq(x.remaining_qty)}</td><td class="num">${x.unit_price==null?'<span'+na('price')+'>N/A</span>':fmt(x.unit_price)}</td><td class="num">${fmt(x.boq_value)}</td><td class="num">${fmt(x.mir_value)}</td><td class="num">${fmt(x.remaining_value)}</td><td class="num"><span${na('qty_pct')}>${pc(x.qty_pct)}</span></td><td class="num"><span${na('fin_pct')||na('price')}>${pc(x.fin_pct)}</span></td><td>${chip(st[0],st[1])}${x.awaiting_review?`<div class="small sub">${x.awaiting_review} awaiting review</div>`:''}</td><td class="small">${src||'—'}</td></tr>`}).join('')+'</tbody>';
 q('rcNote').textContent=D.comp.meta.definition_note+' Counted statuses: '+D.comp.meta.count_statuses.join(', ')+'. BOQ units are not in the workbook; set a unit per row to enable unit-conflict checks.';
 q('rcTable').querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.s;if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=1}renderRec()});
 q('rcTable').querySelectorAll('[data-unit]').forEach(i=>i.onchange=async()=>{try{await act('boq_unit',{item_id:i.dataset.unit,unit:i.value});await load()}catch(e){msg(e.message,'error')}})}
function renderBrands(){
 const brands=D.comp.brands||[];
 const cards=brands.map(b=>{
  const units=Object.entries(b.qty_by_unit||{});
  const rings=units.map(([u,v])=>{
   const boq=Number(v.boq)||0,delivered=Number(v.confirmed)||0,remaining=Math.max(boq-delivered,0);
   const rawPct=boq>0?(delivered/boq*100):null;
   const ringPct=rawPct==null?0:Math.max(0,Math.min(100,rawPct));
   const remPct=rawPct==null?null:Math.max(0,100-ringPct);
   const ringStyle=rawPct==null?'background:conic-gradient(#d9dee7 0% 100%)':`background:conic-gradient(#14805e 0% ${ringPct}%, #e6e9ef ${ringPct}% 100%)`;
   return `<div class="rc-brand-unit"><div class="rc-donut" style="${ringStyle}"><div class="rc-donut-hole"><b>${rawPct==null?'—':pc(rawPct)}</b><span>delivered</span></div></div><div class="rc-brand-unit-info"><b>${esc(u)}</b><div class="rc-brand-legend"><span><i style="background:#14805e"></i> Delivered: ${fq(delivered)}</span><span><i style="background:#e6e9ef"></i> Remaining: ${fq(remaining)}</span></div><div class="small sub">BOQ: ${fq(boq)} ${esc(u)}</div>${rawPct!=null&&rawPct>100?'<div class="small" style="color:#b42318;font-weight:700">Over BOQ by '+fq(delivered-boq)+' '+esc(u)+'</div>':''}<div class="small sub">${rawPct==null?'BOQ quantity unavailable':pc(remPct)+' remaining'}</div></div></div>`;
  }).join('');
  return `<div class="rc-brand-card"><div class="rc-brand-head"><div><b>${esc(b.brand)}</b><div class="small sub">${b.items} BOQ item(s) · ${units.length} unit type(s)</div></div><div class="rc-brand-fin"><span>Financial consumption</span><b>${pc(b.fin_pct)}</b></div></div><div class="rc-brand-rings">${rings||'<div class="sub">No usable BOQ quantities for this brand.</div>'}</div>${b.unspecified_unit_items?'<div class="small sub" style="margin-top:8px">'+b.unspecified_unit_items+' item(s) have no verified unit and are excluded from quantity totals.</div>':''}<div class="rc-brand-foot"><span>Items with confirmed delivery: <b>${b.items_with_confirmed}</b> / ${b.items}</span><span>Remaining value: <b>${fmt(b.remaining_value)}</b></span><span>Unmatched MIR lines: <b>${b.unmatched_mir_records}</b></span></div>${b.over_boq.length?'<div class="small" style="color:#b42318;margin-top:6px;font-weight:700">Over-BOQ items: '+b.over_boq.length+'</div>':''}</div>`;
 }).join('');
 q('rcBrands').innerHTML='<style>#rcBrands .rc-brand-card{border:1px solid var(--border,#d9dee7);border-radius:14px;padding:16px;margin:0 0 14px;background:var(--card-bg,#fff)}#rcBrands .rc-brand-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;border-bottom:1px solid #e6e9ef;padding-bottom:10px}#rcBrands .rc-brand-head>b,#rcBrands .rc-brand-head>div:first-child>b{font-size:16px}#rcBrands .rc-brand-fin{display:flex;gap:10px;align-items:center;font-size:12px;color:var(--muted,#667085)}#rcBrands .rc-brand-fin b{font-size:16px;color:var(--text,#101828)}#rcBrands .rc-brand-rings{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;padding-top:14px}#rcBrands .rc-brand-unit{display:flex;align-items:center;gap:12px;border:1px solid #edf0f4;border-radius:12px;padding:12px;min-width:0}#rcBrands .rc-donut{width:94px;height:94px;flex:0 0 94px;border-radius:50%;display:grid;place-items:center}#rcBrands .rc-donut-hole{width:70px;height:70px;border-radius:50%;background:var(--card-bg,#fff);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}#rcBrands .rc-donut-hole b{font-size:15px;line-height:1.2}#rcBrands .rc-donut-hole span{font-size:10px;color:var(--muted,#667085)}#rcBrands .rc-brand-unit-info{min-width:0;overflow-wrap:anywhere}#rcBrands .rc-brand-legend{display:flex;flex-direction:column;gap:4px;font-size:12px;margin:5px 0}#rcBrands .rc-brand-legend span{display:flex;align-items:center;gap:5px}#rcBrands .rc-brand-legend i{display:inline-block;width:9px;height:9px;border-radius:50%;flex:0 0 9px}#rcBrands .rc-brand-foot{display:flex;gap:8px 18px;flex-wrap:wrap;border-top:1px solid #e6e9ef;margin-top:12px;padding-top:10px;font-size:12px;color:var(--muted,#667085)}@media(max-width:600px){#rcBrands .rc-brand-unit{align-items:flex-start}#rcBrands .rc-donut{width:78px;height:78px;flex-basis:78px}#rcBrands .rc-donut-hole{width:58px;height:58px}#rcBrands .rc-donut-hole b{font-size:12px}}</style>'+cards+'<div class="sub" style="margin-top:6px">Each circle is calculated separately per unit. Green = confirmed delivered; grey = remaining BOQ quantity. Only reviewed, approved MIR quantities are counted. Values may be unavailable when BOQ prices are missing.</div>';
}
function renderExp(){
 q('rcExc').innerHTML=D.comp.exceptions.length?'<div class="rc-scroll" style="max-height:300px"><table class="rc"><thead><tr><th>Severity</th><th>Type</th><th>Message</th></tr></thead><tbody>'+D.comp.exceptions.slice(0,300).map(e=>`<tr><td>${chip(e.severity,e.severity==='error'?'c-bad':e.severity==='warning'?'c-warn':'c-info')}</td><td>${esc(e.type)}</td><td>${esc(e.message)}</td></tr>`).join('')+'</tbody></table></div>':'<div class="sub">None.</div>';
 q('rcAudit').innerHTML=D.audit.length?D.audit.map(a=>`${esc(a.ts)} · <b>${esc(a.user)}</b> · ${esc(a.action)} · ${esc(a.detail)}`).join('<br>'):'No actions yet.'}
function tab(t){document.querySelectorAll('#rcTabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));document.querySelectorAll('#reconPanel .rc-pane').forEach(p=>p.classList.toggle('on',p.id==='rcP-'+t))}
q('rcTabs').onclick=e=>{const b=e.target.closest('button');if(b)tab(b.dataset.t)};
['rcRevFilter','rcRevSearch'].forEach(i=>q(i).oninput=()=>D&&renderRecs());['rcSearch','rcBrandF','rcStatF'].forEach(i=>q(i).oninput=()=>D&&renderRec());
async function upload(kind,file){const fd=new FormData();fd.append('kind',kind);fd.append('file',file);return api('/api/recon/upload',{method:'POST',body:fd})}
q('rcBoqUp').onclick=async()=>{const f=q('rcBoqFile').files[0];if(!f)return msg('Choose the BOQ .xlsx first.','error');try{msg('Reading workbook...','info');const j=await upload('boq',f);msg('BOQ loaded: '+j.items+' items from sheet “'+j.sheet+'”.','success');q('rcBoqFile').value='';await load()}catch(e){msg(e.message,'error')}};
q('rcMirUp').onclick=async()=>{const fs=[...q('rcMirFiles').files];if(!fs.length)return msg('Choose one or more MIR PDFs.','error');let ok=0,bad=[];for(const f of fs){try{msg('Uploading '+f.name+'...','info');await upload('mir',f);ok++}catch(e){bad.push(f.name+': '+e.message)}}msg(ok+' PDF(s) added.'+(bad.length?' Skipped: '+bad.join(' | '):''),bad.length?'error':'success');q('rcMirFiles').value='';await load()};
async function loadPick(){try{const j=await api('/api/project_documents');const rows=(j.documents||[]).filter(d=>/\.(pdf|xlsx|xlsm)$/i.test(d.filename));q('rcDocPick').innerHTML=rows.length?rows.map(d=>`<label style="display:block"><input type="checkbox" data-pick="${esc(d.id)}"> ${esc(d.filename)} <span class="small sub">(${esc(d.category_label||d.category)})</span></label>`).join(''):'No PDF/Excel documents saved yet.'}catch(e){q('rcDocPick').textContent=e.message}}
q('rcImport').onclick=async()=>{const ids=[...document.querySelectorAll('[data-pick]:checked')].map(x=>x.dataset.pick);if(!ids.length)return msg('Tick at least one saved document.','error');try{const j=await act('import_docs',{doc_ids:ids});msg('Imported '+j.imported+' file(s).'+(j.skipped.length?' Skipped: '+j.skipped.join(' | '):''),j.skipped.length?'error':'success');await load()}catch(e){msg(e.message,'error')}};
q('rcMatch').onclick=async()=>{try{const j=await act('match_run');const n=j.result;msg(`Matching done: ${n.exact} exact, ${n.suggested} suggested, ${n.ambiguous} ambiguous, ${n.unmatched} unmatched (${n.kept} approved kept). Nothing is counted until you approve.`,'success');await load()}catch(e){msg(e.message,'error')}};
q('rcApproveExact').onclick=async()=>{if(!confirm('Approve every match classed as “exact”? Review status is unchanged and the action is logged.'))return;try{const j=await act('approve_exact');msg(j.approved+' exact match(es) approved.','success');await load()}catch(e){msg(e.message,'error')}};
q('rcExport').onclick=async()=>{try{q('rcExpMsg').textContent='Generating...';const j=await act('export');q('rcExpMsg').textContent='Ready: '+j.filename;location.href='/api/recon/download?name='+encodeURIComponent(j.filename)}catch(e){q('rcExpMsg').textContent=e.message}};
document.querySelectorAll('.navItem[data-view=docs]').forEach(b=>b.addEventListener('click',()=>{load();loadPick()}));
const ps=q('projectSelect');if(ps)ps.addEventListener('change',()=>setTimeout(()=>{load();loadPick()},800));
setTimeout(()=>{load();loadPick()},900);
})();
'''


def inject(index_html):
    """Insert the panel into the Documents view and append the script. Returns input unchanged if anchors are missing."""
    a1 = '<div id="projectDocsList"></div></div>\n</div></section>'
    if a1 not in index_html or '</body>' not in index_html:
        print('[RECON] UI anchors not found - reconciliation panel not injected.')
        return index_html
    out = index_html.replace(a1, '<div id="projectDocsList"></div></div>\n' + RECON_HTML + '\n</div></section>', 1)
    i = out.rfind('</body>')
    return out[:i] + '<script>' + RECON_JS + '</script>' + out[i:]
