# -*- coding: utf-8 -*-
"""HTML + JS for the BOQ vs MIR panel (injected into the existing Documents view)."""

RECON_HTML = r'''
<style>
#reconPanel{--rc-ink:#172b4d;--rc-muted:#667085;--rc-line:#e4e9f0;--rc-soft:#f7f9fc;color:var(--text,#172b4d)}
#reconPanel .cardHead{align-items:flex-start;gap:14px;padding-bottom:16px;border-bottom:1px solid var(--rc-line)}
#reconPanel .cardHead h3{font-size:22px;letter-spacing:-.45px;margin:0 0 6px}
#reconPanel .cardHead .sub{max-width:760px;line-height:1.55}
#reconPanel #rcBadge{white-space:nowrap;border-radius:999px;padding:7px 11px}
#reconPanel .rc-tabs{display:flex;gap:7px;flex-wrap:wrap;margin:18px 0 14px;padding:5px;background:rgba(247,249,252,.97);border:1px solid var(--rc-line);border-radius:13px;position:sticky;top:76px;z-index:120;box-shadow:0 6px 18px rgba(16,24,40,.09);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);scroll-margin-top:12px}
#reconPanel .rc-tabs button{background:transparent;color:#475467;border:1px solid transparent;border-radius:9px;padding:9px 13px;font-weight:650}
#reconPanel .rc-tabs button:hover{background:#edf2f8}
#reconPanel .rc-tabs button.on{background:#203c65;color:#fff;border-color:#203c65;box-shadow:0 2px 5px #203c651c}
#reconPanel .rc-pane{display:none}#reconPanel .rc-pane.on{display:block}
#reconPanel .rc-section-intro{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin:4px 0 16px}
#reconPanel .rc-section-intro h4{font-size:16px;margin:0 0 5px}
#reconPanel .rc-section-intro p{font-size:12.5px;line-height:1.55;color:var(--rc-muted);margin:0}
#reconPanel .rc-upload-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:14px;margin:14px 0}
#reconPanel .rc-upload-card{border:1px solid var(--rc-line);border-radius:14px;padding:17px;background:linear-gradient(180deg,#fff,#fbfcfe);min-width:0}
#reconPanel .rc-upload-top{display:flex;align-items:center;gap:11px;margin-bottom:12px}
#reconPanel .rc-upload-icon{width:40px;height:40px;display:grid;place-items:center;border-radius:12px;background:#edf3ff;color:#3157a4;font-size:16px;font-weight:800;flex:0 0 40px}
#reconPanel .rc-upload-top h4{font-size:15px;margin:0 0 3px}
#reconPanel .rc-upload-top p{font-size:12px;color:var(--rc-muted);margin:0;line-height:1.4}
#reconPanel .rc-file-row{display:flex;flex-direction:column;gap:10px}
#reconPanel .rc-file-row input[type=file]{width:100%;max-width:100%;box-sizing:border-box;font-size:12px;padding:9px;border:1px dashed #cbd5e1;border-radius:9px;background:#fff}
#reconPanel .rc-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0}
#reconPanel .rc-bar input:not([type=file]),#reconPanel .rc-bar select{min-height:38px}
#reconPanel button{border-radius:9px;font-weight:600;transition:filter .15s ease}
#reconPanel button:hover{filter:brightness(.97)}
#reconPanel button:disabled{opacity:.55;cursor:not-allowed}
#reconPanel input,#reconPanel select,#reconPanel textarea{border:1px solid #d0d7e2;border-radius:8px;padding:8px 9px;background:var(--input-bg,#fff);color:inherit}
#reconPanel input:focus,#reconPanel select:focus,#reconPanel textarea:focus{outline:2px solid #9ab8f7;outline-offset:1px;border-color:#6389d7}
#reconPanel .rc-source-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(285px,1fr));gap:12px;margin-top:10px}
#reconPanel .rc-source-card{border:1px solid var(--rc-line);border-radius:13px;padding:14px;background:var(--card-bg,#fff);min-width:0}
#reconPanel .rc-source-head{display:flex;align-items:flex-start;gap:10px}
#reconPanel .rc-file-icon{width:38px;height:42px;display:grid;place-items:center;flex:0 0 38px;border-radius:9px;background:#fff0ed;color:#b54735;font-size:11px;font-weight:800;border:1px solid #f8d8d1}
#reconPanel .rc-source-name{font-weight:700;font-size:13px;overflow-wrap:anywhere;line-height:1.45}
#reconPanel .rc-source-meta{font-size:11px;color:var(--rc-muted);margin-top:4px}
#reconPanel .rc-source-stats{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}
#reconPanel .rc-stat-pill{border-radius:7px;padding:5px 8px;background:var(--rc-soft);font-size:11px;color:#475467}
#reconPanel .rc-source-actions{display:flex;gap:6px;flex-wrap:wrap;border-top:1px solid #eef1f5;padding-top:10px}
#reconPanel .rc-source-actions button,#reconPanel .rc-source-actions a{font-size:11px;padding:7px 9px}
#reconPanel .rc-boq-summary{border:1px solid #cbdcf8;background:#f5f8ff;border-radius:13px;padding:15px;margin:8px 0 18px}
#reconPanel .rc-boq-file{font-size:15px;font-weight:750;overflow-wrap:anywhere}
#reconPanel .rc-boq-meta{font-size:12px;color:#52627a;margin-top:5px;line-height:1.6}
#reconPanel .rc-doc-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:22px 0 8px}
#reconPanel .rc-doc-heading h4{margin:0;font-size:16px}
#reconPanel .rc-empty{border:1px dashed #cbd5e1;background:var(--rc-soft);border-radius:12px;padding:22px;text-align:center;color:var(--rc-muted);font-size:13px;line-height:1.6}
#reconPanel table.rc{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px}
#reconPanel table.rc th{background:#f0f3f8;color:#344054;padding:10px 9px;text-align:left;position:sticky;top:0;cursor:pointer;white-space:nowrap;border-bottom:1px solid #dfe5ed}
#reconPanel table.rc td{padding:9px;border-bottom:1px solid #edf0f4;vertical-align:top}
#reconPanel table.rc tbody tr:hover{background:#fafbfd}
#reconPanel .num{text-align:right;font-variant-numeric:tabular-nums}
#reconPanel .rc-chip{display:inline-block;padding:3px 8px;border-radius:999px;font-size:10.5px;font-weight:650;margin:2px 3px 2px 0;background:#edf0f5;color:#344054}
#reconPanel .c-ok{background:#e0f5e9;color:#067647}#reconPanel .c-warn{background:#fff4d6;color:#8a6100}
#reconPanel .c-bad{background:#fee4e2;color:#b42318}#reconPanel .c-info{background:#e8efff;color:#284da0}#reconPanel .c-mute{background:#f0f1f3;color:#596273}
#reconPanel .rc-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:10px 0}
#reconPanel .rc-card{background:#fff;border:1px solid var(--rc-line);border-radius:12px;padding:14px;box-shadow:0 1px 2px #10182808}
#reconPanel .rc-card b{display:block;font-size:21px;margin-top:5px;letter-spacing:-.4px}
#reconPanel .rc-rec{border:1px solid var(--rc-line);border-radius:13px;padding:14px;margin:12px 0;background:var(--card-bg,#fff);box-shadow:0 1px 2px #10182808}
#reconPanel .rc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:10px}
#reconPanel .rc-grid label{font-size:11px;color:#667085;display:block;margin-bottom:4px;font-weight:650}
#reconPanel .rc-grid input,#reconPanel .rc-grid select,#reconPanel .rc-grid textarea{width:100%;box-sizing:border-box}
#reconPanel .rc-ocr{font-size:11px;color:#667085;line-height:1.55;margin:8px 0 12px;padding:9px 11px;background:#f8fafc;border-radius:8px}
#reconPanel .rc-guess{background:#fff8e1;border-color:#e0a800!important}
#reconPanel .rc-scan img{max-width:100%;border:1px solid #cfd6e2;border-radius:8px;margin-top:8px}
#reconPanel .rc-scroll{overflow:auto;max-height:620px;border:1px solid var(--rc-line);border-radius:10px}
#reconPanel #rcMsg{border-radius:9px;margin:10px 0;padding:8px 11px;min-height:16px}
#reconPanel .advanced{border:1px solid var(--rc-line);border-radius:10px;padding:11px 13px;background:#fafbfd;margin:10px 0}
#reconPanel .advanced summary{cursor:pointer;font-size:12px;font-weight:650;color:#475467}
@media(max-width:720px){#reconPanel .rc-upload-grid{grid-template-columns:1fr}#reconPanel .cardHead{flex-direction:column}#reconPanel .rc-tabs button{flex:1 1 auto;padding:8px 10px}#reconPanel .rc-source-list{grid-template-columns:1fr}#reconPanel .rc-rec{padding:10px}}
#rcBrands .rc-brand-card{border:1px solid var(--border,#d9dee7);border-radius:14px;padding:16px;margin:0 0 14px;background:var(--card-bg,#fff)}
#rcBrands .rc-brand-head{display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid #e6e9ef;padding-bottom:12px}
#rcBrands .rc-brand-head>div>b{font-size:17px}
#rcBrands .rc-brand-tag{font-size:11px;padding:5px 9px;border-radius:20px;background:#eef3f8;color:#475467}
#rcBrands .rc-brand-section{padding:13px 0;border-bottom:1px solid #e6e9ef}
#rcBrands .rc-brand-section-title{display:flex;align-items:center;gap:9px;font-weight:700;font-size:14px;margin-bottom:10px}
#rcBrands .rc-brand-section-title span{font-size:11px;font-weight:400;color:var(--muted,#667085)}
#rcBrands .rc-brand-quantity{display:block}
#rcBrands .rc-brand-rings{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px;min-width:0}
#rcBrands .rc-brand-unit{display:flex;align-items:center;gap:14px;border:1px solid #edf0f4;border-radius:12px;padding:12px;min-width:0}
#rcBrands .rc-donut{width:112px;height:112px;flex:0 0 112px;display:grid;place-items:center;position:relative;border-radius:50%;isolation:isolate}
#rcBrands .rc-donut-svg{position:absolute;inset:0;width:100%;height:100%;display:block;overflow:visible;z-index:0}
#rcBrands .rc-donut-hole{position:relative;z-index:1;width:78px;height:78px;border:1px solid rgba(217,222,231,.75);border-radius:50%;background:var(--card-bg,#fff);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;pointer-events:none}
#rcBrands .rc-donut-hole b{font-size:17px;line-height:1.2;font-variant-numeric:tabular-nums}
#rcBrands .rc-donut-hole span{font-size:10px;color:var(--muted,#667085)}
#rcBrands .rc-brand-unit-info{min-width:0;overflow-wrap:anywhere}
#rcBrands .rc-brand-legend{display:flex;flex-direction:column;gap:5px;font-size:12px;margin:0 0 6px}
#rcBrands .rc-brand-fin-section{border-bottom:0;padding-bottom:3px}
#rcBrands .rc-brand-section-title>b{margin-inline-start:auto;font-size:16px;font-variant-numeric:tabular-nums}
#rcBrands .rc-brand-fin-track{height:10px;border-radius:20px;background:#e6e9ef;overflow:hidden}
#rcBrands .rc-brand-fin-track>div{height:100%;background:#2874bd;border-radius:20px}
#rcBrands .rc-brand-fin-caption{display:flex;justify-content:space-between;gap:8px;margin:6px 0;font-size:11px;color:var(--muted,#667085)}
#rcBrands .rc-brand-foot{display:flex;gap:8px 20px;flex-wrap:wrap;border-top:1px solid #e6e9ef;margin-top:12px;padding-top:10px;font-size:12px;color:var(--muted,#667085)}
#rcBrands .rc-brand-warning{font-size:12px;color:#b42318;font-weight:700;margin-top:7px}
#rcSummary .rc-summary-rings{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:10px 0 18px}
#rcSummary .rc-summary-ring{display:flex;align-items:center;gap:12px;border:1px solid var(--rc-line);border-radius:14px;padding:12px;background:var(--card-bg,#fff);min-width:0}
#rcSummary .rc-summary-ring .rc-donut{width:94px;height:94px;flex:0 0 94px;display:grid;place-items:center;position:relative;border-radius:50%;isolation:isolate}
#rcSummary .rc-summary-ring .rc-donut-svg{position:absolute;inset:0;width:100%;height:100%;display:block;z-index:0}
#rcSummary .rc-summary-ring .rc-donut-hole{position:relative;z-index:1;width:66px;height:66px;border:1px solid rgba(217,222,231,.75);border-radius:50%;background:var(--card-bg,#fff);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;pointer-events:none}
#rcSummary .rc-summary-ring .rc-donut-hole b{font-size:14px}
#rcSummary .rc-summary-info{min-width:0;overflow-wrap:anywhere}
#rcSummary .rc-summary-info b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
#rcSummary .rc-summary-info span{font-size:11px;color:var(--rc-muted)}
@media(max-width:650px){#rcBrands .rc-brand-unit{align-items:flex-start}#rcBrands .rc-donut{width:92px;height:92px;flex-basis:92px}#rcBrands .rc-donut-hole{width:66px;height:66px}}

/* Refined workspace: calm slate + teal, compact hierarchy, consistent controls */
#reconPanel{--rc-ink:#172b4d;--rc-muted:#667085;--rc-line:#e5eaf0;--rc-soft:#f6f8fb;direction:rtl;text-align:right;color:#26364a;background:#fbfcfe;border:1px solid #e8edf3;border-radius:16px;box-shadow:0 8px 28px rgba(20,38,60,.045);padding:18px}
#reconPanel .cardHead{padding:4px 2px 14px;border-bottom:1px solid #e9eef4}
#reconPanel .cardHead h3{font-size:20px;font-weight:750;color:#1e344d;letter-spacing:0;margin:0 0 3px}
#reconPanel .cardHead .sub{display:none}
#reconPanel #rcBadge{font-size:11px;background:#edf5f4;color:#27766d;border:1px solid #dcebe8}
#reconPanel .rc-tabs{top:68px;gap:5px;padding:4px;margin:13px 0 16px;background:#f3f6f9;border:1px solid #e8edf2;border-radius:11px;box-shadow:none}
#reconPanel .rc-tabs button{font-size:12px;padding:8px 12px;color:#526174;border-radius:8px}
#reconPanel .rc-tabs button.on{background:#285c62;border-color:#285c62;color:#fff;box-shadow:none}
#reconPanel .rc-section-intro{margin:0 0 10px}
#reconPanel .rc-section-intro h4,#reconPanel .rc-doc-heading h4{font-size:14px;font-weight:750;color:#263b52}
#reconPanel .rc-section-intro p{display:none}
#reconPanel .rc-upload-grid{gap:11px;margin:10px 0 14px}
#reconPanel .rc-upload-card{border-color:#e6ebf1;border-radius:12px;padding:14px;background:#fff;box-shadow:0 2px 8px rgba(30,50,70,.025)}
#reconPanel .rc-upload-icon{width:36px;height:36px;flex-basis:36px;border-radius:10px;background:#edf4f4;color:#326b70;font-size:12px}
#reconPanel .rc-upload-top{margin-bottom:10px}
#reconPanel .rc-upload-top h4{font-size:13px;color:#263b52}
#reconPanel .rc-upload-top p{display:none}
#reconPanel .rc-file-row input[type=file]{font-size:11px;background:#fafbfd;border-color:#dce3eb}
#reconPanel .rc-bar{gap:7px;margin:9px 0}
#reconPanel button{border-radius:8px;font-size:12px;padding:8px 12px;box-shadow:none}
#reconPanel button:not(.alt):not(.good){background:#285c62;border-color:#285c62;color:white}
#reconPanel button.alt{background:#f3f6f8;border:1px solid #e2e8ee;color:#47586a}
#reconPanel button.good{background:#e8f3ef;border:1px solid #d5e9e1;color:#24725d}
#reconPanel input,#reconPanel select,#reconPanel textarea{border-color:#dce3eb;border-radius:8px;padding:8px 10px;font-size:12px}
#reconPanel input:focus,#reconPanel select:focus,#reconPanel textarea:focus{outline:2px solid #c5dddd;outline-offset:0;border-color:#75a6a5}
#reconPanel .rc-source-list{gap:10px}
#reconPanel .rc-source-card{border-color:#e6ebf1;border-radius:12px;padding:13px;background:#fff;box-shadow:0 2px 8px rgba(30,50,70,.025)}
#reconPanel .rc-source-name{font-size:12px;color:#263b52}
#reconPanel .rc-source-meta{font-size:10.5px;line-height:1.5}
#reconPanel .rc-stat-pill{background:#f3f6f8;color:#526174;border-radius:6px}
#reconPanel .rc-boq-summary{border:1px solid #e0e9ed;background:#f5f9f9;border-radius:11px;padding:13px}
#reconPanel .rc-empty{border-color:#dfe6ed;background:#f8fafc;border-radius:11px;padding:20px}
#reconPanel table.rc{font-size:11.5px}
#reconPanel table.rc th{background:#f3f6f8;color:#405267;padding:9px 8px;border-bottom-color:#e2e8ee}
#reconPanel table.rc td{padding:8px;border-bottom-color:#eef1f4}
#reconPanel table.rc tbody tr:hover{background:#f7fafb}
#reconPanel .rc-chip{font-size:10px;padding:3px 7px;border-radius:6px;font-weight:650}
#reconPanel .c-ok{background:#e8f3ee;color:#28745d}
#reconPanel .c-warn{background:#fbf2df;color:#89651c}
#reconPanel .c-bad{background:#faeae8;color:#a94d45}
#reconPanel .c-info{background:#eaf1f5;color:#456b80}
#reconPanel .rc-card,#reconPanel .rc-rec{border-color:#e6ebf1;border-radius:12px;box-shadow:0 2px 8px rgba(30,50,70,.025)}
#reconPanel .rc-rec{padding:13px;margin:10px 0;background:#fff}
#reconPanel .rc-grid{gap:9px}
#reconPanel .rc-grid label{font-size:11px;color:#5d6c7d}
#reconPanel .rc-ocr{font-size:11px;line-height:1.6;background:#f6f8fa;color:#657487;border:1px solid #edf1f4;border-radius:8px;padding:8px 10px}
#reconPanel .rc-scroll{border-color:#e5eaf0;border-radius:10px}
#reconPanel #rcMsg{font-size:12px;min-height:0;padding:8px 10px}
#reconPanel .advanced,#reconPanel .rc-advanced{border-color:#e6ebf1;border-radius:9px;background:#f8fafb}
#reconPanel .advanced summary,#reconPanel .rc-advanced summary{font-size:11px;color:#596b7e}
#reconPanel .rc-doc-heading{margin:18px 0 8px}
#reconPanel .rc-doc-heading .small{font-size:10px}
#reconPanel .num{font-variant-numeric:tabular-nums}
#reconPanel .rc-source-actions{border-top-color:#edf1f4}
#reconPanel a{color:#326b78}
#reconPanel .rc-summary-ring{border-color:#e7edf1;border-radius:12px;background:#fff}
#reconPanel .rc-summary-ring .rc-donut-hole b{color:#294b57}
#reconPanel .rc-brand-card{border-color:#e6ebf1;border-radius:12px;box-shadow:0 2px 8px rgba(30,50,70,.025)}
@media(max-width:720px){#reconPanel{padding:12px}#reconPanel .rc-tabs{top:58px}#reconPanel .rc-tabs button{font-size:11px;padding:8px 9px}}

</style>
<div class="card" id="reconPanel" style="margin-top:16px">
 <div class="cardHead"><div><h3>متابعة الخامات والتوريدات</h3></div><span id="rcBadge" class="badge neutral">Loading</span></div>
 <div class="rc-tabs" id="rcTabs"><button data-t="src" class="on">الملفات</button><button data-t="rev">مراجعة MIR</button><button data-t="rec">ملخص المقايسة</button><button data-t="brd">تقدم البراندات</button><button data-t="exp">التقارير وسجل المراجعة</button></div>
 <div id="rcMsg" class="status" role="status">&nbsp;</div>
 <div class="rc-pane on" id="rcP-src">
  <div class="rc-section-intro"><div><h4>ملفات المشروع</h4></div><div id="rcOcr" class="sub"></div></div>
  <div class="rc-upload-grid">
   <div class="rc-upload-card"><div class="rc-upload-top"><div class="rc-upload-icon">XLS</div><div><h4>ملف المقايسة BOQ</h4></div></div><div class="rc-file-row"><input id="rcBoqFile" type="file" accept=".xlsx,.xlsm"><button id="rcBoqUp">رفع المقايسة</button></div></div>
   <div class="rc-upload-card"><div class="rc-upload-top"><div class="rc-upload-icon">PDF</div><div><h4>مستندات MIR</h4></div></div><div class="rc-file-row"><input id="rcMirFiles" type="file" accept=".pdf" multiple><button id="rcMirUp">رفع MIR</button></div></div>
   <div class="rc-upload-card"><div class="rc-upload-top"><div class="rc-upload-icon">INV</div><div><h4>فواتير التوريد</h4></div></div><div class="rc-file-row"><input id="rcInvoiceFiles" type="file" accept=".pdf" multiple><button id="rcInvoiceUp">رفع الفواتير</button><button id="rcImportRepoInvoices" class="alt">استيراد فواتير المشروع</button><button id="rcImportRepoDocs" class="alt">استيراد ملفات المشروع</button></div></div>
  </div>
  <details class="advanced"><summary>ملفات محفوظة بالمشروع</summary><div class="advInner"><div id="rcDocPick" class="sub">Loading...</div><div class="rc-bar"><button id="rcImport" class="alt">Import selected files</button></div></div></details>
  <div class="rc-doc-heading"><h4>المقايسة الحالية</h4><span class="small sub">Excel reference</span></div><div id="rcBoqInfo" class="sub">No BOQ yet.</div>
  <div class="rc-doc-heading"><h4>ملفات MIR والفواتير</h4><span id="rcMirCount" class="small sub">0 documents</span></div><div id="rcDocs"></div>
 </div>
 <div class="rc-pane" id="rcP-rev">
  <div class="rc-bar"><button id="rcMatch">مطابقة تلقائية</button><button id="rcApproveExact" class="good">اعتماد المطابقات الدقيقة</button>
   <select id="rcRevFilter"><option value="all">كل السطور</option><option value="todo">تحتاج مراجعة</option><option value="unreviewed">غير مراجعة</option><option value="nomatch">بلا مطابقة معتمدة</option><option value="amb">مطابقة غير محسومة</option><option value="dup">مكرر</option></select>
   <input id="rcRevSearch" placeholder="بحث بالوصف أو الملف" style="min-width:200px"></div>
  
  <div id="rcRecs"></div>
 </div>
 <div class="rc-pane" id="rcP-rec">
  <div id="rcSummary"></div>
  <div class="rc-bar"><input id="rcSearch" placeholder="بحث في المقايسة" style="min-width:220px"><select id="rcBrandF"><option value="">كل البراندات</option></select>
   <select id="rcStatF"><option value="">كل الحالات</option><option value="complete">مكتمل</option><option value="partial">جزئي</option><option value="pending_review">قيد المراجعة</option><option value="no_mir">بدون MIR</option><option value="over_boq">تجاوز المقايسة</option><option value="no_confirmed">بلا كمية معتمدة</option></select></div>
  <div class="rc-scroll"><table class="rc" id="rcTable"></table></div>
  <div class="sub" id="rcNote" style="margin-top:8px"></div>
 </div>
 <div class="rc-pane" id="rcP-brd"><div id="rcBrands"></div></div>
 <div class="rc-pane" id="rcP-exp">
  <div class="rc-bar"><button id="rcExport" class="good">تصدير Excel</button><span id="rcExpMsg" class="sub"></span></div>
  <h4 style="margin:12px 0 6px">الاستثناءات</h4><div id="rcExc"></div>
  <h4 style="margin:12px 0 6px">سجل العمليات</h4><div id="rcAudit" class="small sub"></div>
 </div>
</div>
'''

RECON_JS = r'''
(function(){
const q=id=>document.getElementById(id);if(!q('reconPanel'))return;
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const dedupeRepeatedWords=s=>{let w=String(s??'').trim().split(/\s+/).filter(Boolean),changed=true;while(changed){changed=false;for(let n=Math.floor(w.length/2);n>=3&&!changed;n--){for(let i=0;i+2*n<=w.length;i++){const norm=a=>a.map(x=>x.toLowerCase().replace(/[^\p{L}\p{N}]/gu,'')).join(' ');if(norm(w.slice(i,i+n))===norm(w.slice(i+n,i+2*n))){w.splice(i+n,n);changed=true;break}}}}return w.join(' ')};
const invoiceQualitySet=new Set(['invoice_math_mismatch_review','invoice_math_incomplete_review','invoice_raw_value_mismatch','invoice_possible_zero_scale_error','line_math_review_required','invoice_visual_confirmation_required']);
const invoiceNumber=v=>{if(v==null||String(v).trim()==='')return null;let s=String(v).replace(/[٠-٩۰-۹]/g,ch=>String(ch.charCodeAt(0)-(ch.charCodeAt(0)>=0x06f0?0x06f0:0x0660))).replace(/[٬،,]/g,'').replace(/٫/g,'.').replace(/[\s\u00a0]/g,'').replace(/[^0-9.+-]/g,'').replace(/[.]+$/,'');if(!s||!/[0-9]/.test(s))return null;const n=Number(s);return Number.isFinite(n)?n:null};
const invoiceQualityFlags=(r,e=r.eff||{},o=r.ocr||{})=>{const raw=o.raw_values||o.raw||{};const rawFor=k=>raw[k]??(k==='line_total'?raw.total:undefined);const calc=invoiceNumber(e.qty)!=null&&invoiceNumber(e.unit_price)!=null?invoiceNumber(e.qty)*invoiceNumber(e.unit_price):null;const total=invoiceNumber(e.total);const flags=[];if(calc==null||total==null)flags.push('invoice_math_incomplete_review');else if(Math.abs(calc-total)>Math.max(.5,Math.abs(total)*.001))flags.push('invoice_math_mismatch_review');if(r.origin==='ocr'&&r.review_status!=='reviewed')flags.push('invoice_visual_confirmation_required');for(const k of ['qty','unit_price','line_total']){const rv=invoiceNumber(rawFor(k)),cv=invoiceNumber(k==='line_total'?e.total:e[k]);if(rv==null||cv==null||rv===0||Math.abs(rv-cv)<=Math.max(.0001,Math.abs(rv)*.000001))continue;flags.push('invoice_raw_value_mismatch');const ratio=Math.max(Math.abs(rv),Math.abs(cv))/Math.min(Math.abs(rv),Math.abs(cv));if([10,100,1000].some(n=>Math.abs(ratio-n)<.02))flags.push('invoice_possible_zero_scale_error')}return [...new Set(flags)]};
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
 const s=D.comp.summary;q('rcBadge').textContent=D.boq?`${s.boq_items} BOQ items · ${s.mir_docs} MIR PDFs · ${s.invoice_docs||0} invoice PDFs`:'No BOQ';q('rcBadge').className='badge'+(D.boq?'':' neutral');
 q('rcOcr').innerHTML=D.ocr.available?chip('OCR: '+D.ocr.backend+' ('+(D.ocr.langs||[]).join(', ')+')','c-ok')+' <span class="small">OCR output is a draft only; handwriting and check-boxes are often misread.</span>':chip('OCR unavailable','c-bad')+' '+esc(D.ocr.message);
 const b=D.boq;
 q('rcBoqInfo').innerHTML=b
  ? '<div class="rc-boq-summary"><div class="rc-boq-file">▤ '+esc(b.filename)+'</div><div class="rc-boq-meta">Sheet: <b>'+esc(b.sheet)+'</b> · Header row: '+b.header_row+' · <b>'+b.items+'</b> BOQ items<br>Columns detected: '+Object.entries(b.columns).map(([k,v])=>esc(k)+' → '+esc(v)).join(' · ')+'<br>'+(b.sheets_considered||[]).map(x=>esc(x.sheet)+(x.header_row?' (score '+x.score+')':' (no BOQ header)')).join(' · ')+'</div>'+b.warnings.filter(w=>w.severity!=='info').slice(0,6).map(w=>'<div class="small" style="color:#9a6700;margin-top:5px">⚠ '+esc(w.message)+'</div>').join('')+'</div>'
  : '<div class="rc-empty">No ملف المقايسة BOQ uploaded yet.<br>Upload the contract Excel file above to begin reconciliation.</div>';
 q('rcMirCount').textContent=D.docs.length+' PDF document(s) · '+D.docs.reduce((n,d)=>n+d.pages,0)+' pages';
 q('rcDocs').innerHTML=D.docs.length
  ? '<div class="rc-source-list">'+D.docs.map(d=>{
    const n=D.comp.records.filter(r=>r.doc_id===d.id).length;
    const isInvoice=d.kind==='invoice';
    let status=d.ocr_status==='done'?chip('OCR complete','c-ok')
      :d.ocr_status==='running'?chip('OCR running '+((d.ocr_progress&&d.ocr_progress.page)||0)+'/'+d.pages,'c-info')
      :d.ocr_status==='error'?chip('OCR failed','c-bad')+'<div class="small" style="color:#b42318;margin-top:5px">'+esc(d.ocr_error||'Please run OCR again.')+'</div>'
      :d.ocr_status==='interrupted'?chip('Interrupted · retry OCR','c-warn')
      :chip('OCR not started','c-mute');
    const invoiceRows=D.comp.records.filter(r=>r.doc_id===d.id&&r.doc_kind==='invoice');
    const qualityBad=invoiceRows.filter(r=>invoiceQualityFlags(r).length>0).length;
    const invoiceMeta=d.invoice_document_meta||{};
    const metaText=isInvoice?'<div class="rc-source-meta">رقم الفاتورة: '+esc(invoiceMeta.invoice_no||'?')+' · أمر التوريد: '+esc(invoiceMeta.supply_order_no||'?')+' · العميل: '+esc(invoiceMeta.customer||'?')+' · المورد: '+esc(invoiceMeta.supplier||'?')+' · التاريخ: '+esc(invoiceMeta.date||'?')+' · الإجمالي المطبوع: '+esc(invoiceMeta.printed_grand_total??'غير مؤكد')+'</div>':'';
    const qualityText=isInvoice&&invoiceRows.length?'<div class="rc-source-meta" style="color:'+(qualityBad?'#b42318':'#067647')+'">فحص الأرقام: '+(invoiceRows.length-qualityBad)+' سليم مبدئيًا · '+qualityBad+' يحتاج مراجعة (لا يتم التصحيح تلقائيًا)</div>':'';
    return '<div class="rc-source-card"><div class="rc-source-head"><div class="rc-file-icon">'+(isInvoice?'INV':'PDF')+'</div><div style="min-width:0;flex:1"><div class="rc-source-name">'+esc(d.filename)+'</div><div class="rc-source-meta">'+(isInvoice?'Invoice document':'MIR document')+'</div><div class="rc-source-meta">Uploaded by '+esc(d.uploaded_by||'project user')+' · '+esc(d.sha256.slice(0,10))+'</div></div></div>'+metaText+qualityText+'<div class="rc-source-stats"><span class="rc-stat-pill">'+d.pages+' page(s)</span><span class="rc-stat-pill">'+n+' extracted line(s)</span>'+status+'</div><div class="rc-source-actions"><button data-ocr="'+d.id+'">'+(d.ocr_status==='running'?'OCR running…':'Run OCR')+'</button><button class="alt" data-addline="'+d.id+'">إضافة سطر</button><a class="alt" href="/api/recon/page?doc='+d.id+'&page=1" target="_blank">عرض الصفحة الأولى ↗</a><button class="alt" data-rmdoc="'+d.id+'">حذف</button></div></div>';
   }).join('')+'</div>'
  : '<div class="rc-empty">No PDF documents yet.<br>Upload MIRs or supplier invoices above.</div>';
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
  const isInvoice=r.doc_kind==='invoice';
  const ocrTxt=r.origin==='ocr'?`${isInvoice?'Invoice OCR draft':'MIR OCR draft'}: بند ${esc(o.contract_item_no||'?')} · ref ${esc(o.ref_no||'?')} · date ${esc(o.date||'?')} · “${esc(dedupeRepeatedWords(o.description||'(nothing read)'))}” · unit ${esc(o.unit||'?')} · Q.T ${o.qty??'?'} · ${isInvoice?'Line amount':'TOTAL'} ${o.total??'?'} · conf ${o.conf==null?'n/a':(isInvoice&&Number(o.conf)<=1?Math.round(Number(o.conf)*100)+'%':o.conf)}${o.date_alternatives&&o.date_alternatives.length?' (other date readings: '+esc(o.date_alternatives.join(', '))+')':''}${o.mir_status?' · box ticked: '+esc(o.mir_status):''}${o.invoice_no?' · invoice '+esc(o.invoice_no):''}${o.extra_text?' · handwriting/extra: '+esc(o.extra_text):''}${o.alts&&Object.keys(o.alts).length?' · <b>other readings:</b> '+Object.entries(o.alts).map(([k,v])=>esc(k)+' '+v.join(' / ')).join('; '):''}`:'Manually entered line (no OCR data)';
  const rawOcr=String(o.raw_text||'').trim(), sourceCells=Array.isArray(o.source_cells)?o.source_cells:[];
  const rawValues=o.raw_values||o.raw||{};
  const mathExpected=(isInvoice&&invoiceNumber(e.qty)!=null&&invoiceNumber(e.unit_price)!=null)?invoiceNumber(e.qty)*invoiceNumber(e.unit_price):null;
  const mathDelta=(mathExpected!=null&&invoiceNumber(e.total)!=null)?mathExpected-invoiceNumber(e.total):null;
  const qualityLabels=isInvoice?invoiceQualityFlags(r,e,o):[];
   const qualityNames={invoice_math_mismatch_review:'الكمية × سعر الوحدة لا يساوي الإجمالي',invoice_math_incomplete_review:'بيانات الحساب غير مكتملة',invoice_raw_value_mismatch:'القيم النهائية تختلف عن النص الخام للنموذج',invoice_possible_zero_scale_error:'احتمال سقوط صفر أو تغيير مقياس الرقم',invoice_visual_confirmation_required:'قراءة OCR مسودة؛ يجب تأكيد كل رقم من صورة الفاتورة حتى لو كانت العملية الحسابية صحيحة'};
  const qualityPanel=isInvoice?'<div class="rc-ocr"><b>فحص الأرقام:</b> '+(qualityLabels.length?'<span style="color:#b42318">يحتاج مراجعة بصرية/حسابية</span>':'<span style="color:#067647">تمت المراجعة</span>')+' · المحسوب: '+(mathExpected==null?'N/A':esc(mathExpected))+' · الفرق عن الإجمالي: '+(mathDelta==null?'N/A':esc(mathDelta))+'<br>النص الخام الذي أعاده النموذج: كمية '+esc(rawValues.qty??'—')+' · سعر '+esc(rawValues.unit_price??'—')+' · إجمالي '+esc(rawValues.line_total??'—')+'<br>التحذيرات: '+esc(qualityLabels.map(f=>qualityNames[f]||f).join('، ')||'لا توجد')+'</div>':'';
  const rawEvidence=(rawOcr||isInvoice)?'<details class="rc-advanced"><summary>'+(isInvoice?'دليل الفاتورة · صورة الصفحة الأصلية':'Original OCR evidence · '+sourceCells.length+' source cells')+'</summary>'+(isInvoice?'<div class="small sub">الصورة الكاملة للصفحة متاحة من زر «عرض الصورة» أعلى السطر؛ لا توجد مربعات OCR منفصلة لهذه القراءة المرئية.</div>':'')+(rawOcr?'<pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto;margin:8px 0;font:12px/1.5 monospace">'+esc(rawOcr)+'</pre>':'')+(o.row_flags&&o.row_flags.length?'<div class="small sub">Extraction flags: '+esc(o.row_flags.join(', '))+'</div>':'')+'</details>':'';
  const gs=f=>(r.user&&f in r.user)?'':((o&&o[f]!=null)?' rc-guess':'');
  return `<div class="rc-rec" data-id="${r.id}" data-doc="${r.doc_id}" data-page="${r.page}"><div class="rc-bar">${chip(isInvoice?'INVOICE':'MIR',isInvoice?'c-info':'c-mute')} <b>${esc(r.filename)}</b> p${r.page} <button class="alt" data-scan>عرض الصورة</button> <a href="/api/recon/page?doc=${r.doc_id}&page=${r.page}" target="_blank">فتح الصورة ↗</a> ${r.review_status==='reviewed'?chip('reviewed','c-ok'):chip('not reviewed','c-warn')} ${chip('match: '+ms[0],ms[1])}${m.method==='manual'?chip('manual decision','c-info'):''} ${flags((()=>{const c=Number(o.conf);const pct=Number.isFinite(c)?(c>=0&&c<=1?c*100:c):null;let fs=r.flags||[];if(isInvoice){fs=fs.filter(f=>!invoiceQualitySet.has(f));fs.push(...qualityLabels)}return isInvoice&&pct!==null&&pct>=60?fs.filter(f=>String(f).trim()!=='low_ocr_confidence'):fs})())}</div>
  <div class="rc-ocr">${ocrTxt}</div>${qualityPanel}${rawEvidence}<div class="rc-scan" hidden></div>
  <div class="rc-grid"><div><label>${isInvoice?'Invoice number':'MIR ref no.'}</label><input data-f="ref_no" class="${gs('ref_no')}" value="${esc(e.ref_no||'')}"${dis}></div><div><label>التاريخ</label><input data-f="date" class="${gs('date')}" value="${esc(e.date||'')}"${dis}></div>
  <div style="grid-column:span 2"><label>وصف الصنف</label><textarea data-f="description" rows="2" class="${gs('description')}"${dis}>${esc(dedupeRepeatedWords(e.description||''))}</textarea></div>
  <div><label>الوحدة</label><input data-f="unit" class="${gs('unit')}" value="${esc(e.unit||'')}"${dis}></div><div><label>الكمية Q.T</label><input data-f="qty" class="${gs('qty')}" value="${e.qty??''}"${dis}></div>${isInvoice?`<div><label>سعر الوحدة</label><input data-f="unit_price" class="${gs('unit_price')}" value="${e.unit_price??''}"${dis}></div>`:''}<div><label>${isInvoice?'قيمة بند الفاتورة':'الإجمالي كما هو مكتوب'}</label><input data-f="total" class="${gs('total')}" value="${e.total??''}"${dis}></div>
  <div><label>${isInvoice?'تأكيد مراجعة الفاتورة':'حالة MIR (خانة الاختيار)'}</label><select data-f="mir_status"${dis}><option value="">— set —</option>${(isInvoice?['A']:['A','B','C','D']).map(s=>`<option value="${s}"${e.mir_status===s?' selected':''}>${isInvoice?'Reviewed / confirmed':s+' – '+({A:'Approved',B:'Approved with note',C:'Not approved',D:'Rejected'})[s]}</option>`).join('')}</select></div>
  <div><label>العلامة التجارية</label><input data-f="brand" value="${esc(e.brand||'')}"${dis}></div>
  <div style="grid-column:span 2"><label>بند المقايسة</label><select data-boq${dis}>${boqOpts(r)}</select></div></div>
  <div class="rc-bar"><button data-save${dis}>حفظ</button><button data-rev class="good"${dis}>حفظ وتأكيد المراجعة</button><button data-tot class="alt"${dis} title="نسخ الإجمالي إلى الكمية بعد مراجعة الصورة">نسخ الإجمالي للكمية</button><button data-appr class="good"${dis}>اعتماد المطابقة</button><button data-rej class="alt"${dis}>رفض المطابقة</button>${r.flags.includes('duplicate')?`<button data-notdup class="alt"${dis}>ليس مكررًا</button>`:`<button data-isdup class="alt"${dis} title="استبعاد السطر المكرر">استبعاد التكرار</button>`}${r.origin==='manual'?`<button data-del class="alt"${dis}>حذف السطر</button>`:''}</div></div>`}).join(''):'<div class="sub">No document lines match this filter. Check OCR status in Files, run OCR, or add a line manually.</div>';
 document.querySelectorAll('#rcRecs .rc-rec').forEach(box=>{const id=box.dataset.id;const get=()=>{const o={};box.querySelectorAll('[data-f]').forEach(i=>o[i.dataset.f]=i.value);return o};
  if(keep[id]){box.querySelectorAll('[data-f],[data-boq]').forEach(i=>{const k=i.dataset.f||'_boq';if(k in keep[id])i.value=keep[id][k]});box.dataset.dirty='1'}
  box.addEventListener('input',()=>box.dataset.dirty='1');box.addEventListener('change',()=>box.dataset.dirty='1');
  const run=async(fn,ok)=>{try{await fn();msg(ok,'success');box.removeAttribute('data-dirty');await load()}catch(e){msg(e.message,'error')}};
  box.querySelector('[data-scan]').onclick=e=>{const sc=box.querySelector('.rc-scan');if(sc.hidden){sc.innerHTML='<img loading="lazy" alt="scanned MIR page" src="/api/recon/page?doc='+box.dataset.doc+'&page='+box.dataset.page+'&w=1100">';sc.hidden=false;e.target.textContent='Hide scan'}else{sc.hidden=true;sc.innerHTML='';e.target.textContent='عرض الصورة'}};
  box.querySelector('[data-tot]').onclick=()=>{const t=box.querySelector('[data-f=total]').value;if(t==='')return msg('TOTAL is empty - read it from the scan first.','error');const qf=box.querySelector('[data-f=qty]');qf.value=t;qf.classList.remove('rc-guess');box.dataset.dirty='1'};
  box.querySelector('[data-save]').onclick=()=>run(async()=>{await act('update_record',{id,fields:get(),mark_reviewed:false});await act('match_run')},'Saved and BOQ auto-match refreshed. Check the suggested row and score; it is not approved automatically.');
  box.querySelector('[data-rev]').onclick=()=>run(async()=>{await act('update_record',{id,fields:get(),mark_reviewed:true});await act('match_run')},'Reviewed and BOQ auto-match refreshed. Check the suggested row and score; it is not approved automatically.');
  box.querySelector('[data-appr]').onclick=()=>run(()=>act('match_decide',{id,decision:'approve',boq_id:box.querySelector('[data-boq]').value}),'Match approved.');
  box.querySelector('[data-rej]').onclick=()=>run(()=>act('match_decide',{id,decision:'reject',boq_id:box.querySelector('[data-boq]').value}),'Match rejected.');
  const idp=box.querySelector('[data-isdup]');if(idp)idp.onclick=()=>run(()=>act('dup',{id,value:'duplicate'}),'Line excluded as duplicate.');
  const nd=box.querySelector('[data-notdup]');if(nd)nd.onclick=()=>run(()=>act('dup',{id,value:'not_duplicate'}),'Marked as not a duplicate.');
  const dl=box.querySelector('[data-del]');if(dl)dl.onclick=()=>{if(confirm('Delete this manual line?'))run(()=>act('delete_record',{id}),'Line deleted.')};
});}
function summaryRing(value,pct,label,detail){
 const c=2*Math.PI*42, p=pct==null?0:Math.max(0,Math.min(100,Number(pct))), dash=c*p/100;
 return `<div class="rc-summary-ring"><div class="rc-donut"><svg class="rc-donut-svg" viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="42" fill="none" stroke="#e6e9ef" stroke-width="11"></circle><circle cx="50" cy="50" r="42" fill="none" stroke="#14805e" stroke-width="11" stroke-linecap="round" stroke-dasharray="${dash} ${c}" transform="rotate(-90 50 50)"></circle></svg><div class="rc-donut-hole"><b>${pc(pct)}</b><span>progress</span></div></div><div class="rc-summary-info"><span>${esc(label)}</span><b>${fmt(value)}</b><span>${esc(detail||'')}</span></div></div>`;
}
function renderRec(){
 const s=D.comp.summary;const cur=s.currency_label;
 const financialPct=s.fin_pct==null?null:Number(s.fin_pct), remainingPct=financialPct==null?null:Math.max(0,100-financialPct);
 const mirReviewedPct=s.mir_records?((s.records_reviewed||0)/s.mir_records*100):100;
 const invoiceReviewedPct=s.invoice_records?((s.invoice_records_reviewed||0)/s.invoice_records*100):100;
 const warningClearPct=s.warnings===0?100:0;
 q('rcSummary').innerHTML=`<div class="rc-summary-rings">${summaryRing(s.boq_total_value,100,'Original BOQ value',cur)}${summaryRing(s.mir_value,s.fin_pct,'Recognized financial value',cur)}${summaryRing(s.remaining_value,remainingPct,'Remaining financial value',cur)}${summaryRing(s.boq_items,100,'BOQ items',s.items_with_confirmed+' with confirmed MIR · '+s.items_pending_review+' pending · '+s.items_without_mir+' without MIR · '+s.items_over_boq+' over BOQ')}${summaryRing(s.mir_records,mirReviewedPct,'MIR lines',s.records_reviewed+' reviewed · '+s.unmatched_mir_records+' unmatched · '+s.mir_docs_ocr_done+'/'+s.mir_docs+' PDFs OCR’d')}${summaryRing(s.invoice_records||0,invoiceReviewedPct,'Invoice lines',(s.invoice_records_reviewed||0)+' reviewed · '+(s.unmatched_invoice_records||0)+' unmatched · '+(s.invoice_docs_ocr_done||0)+'/'+(s.invoice_docs||0)+' PDFs OCR’d')}${summaryRing(s.warnings,warningClearPct,'Warnings',s.warnings===0?'No active warnings':'Review warnings in Export & log · 100% means clear')}</div>`;
 const bf=q('rcBrandF'),cv=bf.value;bf.innerHTML='<option value="">كل البراندات</option>'+D.comp.brands.filter(b=>b.items).map(b=>`<option value="${esc(b.key)}">${esc(b.brand)}</option>`).join('');bf.value=cv;
 const t=q('rcSearch').value.toLowerCase(),sf=q('rcStatF').value;
 let rows=D.comp.items.filter(x=>(!t||(x.description+' '+x.code+' '+x.part+' '+x.row_ref).toLowerCase().includes(t))&&(!bf.value||x.brand_key===bf.value)&&(!sf||x.status===sf));
 rows.sort((a,b)=>{let va=a[sortKey],vb=b[sortKey];if(va==null)va=-Infinity;if(vb==null)vb=-Infinity;return (va>vb?1:va<vb?-1:0)*sortDir});
 const H=[['code','Code'],['description','Description'],['brand','Brand'],['unit','الوحدة'],['boq_qty','BOQ qty'],['confirmed_qty','Confirmed MIR qty'],['invoice_confirmed_qty','Approved invoice qty (separate)'],['invoice_pending_qty','Pending invoice qty'],['remaining_qty','Remaining MIR qty'],['unit_price','الوحدة price'],['boq_value','BOQ value'],['financial_value','Recognized value'],['remaining_value','Remaining value'],['qty_pct','Qty %'],['fin_pct','Fin %'],['status','Status'],['n_records','Sources']];
 q('rcTable').innerHTML='<thead><tr>'+H.map(h=>`<th data-s="${h[0]}">${h[1]}${sortKey===h[0]?(sortDir>0?' ▲':' ▼'):''}</th>`).join('')+'</tr></thead><tbody>'+rows.map(x=>{const st=ST[x.status]||[x.status,''];const un=x.unit_source==='user'?x.unit:(x.unit||'');
  const src=[...(x.sources||[]).map(z=>`<a href="/api/recon/page?doc=${esc(D.docs.find(d=>d.filename===z.file)?.id||'')}&page=${z.page}" target="_blank">${esc(z.file)} p${z.page}</a> <span class="small">(${esc(z.ref_no||'?')}: ${fq(z.qty)} ${esc(z.unit||'')}, ${esc(z.status||'?')}${z.reviewed?'':', unreviewed'})</span>`),...(x.invoice_sources||[]).map(z=>`<a href="/api/recon/page?doc=${esc(D.docs.find(d=>d.filename===z.file)?.id||'')}&page=${z.page}" target="_blank">${esc(z.file)} p${z.page}</a> <span class="small">(Invoice ${esc(z.ref_no||'?')}: ${fmt(z.amount)})</span>`)].join('<br>');
  const na=k=>x.unavailable[k]?` title="${esc(x.unavailable[k])}"`:'';
  return `<tr><td>${esc(x.code)}<div class="small sub">${esc(x.row_ref.split('!').pop())}</div></td><td style="min-width:230px">${esc(x.description)}<div class="small sub">${esc(x.part)}</div>${flags(x.flags)}</td><td>${esc(x.brand)}</td><td><input value="${esc(un)}" placeholder="set" data-unit="${esc(x.item_id)}" style="width:62px"${canEdit()?'':' disabled'}></td><td class="num">${fq(x.boq_qty)}</td><td class="num"><b>${fq(x.confirmed_qty)}</b>${x.pending_qty?`<div class="small sub">+${fq(x.pending_qty)} unreviewed</div>`:''}${x.not_approved_qty?`<div class="small sub">${fq(x.not_approved_qty)} status C/D</div>`:''}${x.unit_conflict_qty?`<div class="small" style="color:#a00">${fq(x.unit_conflict_qty)} unit conflict</div>`:''}</td><td class="num"${x.remaining_qty<0?' style="color:#a00;font-weight:600"':''}>${fq(x.remaining_qty)}</td><td class="num">${x.unit_price==null?'<span'+na('price')+'>N/A</span>':fmt(x.unit_price)}</td><td class="num">${fmt(x.boq_value)}</td><td class="num">${fmt(x.financial_value)}<div class="small sub">${esc(x.financial_source||'unavailable')}</div></td><td class="num">${fmt(x.remaining_value)}</td><td class="num"><span${na('qty_pct')}>${pc(x.qty_pct)}</span></td><td class="num"><span${na('fin_pct')||na('price')}>${pc(x.fin_pct)}</span></td><td>${chip(st[0],st[1])}${x.awaiting_review?`<div class="small sub">${x.awaiting_review} awaiting review</div>`:''}</td><td class="small">${src||'—'}</td></tr>`}).join('')+'</tbody>';
 q('rcNote').textContent=D.comp.meta.definition_note+' Counted statuses: '+D.comp.meta.count_statuses.join(', ')+'. BOQ units are not in the workbook; set a unit per row to enable unit-conflict checks.';
 q('rcTable').querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.s;if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=1}renderRec()});
 q('rcTable').querySelectorAll('[data-unit]').forEach(i=>i.onchange=async()=>{try{await act('boq_unit',{item_id:i.dataset.unit,unit:i.value});await load()}catch(e){msg(e.message,'error')}})}
function renderBrands(){
 const brands=D.comp.brands||[];
 const cards=brands.map(b=>{
  const units=Object.entries(b.qty_by_unit||{}).filter(([u,v])=>Number(v&&v.boq)>0).sort((x,y)=>String(x[0]).localeCompare(String(y[0])));
  const circumference=2*Math.PI*42;
  const rings=units.map(([unit,v])=>{
   const boq=Number(v.boq)||0, confirmed=Number(v.confirmed)||0, remaining=Math.max(boq-confirmed,0);
   const pct=boq>0?confirmed/boq*100:null, ringPct=pct==null?0:Math.max(0,Math.min(100,pct));
   const dash=circumference*ringPct/100;
   return '<div class="rc-brand-unit"><div class="rc-donut"><svg class="rc-donut-svg" viewBox="0 0 100 100" role="img" aria-label="'+pc(pct)+' '+esc(unit)+' confirmed"><circle cx="50" cy="50" r="42" fill="none" stroke="#e6e9ef" stroke-width="11"></circle><circle cx="50" cy="50" r="42" fill="none" stroke="#14805e" stroke-width="11" stroke-linecap="round" stroke-dasharray="'+dash+' '+circumference+'" transform="rotate(-90 50 50)"></circle></svg><div class="rc-donut-hole"><b>'+pc(pct)+'</b><span>'+esc(unit)+'</span></div></div><div class="rc-brand-unit-info"><div class="rc-brand-legend"><span>Confirmed: <b>'+fq(confirmed)+' '+esc(unit)+'</b></span><span>Remaining: <b>'+fq(remaining)+' '+esc(unit)+'</b></span></div><div class="small sub">BOQ total: <b>'+fq(boq)+' '+esc(unit)+'</b></div><div class="small sub">'+pc(Math.max(0,100-pct))+' remaining</div>'+(pct>100?'<div class="small" style="color:#b42318;font-weight:700">Over BOQ by '+fq(confirmed-boq)+' '+esc(unit)+'</div>':'')+'</div></div>';
  }).join('');
  const fin=b.fin_pct==null?null:Number(b.fin_pct), finBar=fin==null?0:Math.max(0,Math.min(100,fin));
  const missingUnits=Number(b.unspecified_unit_items)||0;
  return '<div class="rc-brand-card"><div class="rc-brand-head"><div><b>'+esc(b.brand)+'</b><div class="small sub">'+b.items+' بند في المقايسة</div></div><span class="rc-brand-tag">ملخص تقدم البراند</span></div>' +
   '<section class="rc-brand-section"><div class="rc-brand-section-title">تقدم الكميات</div><div class="rc-brand-quantity"><div class="rc-brand-rings">'+(rings||'<div class="sub">لا توجد وحدات قياس محددة لها كميات BOQ.</div>')+'</div>'+(missingUnits?'<div class="small" style="margin-top:8px;color:#8a6100">'+missingUnits+' بندًا له كمية بلا وحدة؛ حدّد وحدته من BOQ overview.</div>':'')+'</div></section>' +
   '<section class="rc-brand-section rc-brand-fin-section"><div class="rc-brand-section-title">التقدم المالي <span>بناءً على قيم البنود</span><b>'+pc(fin)+'</b></div><div class="rc-brand-fin-track"><div style="width:'+finBar+'%"></div></div><div class="rc-brand-fin-caption"><span>Invoice value (approved) / MIR estimate / BOQ value</span><span>'+pc(fin)+' of BOQ value confirmed</span></div><div class="small sub">القيمة المالية المتبقية: <b>'+fmt(b.remaining_value)+'</b> · القيمة المعتمدة: <b>'+fmt(b.mir_value)+'</b>.</div></section>' +
   '<div class="rc-brand-foot"><span><b>'+b.items_with_confirmed+' / '+b.items+'</b> بند له كمية معتمدة</span><span><b>'+b.unmatched_mir_records+'</b> سطر مصدر غير مطابق</span></div>'+(b.over_boq.length?'<div class="rc-brand-warning">بنود تجاوزت كمية BOQ: '+b.over_boq.length+'</div>':'')+(b.financial_over_boq&&b.financial_over_boq.length?'<div class="rc-brand-warning">قيمة الفواتير المعتمدة تجاوزت قيمة BOQ في '+b.financial_over_boq.length+' بند</div>':'')+'</div>';
 }).join('');
 q('rcBrands').innerHTML=cards||'<div class="rc-empty">لا توجد بيانات براندات بعد. ارفع ملف BOQ وراجع سطور MIR.</div>';
}
function renderExp(){
 q('rcExc').innerHTML=D.comp.exceptions.length?'<div class="rc-scroll" style="max-height:300px"><table class="rc"><thead><tr><th>Severity</th><th>Type</th><th>Message</th></tr></thead><tbody>'+D.comp.exceptions.slice(0,300).map(e=>`<tr><td>${chip(e.severity,e.severity==='error'?'c-bad':e.severity==='warning'?'c-warn':'c-info')}</td><td>${esc(e.type)}</td><td>${esc(e.message)}</td></tr>`).join('')+'</tbody></table></div>':'<div class="sub">None.</div>';
 q('rcAudit').innerHTML=D.audit.length?D.audit.map(a=>`${esc(a.ts)} · <b>${esc(a.user)}</b> · ${esc(a.action)} · ${esc(a.detail)}`).join('<br>'):'No actions yet.'}
function tab(t){document.querySelectorAll('#rcTabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));document.querySelectorAll('#reconPanel .rc-pane').forEach(p=>p.classList.toggle('on',p.id==='rcP-'+t))}
q('rcTabs').onclick=e=>{const b=e.target.closest('button');if(b)tab(b.dataset.t)};
['rcRevFilter','rcRevSearch'].forEach(i=>q(i).oninput=()=>D&&renderRecs());['rcSearch','rcBrandF','rcStatF'].forEach(i=>q(i).oninput=()=>D&&renderRec());
async function upload(kind,file){const fd=new FormData();fd.append('kind',kind);fd.append('file',file);return api('/api/recon/upload',{method:'POST',body:fd})}
q('rcBoqUp').onclick=async()=>{const f=q('rcBoqFile').files[0];if(!f)return msg('Choose the BOQ .xlsx first.','error');try{msg('Reading workbook...','info');const j=await upload('boq',f);msg('BOQ loaded: '+j.items+' items from sheet “'+j.sheet+'”.','success');q('rcBoqFile').value='';await load()}catch(e){msg(e.message,'error')}};
q('rcMirUp').onclick=async()=>{const fs=[...q('rcMirFiles').files];if(!fs.length)return msg('Choose one or more MIR PDFs.','error');let ok=0,bad=[];for(const f of fs){try{msg('Uploading '+f.name+'...','info');await upload('mir',f);ok++}catch(e){bad.push(f.name+': '+e.message)}}msg(ok+' MIR PDF(s) added.'+(bad.length?' Skipped: '+bad.join(' | '):''),bad.length?'error':'success');q('rcMirFiles').value='';await load()};
q('rcInvoiceUp').onclick=async()=>{const fs=[...q('rcInvoiceFiles').files];if(!fs.length)return msg('Choose one or more invoice PDFs.','error');let ok=0,bad=[];for(const f of fs){try{msg('Uploading invoice '+f.name+'...','info');await upload('invoice',f);ok++}catch(e){bad.push(f.name+': '+e.message)}}msg(ok+' invoice PDF(s) added.'+(bad.length?' Skipped: '+bad.join(' | '):''),bad.length?'error':'success');q('rcInvoiceFiles').value='';await load()};
q('rcImportRepoInvoices').onclick=async()=>{try{msg('Scanning repository for invoice PDFs...','info');const j=await act('import_repo_invoices');msg(j.imported+' repository invoice PDF(s) imported.'+(j.skipped&&j.skipped.length?' Skipped: '+j.skipped.join(' | '):''),j.skipped&&j.skipped.length?'error':'success');await load()}catch(e){msg(e.message,'error')}};
async function loadPick(){try{const j=await api('/api/project_documents');const rows=(j.documents||[]).filter(d=>/\.(pdf|xlsx|xlsm)$/i.test(d.filename));q('rcDocPick').innerHTML=rows.length?rows.map(d=>`<label style="display:block"><input type="checkbox" data-pick="${esc(d.id)}"> ${esc(d.filename)} <span class="small sub">(${esc(d.category_label||d.category)})</span></label>`).join(''):'No PDF/Excel documents saved yet.'}catch(e){q('rcDocPick').textContent=e.message}}
q('rcImport').onclick=async()=>{const ids=[...document.querySelectorAll('[data-pick]:checked')].map(x=>x.dataset.pick);if(!ids.length)return msg('Tick at least one saved document.','error');try{const j=await act('import_docs',{doc_ids:ids});msg('Imported '+j.imported+' file(s).'+(j.skipped.length?' Skipped: '+j.skipped.join(' | '):''),j.skipped.length?'error':'success');await load()}catch(e){msg(e.message,'error')}};
q('rcImportRepoDocs').onclick=async()=>{try{msg('Scanning repository for BOQ, MIR and invoice files...','info');const j=await act('import_repo_docs');const files=(j.files||[]).map(x=>x.kind+': '+x.filename).join(' · ');msg(j.imported+' repository file(s) imported.'+(files?' '+files:'')+(j.skipped&&j.skipped.length?' Skipped: '+j.skipped.join(' | '):''),j.skipped&&j.skipped.length?'error':'success');await load()}catch(e){msg(e.message,'error')}};
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
