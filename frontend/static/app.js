'use strict';

const state = {
  page: 'overview', templates: [], datasets: [], selectedKind: 'sales', selectedDataset: '', analyticsDatasetSelection: '', previewOffset: 0,
  hardware: null, engine: null, pyarrowAvailable: null, benchmarkMaxThreads: 4, live: null, generationSocket: null, benchmarkSocket: null,
  generationId: null, benchmarkId: null, lastResult: null, history: [],
  generationPollTimer: null, benchmarkPollTimer: null, generationTerminal: false, benchmarkTerminal: false,
  generationCancelPending: false, benchmarkStarting: false, generationStarting: false,
  generationCancelQueued: false, generationStartedAt: 0, generationUiTimer: null,
  cpuHistory: [], ramHistory: [], processCpuHistory: [], processRamHistory: [], activeJob: null, analyticsDatasetId: '', lastBenchmarkEventId: 0,
  benchmarkPhaseStartedAt: 0, benchmarkPhaseBaseElapsed: 0, benchmarkPhaseTimer: null, monitorRefreshTimer: null, monitorRefreshPending: false
};
const $ = (id) => document.getElementById(id);
const cv = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#8798b1'; // theme colour for canvas charts
const pageNames = {overview:'Overview',generator:'Data Lab',analytics:'Benchmark Setup',live:'Live Monitor',results:'Results',summary:'Summary'};
const templateIcons = {sales:'▣',finance:'◈',products:'▤',students:'⌂',employees:'♙',attendance:'◷'};
const templateHints = {sales:'Transactions & revenue',finance:'Accounts & amounts',products:'Stock & valuation',students:'Academic performance',employees:'People & payroll',attendance:'Workforce records'};
let MIN_BENCHMARK_ROWS = 100_000;
let ALLOWED_GENERATION_ROWS = new Set([100_000,1_000_000,5_000_000,10_000_000,25_000_000,50_000_000,75_000_000]);
const operationDescriptions = {
  comprehensive_analytics:'Recommended demo: in one pass, summarize the numeric column, count values above your threshold, and calculate count, sum and average for every category. It gives each thread several useful calculations to perform on the same records.',
  numeric_stats:'Looks at one numeric field across the whole dataset and calculates record count, total, average, standard deviation, minimum and maximum. Example: overall Revenue statistics.',
  filter_gt:'Counts rows where the selected numeric value is strictly greater than the limit you enter. Example: how many sales have Revenue above 50,000?',
  frequency:'Counts the number of records in each category. Choose a field such as Region or Product; the result shows how many rows belong to each value.',
  group_sum:'Splits all records into groups using the category you select. For each group it calculates the number of records, the total and the average of your numeric field. Example: choose Region + Revenue to compare total sales for each region.',
  top_k_groups:'Adds the numeric values inside each category, sorts categories from highest total to lowest, and returns only the top K. Example: top 10 products by total Revenue.',
  date_frequency:'Counts records over time using a date column. Choose Year, Month or Day to see the time-based distribution.'
};
const scheduleDescriptions = {
  static:'Static gives threads predictable chunks up front. It is a good starting point when each record takes similar effort.',
  dynamic:'Dynamic lets threads request the next chunk when they finish. It can help uneven work, but adds coordination overhead.',
  guided:'Guided starts with larger chunks and gradually reduces them. It can balance uneven work, with some scheduling overhead.'
};

function fmtNum(n, digits=0){if(n===null||n===undefined||!Number.isFinite(Number(n)))return '—';return Number(n).toLocaleString(undefined,{maximumFractionDigits:digits});}
function fmtBytes(n){if(n===null||n===undefined||!Number.isFinite(Number(n)))return '—';let v=Number(n),u=['B','KB','MB','GB','TB'],i=0;while(v>=1024&&i<u.length-1){v/=1024;i++;}return `${v.toFixed(i?1:0)} ${u[i]}`;}
function fmtRate(n){return `${fmtBytes(n)}/s`;}
function fmtSec(n){return Number.isFinite(Number(n))?`${Number(n).toFixed(3)}s`:'—';}
function toast(message,type='info'){const el=document.createElement('div');el.className=`toast ${type}`;el.textContent=message;$('toastStack').appendChild(el);setTimeout(()=>el.remove(),4300);}
function formatApiErrorDetail(detail){
  if(typeof detail==='string')return detail;
  if(Array.isArray(detail))return detail.map(item=>{
    if(typeof item==='string')return item;
    if(!item||typeof item!=='object')return String(item);
    const where=Array.isArray(item.loc)?item.loc.filter(part=>part!=='body').join('.') : '';
    const message=String(item.msg||item.message||item.type||'Invalid request');
    return where?`${where}: ${message}`:message;
  }).filter(Boolean).join(' · ');
  if(detail&&typeof detail==='object'){
    if(typeof detail.message==='string')return detail.message;
    if(typeof detail.msg==='string')return detail.msg;
    try{return JSON.stringify(detail);}catch{return 'The server returned an unreadable error response.';}
  }
  return detail==null?'':String(detail);
}
async function api(url,options={}){
  const method=(options.method||'GET').toUpperCase();
  const res=await fetch(url,{cache:method==='GET'?'no-store':(options.cache||'no-store'),headers:{'Content-Type':'application/json',...(options.headers||{})},...options});
  const raw=await res.text();let data={};
  if(raw){try{data=JSON.parse(raw);}catch{data={detail:raw};}}
  if(!res.ok){
    const detail=formatApiErrorDetail(data.detail??data.message);
    throw new Error(detail||`Request failed (${res.status})`);
  }
  return data;
}

function formatRecordPreset(rows){
  const value=Number(rows);
  if(value>=1_000_000)return `${value/1_000_000} million records`;
  return `${fmtNum(value)} records`;
}
async function loadGenerationOptions(){
  try{
    const options=await api('/api/datasets/generation-options');
    const presets=(options.generation_row_presets||options.recommended_demo_row_presets||[])
      .map(Number).filter(value=>Number.isSafeInteger(value)&&value>=100_000&&value<=75_000_000);
    if(presets.length){
      ALLOWED_GENERATION_ROWS=new Set(presets);
      const select=$('datasetRowsSelect');
      const previous=Number(select.value);
      select.innerHTML=presets.map((rows,index)=>`<option value="${rows}">${formatRecordPreset(rows)}${index===0?' · Start here':rows===Math.max(...presets)?' · Maximum':''}</option>`).join('');
      select.value=ALLOWED_GENERATION_ROWS.has(previous)?String(previous):String(presets[0]);
    }
    MIN_BENCHMARK_ROWS=Number(options.minimum_benchmark_rows||100_000);
    if($('datasetRowsHint'))$('datasetRowsHint').textContent=options.notes||'Start at 100K, then increase the dataset size after each verified comparison.';
  }catch(error){
    console.warn('Could not load generation preset contract; using the built-in supported sizes.',error);
  }
}

function navigate(requestedPage){
  let page=requestedPage;
  if(page==='preview')page='generator';
  if(page==='monitor')page='live';
  if(page==='history')page='results';
  if(!pageNames[page])return;
  const pageChanged=state.page!==page;
  state.page=page;
  document.querySelectorAll('.page').forEach(x=>x.classList.toggle('active',x.id===`page-${page}`));
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
  $('pageBreadcrumb').textContent=pageNames[page];
  $('sidebar').classList.remove('open');
  // Always open a page at its top (instant, overriding CSS smooth-scroll) — e.g. Start benchmark → Live Monitor.
  if(pageChanged)window.scrollTo({top:0,left:0,behavior:'instant'});
  window.dispatchEvent(new CustomEvent('pbda:page',{detail:{page}}));
  if(page==='generator'){loadDatasets();setWorkflowStep(state.selectedDataset?2:1);}
  if(page==='analytics'){setWorkflowStep(3);loadDatasets().then(syncAnalytics);}
  if(page==='live'){refreshMonitor();if(!state.benchmarkId&&!state.benchmarkTerminal)recoverActiveBenchmark();}
  if(page==='results'){renderResult();loadHistory();if(requestedPage==='history')$('resultsHistory').open=true;}
  if(requestedPage==='preview'){
    (async()=>{
      await loadDatasets();
      const selected=state.selectedDataset||state.datasets[0]?.dataset_id||'';
      if(selected){state.selectedDataset=selected;$('previewDatasetSelect').value=selected;await loadPreview(selected,0);}
      $('dataPreviewSection').scrollIntoView({behavior:'smooth',block:'start'});
    })();
  }
}

function setWorkflowStep(step){
  document.querySelectorAll('.workflow-step').forEach((el,index)=>{
    el.classList.toggle('active',index===step-1);
    el.classList.toggle('completed',index<step-1);
  });
}

document.querySelectorAll('.nav-item').forEach(btn=>btn.addEventListener('click',()=>navigate(btn.dataset.page)));
document.querySelectorAll('[data-goto]').forEach(btn=>btn.addEventListener('click',()=>navigate(btn.dataset.goto)));
$('mobileMenu').addEventListener('click',()=>$('sidebar').classList.toggle('open'));

/* Live line charts are drawn by static/livecharts.js (fixed-size boxes, time-based animation). */
function pushHistory(arr,value){arr.push(Number(value)||0);if(arr.length>60)arr.shift();}

async function loadHealth(){try{const d=await api('/api/health');state.hardware=d.hardware;state.engine=d.engine;state.pyarrowAvailable=d.pyarrow_available===true;state.benchmarkMaxThreads=Number(d.benchmark_limits?.max_threads||4);const caps=d.kernel_capabilities||{};const legacyComprehensive=caps.comprehensive_mode==='legacy_three_kernel_compatibility';$('connectionText').textContent='LOCAL ENGINE CONNECTED';$('hostnameTop').textContent=d.hardware?.hostname||'Local machine';$('ovCores').textContent=d.hardware?.logical_processors??'—';$('ovCpuCores').textContent=`${d.hardware?.physical_cores??'—'} physical / ${d.hardware?.logical_processors??'—'} logical`;$('ovCpuModel').textContent=d.hardware?.cpu_name||'CPU hardware';$('engineName').textContent=d.engine?.engine||'Unavailable';$('engineOpenmp').textContent=d.engine?.openmp?'Available':'Unavailable';$('engineSchedules').textContent=(d.engine?.scheduling||[]).join(', ')||'—';$('engineStatus').className=`engine-status ${d.engine?.openmp?'ready':'unavailable'}`;$('engineStatus').innerHTML=`<span class="status-dot"></span>${d.engine?.openmp?'Native OpenMP engine ready':'Native OpenMP engine not built'}`;$('engineNote').textContent=!d.engine?.openmp?'Run setup_windows.bat (or setup_linux.sh) to compile the Cython/OpenMP extension. Parallel benchmarks remain disabled until the native engine is available.':legacyComprehensive?'Your current native binary is an older build. The Full Big Data Analysis operation has a compatibility route using existing native kernels, so it can run; rebuild the extension to enable the newer fused single-pass kernel.':'Cython/OpenMP extension loaded. The fused Full Big Data Analysis kernel is available.';$('engineWarning').textContent=!d.engine?.openmp?'OpenMP engine unavailable. Build the native extension before starting a benchmark.':legacyComprehensive?'Compatibility mode detected: Full Big Data Analysis will use three existing native kernels. Rebuild the extension for the optimized fused one-pass path.':'Native Cython/OpenMP engine is loaded. The fused analytics kernel is available.';if(!d.engine?.openmp)$('startBenchmark').disabled=true;else $('startBenchmark').disabled=false;}catch(e){$('connectionText').textContent='BACKEND DISCONNECTED';toast(e.message,'error');}}
async function loadTemplates(){try{const d=await api('/api/datasets/templates');state.templates=d.templates;renderTemplates();}catch(e){toast(`Could not load templates: ${e.message}`,'error');}}
function renderTemplates(){const wrap=$('templateGrid');wrap.innerHTML='';state.templates.forEach(t=>{const b=document.createElement('button');b.className=`template-card ${state.selectedKind===t.kind?'selected':''}`;b.innerHTML=`<span class="template-emoji">${templateIcons[t.kind]||'▦'}</span><strong>${escapeHtml(t.label)}</strong><small>${escapeHtml(templateHints[t.kind]||'Structured data')}</small>`;b.addEventListener('click',()=>{state.selectedKind=t.kind;renderTemplates();renderSchemaTemplate();});wrap.appendChild(b);});renderSchemaTemplate();}
function renderSchemaTemplate(){const t=state.templates.find(x=>x.kind===state.selectedKind);if(!t)return;$('schemaDescription').textContent=t.description;$('schemaCount').textContent=`${t.columns.length} FIELDS`;$('schemaTable').innerHTML='<div class="schema-row head"><span>COLUMN</span><span>DATA TYPE</span><span>ROLE</span></div>'+t.columns.map(c=>`<div class="schema-row"><strong>${escapeHtml(c.name)}</strong><span><i class="type-badge">${escapeHtml(c.type)}</i></span><span class="role-badge">${escapeHtml(c.role)}</span></div>`).join('');}
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function generationTerminal(status){return ['completed','failed','cancelled'].includes(status);}
function stopGenerationClock(){
  if(state.generationUiTimer){clearInterval(state.generationUiTimer);state.generationUiTimer=null;}
  state.generationStartedAt=0;
}
function startGenerationClock(){
  stopGenerationClock();
  state.generationStartedAt=performance.now();
  state.generationUiTimer=setInterval(()=>{
    if(state.generationStartedAt&& !state.generationTerminal){
      $('generationElapsed').textContent=fmtSec((performance.now()-state.generationStartedAt)/1000);
    }
  },250);
}
function showGenerationStarted(rows,batchSize){
  state.generationCancelPending=false;
  state.generationCancelQueued=false;
  state.generationStarting=true;
  $('generationProgress').classList.remove('hidden');
  $('generationStatusPill').innerHTML='<i></i> GENERATING';
  $('generationTitle').textContent='Preparing your dataset';
  const blockRows=Math.min(5000,Math.max(500,Number(batchSize)||25000));
  $('generationMessage').textContent=`Preparing ${fmtNum(rows)} records. Work is written in blocks of up to ${fmtNum(blockRows)} rows; completed rows update after each successful write.`;
  updateGeneration({generated_rows:0,total_rows:rows,progress:0,elapsed_seconds:0,records_per_second:0,file_size_bytes:0,batch_number:0,total_batches:Math.ceil(rows/Math.max(1,batchSize)),batch_size:batchSize,write_block_size:blockRows,phase:'preparing',writer:'Preparing CSV writer'});
  $('cancelGeneration').disabled=false;
  $('cancelGeneration').textContent='Cancel generation';
  $('cancelGeneration').classList.remove('hidden');
  $('openGeneratedPreview').classList.add('hidden');
  $('generateBtn').disabled=true;
  setWorkflowStep(1);
  startGenerationClock();
}
async function completeGeneration(dataset){
  if(state.generationTerminal)return;
  state.generationTerminal=true;
  state.generationStarting=false;
  state.generationCancelPending=false;
  stopGenerationClock();
  if(state.generationPollTimer)clearTimeout(state.generationPollTimer);
  state.selectedDataset=dataset.dataset_id;
  if(Number(dataset.rows)>=MIN_BENCHMARK_ROWS)state.analyticsDatasetSelection=dataset.dataset_id;
  setWorkflowStep(2);
  updateGeneration({generated_rows:dataset.rows,total_rows:dataset.rows,progress:100,elapsed_seconds:dataset.generation_seconds,records_per_second:dataset.generation_records_per_second,file_size_bytes:dataset.file_size_bytes,batch_number:Math.ceil(dataset.rows/Math.max(1,dataset.batch_size||10000)),total_batches:Math.ceil(dataset.rows/Math.max(1,dataset.batch_size||10000)),writer:dataset.writer,phase:'completed'});
  $('generationTitle').textContent='Dataset ready';
  $('generationStatusPill').innerHTML='<i></i> DATASET READY';
  $('generationMessage').textContent=`${dataset.name} has been saved. Preview the records below, then continue to Benchmark Setup.`;
  $('cancelGeneration').classList.add('hidden');$('cancelGeneration').disabled=false;
  $('openGeneratedPreview').classList.remove('hidden');$('generateBtn').disabled=false;
  await loadDatasets();renderDatasetSelectors();$('previewDatasetSelect').value=state.selectedDataset;
  await loadPreview(state.selectedDataset,0);toast('Dataset generated and saved on this computer.','success');
}
function stopGeneration(status,message){
  if(state.generationTerminal)return;
  state.generationCancelPending=false;
  state.generationStarting=false;
  state.generationTerminal=true;
  stopGenerationClock();
  if(state.generationPollTimer)clearTimeout(state.generationPollTimer);
  $('generationTitle').textContent=status==='cancelled'?'Generation cancelled':'Generation failed';
  $('generationStatusPill').innerHTML=`<i></i> ${status==='cancelled'?'CANCELLED':'FAILED'}`;
  $('generationProgress').querySelector('.progress-track').classList.remove('is-indeterminate');
  $('generationMessage').textContent=message||`Generation ${status}. No incomplete dataset was added to the library.`;
  $('cancelGeneration').classList.add('hidden');$('cancelGeneration').disabled=false;$('cancelGeneration').textContent='Cancel generation';
  $('generateBtn').disabled=false;$('openGeneratedPreview').classList.add('hidden');
  loadDatasets();toast(message||`Generation ${status}.`,status==='cancelled'?'info':'error');
}
function handleGenerationEvent(e){
  if(!e)return;
  if(e.type==='generation_progress'){
    updateGeneration(e);
    if(state.generationCancelPending){
      $('generationTitle').textContent='Cancelling generation';
      $('generationMessage').textContent='Cancellation accepted. The current small write block is finishing; no incomplete dataset will be kept.';
    }else if(e.phase==='preparing'){
      $('generationTitle').textContent='Preparing your dataset';
    }else if(e.phase==='generating'){
      $('generationTitle').textContent='Generating records';
    }else if(e.phase==='writing'){
      $('generationTitle').textContent='Saving records';
    }else if(e.phase==='saved'){
      $('generationTitle').textContent='Generating your dataset';
    }
    return;
  }
  if(e.type==='generation_status'){
    if(e.status==='cancelling'){$('generationTitle').textContent='Cancelling generation';$('generationMessage').textContent=e.message||'Stopping after the current small write block.';}
    else if(e.status==='cancelled'||e.status==='failed')stopGeneration(e.status,e.message);
    else if(e.status==='generating'){$('generationTitle').textContent='Preparing your dataset';$('generationMessage').textContent=e.message||'Preparing the first small write block.';}
    return;
  }
  if(e.type==='generation_complete'&&e.dataset)completeGeneration(e.dataset);
}
async function pollGeneration(id){
  if(!id||id!==state.generationId||state.generationTerminal)return;
  try{
    const d=await api(`/api/datasets/generation/${id}`);
    if(d.progress_state&&Number.isFinite(Number(d.progress_state.total_rows))){
      handleGenerationEvent({type:'generation_progress',...d.progress_state});
    }
    if(d.latest&&d.latest.type!=='generation_progress')handleGenerationEvent(d.latest);
    if(Number.isFinite(Number(d.elapsed_seconds))){
      const elapsed=Number(d.elapsed_seconds);
      const started=state.generationStartedAt;
      $('generationElapsed').textContent=fmtSec(started?Math.max(elapsed,(performance.now()-started)/1000):elapsed);
    }
    if(d.status==='queued'){
      $('generationTitle').textContent='Waiting for a worker';
      $('generationMessage').textContent='Your request is queued. The elapsed timer is running; generation will begin when a worker is available.';
    }
    if(d.status==='cancelling'){
      state.generationCancelPending=true;
      $('generationTitle').textContent='Cancelling generation';
      $('generationMessage').textContent='Cancellation received. The current small write block is finishing; partial output will not be kept.';
      $('cancelGeneration').disabled=true;$('cancelGeneration').textContent='Cancelling…';
    }
    if(d.status==='completed'&&d.result)await completeGeneration(d.result);
    else if(d.status==='failed'||d.status==='cancelled')stopGeneration(d.status,d.error||d.latest?.message);
    else if(!state.generationTerminal)state.generationPollTimer=setTimeout(()=>pollGeneration(id),450);
  }catch(e){
    $('generationMessage').textContent=`Status connection delayed; retrying. ${e.message}`;
    if(!state.generationTerminal)state.generationPollTimer=setTimeout(()=>pollGeneration(id),1000);
  }
}

async function loadDatasets(){try{const d=await api('/api/datasets');state.datasets=d.datasets||[];renderDatasetTable();renderDatasetSelectors();renderOverviewDatasets();}catch(e){toast(e.message,'error');}}
function renderDatasetTable(){const body=$('datasetsTable');if(!state.datasets.length){body.innerHTML='<tr><td colspan="6" class="empty-cell">No generated datasets.</td></tr>';return;}body.innerHTML=state.datasets.map(d=>{const eligible=Number(d.rows)>=MIN_BENCHMARK_ROWS;const analyze=eligible?`<button class="table-action" data-use="${d.dataset_id}">Analyze</button>`:'<button class="table-action" disabled title="Generate at least 100,000 records to benchmark">Preview only</button>';return `<tr><td><strong>${escapeHtml(d.name)}</strong><br><small class="muted">${escapeHtml(d.filename)}</small></td><td>${escapeHtml(d.label)}</td><td>${fmtNum(d.rows)}</td><td>${escapeHtml(d.file_size_human)}</td><td>${new Date(d.created_at).toLocaleString()}</td><td><button class="table-action" data-preview="${d.dataset_id}">Preview</button>${analyze}</td></tr>`;}).join('');body.querySelectorAll('[data-preview]').forEach(b=>b.addEventListener('click',()=>{state.selectedDataset=b.dataset.preview;navigate('preview');}));body.querySelectorAll('[data-use]').forEach(b=>b.addEventListener('click',()=>{const d=state.datasets.find(x=>x.dataset_id===b.dataset.use);if(!d||Number(d.rows)<MIN_BENCHMARK_ROWS){toast('Benchmark setup accepts datasets with at least 100,000 records.','info');return;}state.analyticsDatasetSelection=d.dataset_id;renderDatasetSelectors();$('analyticsDataset').value=d.dataset_id;syncAnalytics();navigate('analytics');}));}
function renderDatasetSelectors(){
  const previewExists=state.datasets.some(d=>d.dataset_id===state.selectedDataset);
  if(!previewExists)state.selectedDataset=state.datasets[0]?.dataset_id||'';
  const previewOpts=state.datasets.map(d=>`<option value="${d.dataset_id}">${escapeHtml(d.name)} · ${fmtNum(d.rows)} rows</option>`).join('');
  const eligible=state.datasets.filter(d=>Number(d.rows)>=MIN_BENCHMARK_ROWS);
  if(!eligible.some(d=>d.dataset_id===state.analyticsDatasetSelection))state.analyticsDatasetSelection=eligible[0]?.dataset_id||'';
  const analyticsOpts=eligible.map(d=>`<option value="${d.dataset_id}">${escapeHtml(d.name)} · ${fmtNum(d.rows)} rows</option>`).join('');
  $('previewDatasetSelect').innerHTML='<option value="">Select a generated dataset</option>'+previewOpts;
  $('analyticsDataset').innerHTML=eligible.length?'<option value="">Select a Big Data dataset</option>'+analyticsOpts:'<option value="">Generate a dataset of at least 100K records first</option>';
  $('previewDatasetSelect').value=state.selectedDataset;
  $('analyticsDataset').value=state.analyticsDatasetSelection;
  $('previewToAnalytics').disabled=!(state.datasets.find(d=>d.dataset_id===state.selectedDataset)?.rows>=MIN_BENCHMARK_ROWS);
}
function renderOverviewDatasets(){$('datasetCount').textContent=fmtNum(state.datasets.length);$('datasetRows').textContent=fmtNum(state.datasets.reduce((a,b)=>a+Number(b.rows||0),0));$('datasetMini').innerHTML=state.datasets.slice(0,3).map(d=>`<div class="dataset-mini-row"><div><strong>${escapeHtml(d.name)}</strong><small>${escapeHtml(d.label)}</small></div><em>${fmtNum(d.rows)} rows</em></div>`).join('')||'<div class="empty-small">No datasets yet. Generate your first dataset.</div>';}
$('refreshDatasets').addEventListener('click',loadDatasets);
$('generateBtn').addEventListener('click',startGeneration);
async function startGeneration(){
  if(state.generationId&&!state.generationTerminal){toast('A dataset is already being generated. Cancel or finish it first.','error');return;}
  const rows=Number($('datasetRowsSelect').value);
  const seed=Number($('datasetSeed').value);
  const batchSize=Number($('generatorBatchSize').value||25000);
  if(!Number.isSafeInteger(rows)||!ALLOWED_GENERATION_ROWS.has(rows)){toast('Choose one of the supported dataset sizes: 100K, 1M, 5M, 10M, 25M, 50M or 75M records.','error');$('datasetRowsSelect').focus();return;}
  if(!Number.isSafeInteger(seed)||seed<0){toast('Random seed must be a whole number of zero or higher.','error');$('datasetSeed').focus();return;}
  if(!Number.isSafeInteger(batchSize)||batchSize<10000||batchSize>100000){toast('Choose a valid generation chunk size between 10,000 and 100,000.','error');$('generatorBatchSize').focus();return;}
  const payload={kind:state.selectedKind,rows,seed,batch_size:batchSize,name:$('datasetName').value.trim()||null};
  state.generationId=null;state.generationTerminal=false;state.generationCancelPending=false;state.generationStarting=true;state.generationCancelQueued=false;
  showGenerationStarted(rows,batchSize);
  try{
    const r=await api('/api/datasets/generate',{method:'POST',body:JSON.stringify(payload)});
    state.generationId=r.generation_id;state.generationStarting=false;
    $('cancelGeneration').disabled=false;$('cancelGeneration').textContent='Cancel generation';
    if(state.generationSocket){try{state.generationSocket.close();}catch{}}
    const proto=location.protocol==='https:'?'wss':'ws';
    const ws=new WebSocket(`${proto}://${location.host}/ws/generation/${r.generation_id}`);state.generationSocket=ws;
    ws.onmessage=ev=>{try{handleGenerationEvent(JSON.parse(ev.data));}catch(e){console.error('Invalid generation event',e);}};
    ws.onerror=()=>{$('generationMessage').textContent='Live connection reconnecting. Automatic status checks continue.';};
    pollGeneration(r.generation_id);
    if(state.generationCancelQueued)await requestGenerationCancel(r.generation_id);
  }catch(e){
    state.generationStarting=false;state.generationCancelQueued=false;state.generationTerminal=true;stopGenerationClock();state.generationId=null;
    $('generateBtn').disabled=false;$('cancelGeneration').classList.add('hidden');
    $('generationTitle').textContent='Could not start generation';
    $('generationStatusPill').innerHTML='<i></i> NOT STARTED';
    $('generationProgress').querySelector('.progress-track').classList.remove('is-indeterminate');
    $('generationBar').style.width='0%';$('generationPercent').textContent='0%';
    $('generationRows').textContent=`0 / ${fmtNum(rows)} records`;
    $('generationRate').textContent='0';$('generationSize').textContent='0 B';$('generationEta').textContent='—';
    $('generationMessage').textContent=e.message||'The server rejected the generation request. Check the selected size and free disk space.';
    toast(e.message||'Could not start dataset generation.','error');
  }
}
function updateGeneration(e){
  const progress=Math.max(0,Math.min(100,Number(e.progress)||0));
  const generated=Math.max(0,Number(e.generated_rows)||0);const total=Math.max(0,Number(e.total_rows)||0);
  const batch=e.batch_number!==undefined&&e.total_batches?`Generation chunk ${e.batch_number}/${e.total_batches}`:'Preparing first chunk';
  const block=e.write_block_number&&e.total_write_blocks?` · write block ${e.write_block_number}/${e.total_write_blocks}`:'';
  $('generationMessage').textContent=e.message||`${batch}${block}${e.writer?` · ${e.writer}`:''}`;
  const progressLabel=progress>0&&progress<1?progress.toFixed(2):progress.toFixed(1);
  $('generationPercent').textContent=`${progressLabel}%`;
  $('generationRows').textContent=`${fmtNum(generated)} / ${fmtNum(total)} records`;
  const track=$('generationBar').parentElement;
  track.classList.toggle('is-indeterminate',['preparing','generating','writing','finalizing'].includes(e.phase));
  track.setAttribute('aria-valuenow',String(progress.toFixed(1)));
  track.setAttribute('aria-valuetext',e.phase==='generating'?'Generating the next write block':e.phase==='writing'?'Saving the current write block':`${progressLabel} percent complete`);
  $('generationBar').style.width=`${progress}%`;
  $('generationElapsed').textContent=fmtSec(Number(e.elapsed_seconds)||0);
  $('generationRate').textContent=fmtNum(Number(e.records_per_second)||0);
  $('generationSize').textContent=fmtBytes(Number(e.file_size_bytes)||0);
  $('generationEta').textContent=e.estimated_remaining_seconds==null?(generated?'Calculating…':'Calculating after first saved block'):fmtSec(e.estimated_remaining_seconds);
}
async function requestGenerationCancel(id){
  if(!id||state.generationTerminal)return;
  state.generationCancelPending=true;
  $('cancelGeneration').disabled=true;$('cancelGeneration').textContent='Cancelling…';
  $('generationTitle').textContent='Cancellation requested';
  $('generationProgress').querySelector('.progress-track').classList.add('is-indeterminate');
  $('generationMessage').textContent='The server has received the cancel request. It will stop after the current small write block and delete the incomplete file.';
  try{
    const response=await api(`/api/datasets/generation/${id}/cancel`,{method:'POST'});
    if(response.status==='cancelling')return;
  }catch(e){
    // A cancellation can race with successful completion. Read the authoritative
    // server state before deciding whether to restore the button.
    try{
      const status=await api(`/api/datasets/generation/${id}`);
      if(status.status==='completed'&&status.result){await completeGeneration(status.result);return;}
      if(status.status==='cancelled'||status.status==='failed'){stopGeneration(status.status,status.error||e.message);return;}
    }catch(_){/* the status poll below will surface a genuine connectivity problem */}
    state.generationCancelPending=false;
    $('cancelGeneration').disabled=false;$('cancelGeneration').textContent='Cancel generation';
    $('generationTitle').textContent='Generation still running';
    $('generationMessage').textContent=`Cancel request failed: ${e.message}`;
    toast(e.message,'error');
  }
}
$('cancelGeneration').addEventListener('click',async()=>{
  if(state.generationTerminal)return;
  if(!state.generationId&&state.generationStarting){
    state.generationCancelQueued=true;state.generationCancelPending=true;
    $('cancelGeneration').disabled=true;$('cancelGeneration').textContent='Cancel queued';
    $('generationTitle').textContent='Cancellation queued';
    $('generationMessage').textContent='The start request is still being acknowledged. Cancellation will be sent as soon as the server returns the job ID.';
    return;
  }
  if(!state.generationId)return;
  await requestGenerationCancel(state.generationId);
});
$('openGeneratedPreview').addEventListener('click',async()=>{await loadDatasets();refreshPreviewOptions();$('previewDatasetSelect').value=state.selectedDataset;await loadPreview(state.selectedDataset,0);$('dataPreviewSection').scrollIntoView({behavior:'smooth',block:'start'});});
function refreshPreviewOptions(){renderDatasetSelectors();if(state.selectedDataset)$('previewDatasetSelect').value=state.selectedDataset;}
$('previewDatasetSelect').addEventListener('change',()=>{state.selectedDataset=$('previewDatasetSelect').value;state.previewOffset=0;loadPreview(state.selectedDataset,0);});
$('previewPrev').addEventListener('click',()=>loadPreview(state.selectedDataset,Math.max(0,state.previewOffset-50)));
$('previewNext').addEventListener('click',()=>loadPreview(state.selectedDataset,state.previewOffset+50));
$('downloadDatasetBtn').addEventListener('click',()=>{if(state.selectedDataset)location.href=`/api/datasets/${state.selectedDataset}/download`;});
$('previewToAnalytics').addEventListener('click',()=>{const d=state.datasets.find(x=>x.dataset_id===state.selectedDataset);if(!d||Number(d.rows)<MIN_BENCHMARK_ROWS){toast('This dataset is for preview only. Generate at least 100,000 records to benchmark it.','info');return;}state.analyticsDatasetSelection=d.dataset_id;renderDatasetSelectors();$('analyticsDataset').value=d.dataset_id;syncAnalytics();navigate('analytics');});
async function loadPreview(id,offset=0){if(!id)return;try{const d=await api(`/api/datasets/${id}/preview?limit=50&offset=${offset}`);const dataset=d.dataset;state.previewOffset=offset;$('downloadDatasetBtn').disabled=false;$('previewMeta').textContent=`${fmtNum(dataset.rows)} records · ${dataset.file_size_human} · ${dataset.label}`;$('previewHead').innerHTML='<tr>'+d.headers.map(x=>`<th>${escapeHtml(x)}</th>`).join('')+'</tr>';$('previewBody').innerHTML=d.rows.map(row=>'<tr>'+row.map(v=>`<td>${escapeHtml(v)}</td>`).join('')+'</tr>').join('')||'<tr><td colspan="20" class="empty-cell">No records on this page.</td></tr>';$('previewRange').textContent=`Showing ${d.rows.length?fmtNum(offset+1):0}–${fmtNum(offset+d.rows.length)} of ${fmtNum(dataset.rows)} records`;$('previewPrev').disabled=offset===0;$('previewNext').disabled=offset+d.rows.length>=dataset.rows;$('previewToAnalytics').disabled=Number(dataset.rows)<MIN_BENCHMARK_ROWS;$('previewSchema').innerHTML=dataset.columns.map(c=>`<div class="schema-row"><strong>${escapeHtml(c.name)}</strong><span><i class="type-badge">${escapeHtml(c.type)}</i></span><span class="role-badge">${escapeHtml(c.role)}</span></div>`).join('');$('previewDetails').innerHTML=[['DATASET',dataset.name],['RECORDS',fmtNum(dataset.rows)],['COLUMNS',dataset.columns.length],['FILE SIZE',dataset.file_size_human],['RANDOM SEED',dataset.seed],['GENERATION TIME',fmtSec(dataset.generation_seconds)],['GENERATOR',dataset.writer],['CREATED',new Date(dataset.created_at).toLocaleString()]].map(x=>`<div class="detail-item"><span>${x[0]}</span><strong>${escapeHtml(x[1])}</strong></div>`).join('');state.selectedDataset=id;setWorkflowStep(2);if(Number(dataset.rows)>=MIN_BENCHMARK_ROWS){state.analyticsDatasetSelection=id;$('analyticsDataset').value=id;}syncAnalytics();}catch(e){toast(e.message,'error');}}

$('analyticsDataset').addEventListener('change',()=>{state.analyticsDatasetSelection=$('analyticsDataset').value;syncAnalytics();});
$('operationSelect').addEventListener('change',syncOperationFields);
$('threadCount').addEventListener('change',updatePlan);
$('scheduleSelect').addEventListener('change',()=>{ $('scheduleDescription').textContent=scheduleDescriptions[$('scheduleSelect').value]||''; updatePlan(); });
$('repetitions').addEventListener('change',updatePlan);
$('startBenchmark').addEventListener('click',startBenchmark);
$('viewBenchmarkResults').addEventListener('click',()=>navigate('results'));
function currentDataset(){const id=$('analyticsDataset')?.value||state.analyticsDatasetSelection;return state.datasets.find(d=>d.dataset_id===id)||null;}
function syncAnalytics(){
  const d=currentDataset();
  if(!d){
    if(state.analyticsDatasetId){$('numericColumn').innerHTML='';$('groupColumn').innerHTML='';$('dateColumn').innerHTML='';state.analyticsDatasetId='';}
    updatePlan();
    return;
  }
  // Preserve the user's chosen columns and thread count when the same dataset
  // is reloaded; only rebuild options when the selected dataset actually changes.
  if(state.analyticsDatasetId!==d.dataset_id){
    const nums=d.numeric_columns||[],cats=d.category_columns||[],dates=d.date_columns||[];
    $('numericColumn').innerHTML=nums.map(x=>`<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join('');
    $('groupColumn').innerHTML=cats.map(x=>`<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join('');
    if(nums.includes('Revenue'))$('numericColumn').value='Revenue';
    if(cats.includes('Region'))$('groupColumn').value='Region';else if(cats.includes('Category'))$('groupColumn').value='Category';
    $('dateColumn').innerHTML=dates.map(x=>`<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join('');
    $('threadCount').innerHTML='';
    const core=Math.max(1,Number(state.hardware?.logical_processors||4));
    const availableThreads=[1,2,3,4,6,8,12,16,24,32].filter(x=>x<=state.benchmarkMaxThreads);
    availableThreads.forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=`${n} ${n===1?'thread':'threads'}`;$('threadCount').appendChild(o);});
    // Choose only a value that exists in the options (e.g. a 3-core system
    // offers 1/2/4/6, so it must default to 2 rather than an empty 3).
    const defaultThreads=availableThreads.includes(4)?4:(availableThreads.filter(x=>x<=core).pop()||availableThreads[0]||1);
    $('threadCount').value=String(defaultThreads);
    state.analyticsDatasetId=d.dataset_id;
  }
  syncOperationFields();
}

function syncOperationFields(){
  const op=$('operationSelect').value;
  const needsNumeric=['numeric_stats','filter_gt','group_sum','top_k_groups','comprehensive_analytics'].includes(op);
  const needsCategory=['frequency','group_sum','top_k_groups','comprehensive_analytics'].includes(op);
  $('operationDescription').textContent=operationDescriptions[op]||'';
  $('numericField').classList.toggle('hidden',!needsNumeric);
  $('groupField').classList.toggle('hidden',!needsCategory);
  $('thresholdField').classList.toggle('hidden',!['filter_gt','comprehensive_analytics'].includes(op));
  $('topKField').classList.toggle('hidden',op!=='top_k_groups');
  $('dateField').classList.toggle('hidden',op!=='date_frequency');
  $('dateGranularityField').classList.toggle('hidden',op!=='date_frequency');
  const dateOption=$('operationSelect').querySelector('option[value="date_frequency"]');
  if(dateOption)dateOption.disabled=!(currentDataset()?.date_columns||[]).length;
  if(op==='comprehensive_analytics'){
    $('numericColumnHint').textContent='The numeric values used for totals, averages, spread and threshold counting.';
    $('groupColumnHint').textContent='The category used to produce one count, total and average per group. For Sales, Region is a clear example.';
    $('thresholdHint').textContent='The pipeline also counts how many records have a numeric value greater than this number.';
  }else{
    $('numericColumnHint').textContent='Choose the numeric values to analyse, for example Revenue or Sales Amount.';
    $('groupColumnHint').textContent=op==='frequency'?'Choose a category to count, such as Region, Product or Department.':'Choose a category to group by, such as Region or Category.';
    $('thresholdHint').textContent='Counts only records whose selected numeric value is greater than this number.';
  }
  updatePlan();
}
function benchmarkValidation(){
  const d=currentDataset(),op=$('operationSelect').value;
  const needsNumeric=['numeric_stats','filter_gt','group_sum','top_k_groups','comprehensive_analytics'].includes(op);
  const needsCategory=['frequency','group_sum','top_k_groups','comprehensive_analytics'].includes(op);
  const needsDate=op==='date_frequency';
  if(!state.engine?.openmp)return 'The native OpenMP engine is unavailable. Run setup_windows.bat successfully first.';
  if(state.benchmarkStarting||(state.benchmarkId&&!state.benchmarkTerminal))return 'A benchmark is already running. Open Live Monitor to follow its progress.';
  if(state.generationId&&!state.generationTerminal)return 'Dataset generation is still running. Wait for it to finish or cancel it before starting a benchmark.';
  if(!d)return 'Generate a dataset with at least 100,000 records in Data Lab.';
  if(Number(d.rows)<MIN_BENCHMARK_ROWS)return 'Benchmarking requires at least 100,000 records. Select a dataset with at least 100K rows.';
  if(needsNumeric&&!$('numericColumn').value)return 'This calculation needs a numeric column. Select one to continue.';
  if(needsCategory&&!$('groupColumn').value)return 'This calculation needs a category column. Select one to continue.';
  if(needsDate&&!$('dateColumn').value)return 'This calculation needs a date column. Select one to continue.';
  const threads=Number($('threadCount').value),reps=Number($('repetitions').value),topK=Number($('topKValue').value),threshold=Number($('thresholdValue').value);
  if(!Number.isInteger(threads)||threads<1||threads>state.benchmarkMaxThreads)return `Choose between 1 and ${state.benchmarkMaxThreads} threads for this computer's memory-safe profile.`;
  if(!Number.isInteger(reps)||reps<1||reps>7)return 'Choose between 1 and 7 measured runs per engine.';
  if(op==='top_k_groups'&&(!Number.isInteger(topK)||topK<1||topK>100))return 'Top-K must be between 1 and 100.';
  if(['filter_gt','comprehensive_analytics'].includes(op)&&$('thresholdValue').value.trim()==='')return 'Enter the threshold number before starting.';
  if(['filter_gt','comprehensive_analytics'].includes(op)&&!Number.isFinite(threshold))return 'Enter a valid numeric threshold.';
  if(needsDate&&!['year','month','day'].includes($('dateGranularity').value))return 'Choose Year, Month or Day grouping.';
  return '';
}
function updatePlan(){
  const d=currentDataset(),validation=benchmarkValidation();
  $('planThreads').textContent=`${$('threadCount').value} OpenMP threads · ${$('scheduleSelect').value}`;
  $('planInfo').innerHTML=[['Dataset',d?.name||'Select dataset'],['Records',d?fmtNum(d.rows):'—'],['Calculation',$('operationSelect').selectedOptions[0]?.text||'—'],['Runs',`${$('repetitions').value} sequential + ${$('repetitions').value} parallel`]].map(x=>`<div><span>${x[0]}</span><strong>${escapeHtml(x[1])}</strong></div>`).join('');
  const clientBenchmarkActive=!!(state.benchmarkId&&!state.benchmarkTerminal);
  // Keep this control clickable during an active run so it can take the user
  // directly to Live Monitor instead of showing an inert disabled button.
  $('startBenchmark').disabled=state.benchmarkStarting;
  $('startBenchmark').textContent=state.benchmarkStarting?'Starting benchmark…':clientBenchmarkActive?'Open Live Monitor →':'Start benchmark →';
  $('startBenchmark').title=validation||'Run the same calculation in both engines';
  const message=$('benchmarkValidationMessage');
  if(message){message.textContent=validation||'Ready. Starting will open the Live Monitor automatically.';message.classList.toggle('is-ready',!validation);}
}
function connectBenchmarkJob(job, {reset=true}={}){
  if(!job||!job.job_id)return false;
  state.benchmarkId=job.job_id;
  state.benchmarkTerminal=false;
  state.benchmarkStarting=false;
  state.activeJob=job;
  startPolling();
  state.lastBenchmarkEventId=0;
  if(state.benchmarkPollTimer)clearTimeout(state.benchmarkPollTimer);
  if(reset)resetLive();
  $('executionId').textContent=`JOB ${job.job_id.toUpperCase()}`;
  $('executionStatus').textContent=job.status==='queued'?'Benchmark queued':'Benchmark running';
  const cfg=job.config||{};
  const ds=job.dataset||{};
  if(cfg.dataset_id)state.analyticsDatasetSelection=cfg.dataset_id;
  $('executionSubstatus').textContent=`${ds.name||state.datasets.find(d=>d.dataset_id===cfg.dataset_id)?.name||'Selected dataset'} · ${fmtNum(ds.rows||state.datasets.find(d=>d.dataset_id===cfg.dataset_id)?.rows||0)} records. Restoring the latest live progress.`;
  $('parallelChip').textContent=`${cfg.threads||1} THREADS`;
  $('cancelBenchmark').classList.remove('hidden');
  $('cancelBenchmark').disabled=false;
  $('cancelBenchmark').textContent='Cancel benchmark';
  $('liveNavPulse').classList.add('show');
  navigate('live');

  // Paint a durable server snapshot before relying on WebSocket delivery.
  const snapshot=job.progress_state||job.latest;
  if(snapshot&&snapshot.type)handleBenchmarkEvent(snapshot);
  if(job.latest&&job.latest.type&&job.latest.event_id!==snapshot?.event_id)handleBenchmarkEvent(job.latest);
  if(job.result){state.lastResult=job.result;renderResult();finishBenchmarkUI(job.status);return true;}
  if(['failed','cancelled','verification_failed'].includes(job.status)){
    $('executionSubstatus').textContent=job.error||`Benchmark ${job.status}.`;
    finishBenchmarkUI(job.status);
    return true;
  }

  if(state.benchmarkSocket){try{state.benchmarkSocket.close();}catch{}}
  const proto=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${proto}://${location.host}/ws/benchmarks/${job.job_id}`);
  state.benchmarkSocket=ws;
  ws.onmessage=ev=>{try{handleBenchmarkEvent(JSON.parse(ev.data));}catch(e){console.error('Invalid benchmark event',e);}};
  ws.onerror=()=>{$('executionSubstatus').textContent='Reconnecting to live updates. The status checker will keep this monitor current.';};
  ws.onclose=()=>{if(!state.benchmarkTerminal&&state.benchmarkId===job.job_id){setTimeout(()=>pollBenchmark(job.job_id),250);}};
  pollBenchmark(job.job_id);
  return true;
}

async function recoverActiveBenchmark(){
  try{
    const active=await api('/api/benchmarks/active');
    if(active?.job_id&&['queued','running'].includes(active.status))return connectBenchmarkJob(active);
  }catch(e){console.warn('Could not restore active benchmark',e);}
  return false;
}

async function startBenchmark(){
  if(state.benchmarkStarting){
    toast('Benchmark submission is still being confirmed. Opening its live monitor.','info');
    navigate('live');
    return;
  }
  if(state.benchmarkId&&!state.benchmarkTerminal){
    const attached=await recoverActiveBenchmark();
    if(attached){toast('Reconnected to the running benchmark.','info');return;}
    // The browser may have missed the job's terminal event. Do not leave the
    // Start button trapped behind a stale client-side job ID.
    state.benchmarkId=null;
    state.benchmarkTerminal=true;
  }
  const activeRecovered=await recoverActiveBenchmark();
  if(activeRecovered){toast('Connected to the benchmark that is already running.','info');return;}

  // Validate the current form before changing pages. A valid click then opens
  // Live Monitor immediately; the dataset is refreshed in the background path
  // before the backend job is submitted.
  const selectedId=$('analyticsDataset').value||state.analyticsDatasetSelection;
  if(selectedId)$('analyticsDataset').value=selectedId;
  syncAnalytics();
  const validation=benchmarkValidation();
  if(validation){
    $('benchmarkValidationMessage').textContent=validation;
    toast(validation,'error');
    return;
  }
  const selected=currentDataset();
  if(!selected){
    const message='Select a generated dataset before starting the benchmark.';
    $('benchmarkValidationMessage').textContent=message;
    toast(message,'error');
    return;
  }
  const payload={
    dataset_id:selected.dataset_id,
    operation:$('operationSelect').value,
    column:$('numericColumn').value||null,
    group_column:$('groupColumn').value||null,
    date_column:$('dateColumn').value||null,
    date_granularity:$('dateGranularity').value,
    top_k:Number($('topKValue').value),
    threshold:Number($('thresholdValue').value||0),
    threads:Number($('threadCount').value),
    schedule:$('scheduleSelect').value,
    repetitions:Number($('repetitions').value)
  };

  state.benchmarkStarting=true;
  state.benchmarkTerminal=false;
  state.benchmarkId=null;
  state.lastResult=null;
  if(state.benchmarkPollTimer)clearTimeout(state.benchmarkPollTimer);
  $('activityLog').innerHTML='';
  resetLive();
  navigate('live');
  $('executionId').textContent='SUBMITTING';
  $('executionStatus').textContent='Starting benchmark';
  $('executionSubstatus').textContent=`Preparing ${selected.name} (${fmtNum(selected.rows)} records). Live measurements will appear here.`;
  $('cancelBenchmark').classList.add('hidden');
  $('viewResultsAction').classList.add('hidden');
  $('liveNavPulse').classList.add('show');

  try{
    await loadDatasets();
    const latest=state.datasets.find(item=>item.dataset_id===selected.dataset_id);
    if(!latest)throw new Error('The selected dataset is no longer available. Return to Data Lab and select a saved dataset.');
    state.analyticsDatasetSelection=latest.dataset_id;
    $('analyticsDataset').value=latest.dataset_id;

    const r=await api('/api/benchmarks',{method:'POST',body:JSON.stringify(payload)});
    state.benchmarkStarting=false;
    const startedJob={job_id:r.job_id,status:'queued',config:payload,dataset:latest,latest:{type:'job_status',status:'running',message:'Benchmark queued. Preparing the dataset for both engines.'}};
    connectBenchmarkJob(startedJob,{reset:false});
    $('executionStatus').textContent='Benchmark queued';
    $('executionSubstatus').textContent=`${latest.name} · ${fmtNum(latest.rows)} records. Preparing one shared copy for both engines.`;
  }catch(e){
    state.benchmarkStarting=false;
    if(String(e.message||'').toLowerCase().includes('already active')||String(e.message||'').includes('Another generation or benchmark task is active')){
      const attached=await recoverActiveBenchmark();
      if(attached){toast('Connected to the active benchmark.','info');updatePlan();return;}
    }
    state.benchmarkTerminal=true;
    state.benchmarkId=null;
    $('liveNavPulse').classList.remove('show');
    $('cancelBenchmark').classList.add('hidden');
    $('executionStatus').textContent='Benchmark could not start';
    $('executionSubstatus').textContent=e.message;
    $('executionId').textContent='NOT STARTED';
    $('viewResultsAction').classList.add('hidden');
    setLiveSummary('Benchmark could not start',e.message,'error');
    const b=$('startBenchmark');
    if(b){b.disabled=false;b.textContent='Start benchmark →';}
    toast(`Benchmark could not start: ${e.message}`,'error');
  }finally{
    updatePlan();
  }
}

function setLiveSummary(title, body, tone='info'){
  const box=$('livePlainSummary');
  if(!box)return;
  box.dataset.tone=tone;
  box.innerHTML=`<strong>${escapeHtml(title)}</strong><p>${escapeHtml(body)}</p>`;
}
function resetLive(){
  stopBenchmarkPhaseTimer();
  state.lastBenchmarkEventId=0;
  ['seqWall','seqKernel','seqRate','parWall','parKernel','parRate'].forEach(id=>$(id).textContent='—');
  ['seqWallLabel','parWallLabel'].forEach(id=>$(id).textContent='LATEST RUN · COMPUTE TIME');
  ['seqKernelLabel','parKernelLabel'].forEach(id=>$(id).textContent='LATEST RUN · NATIVE TIME');
  ['seqRateLabel','parRateLabel'].forEach(id=>$(id).textContent='LATEST RUN · THROUGHPUT');
  $('liveElapsedLabel').textContent='CURRENT RUN ELAPSED';
  $('seqStatus').textContent='Waiting for benchmark';
  $('parStatus').textContent='Waiting for benchmark';
  $('liveProgress').innerHTML='0<small>%</small>';
  $('liveProgressBar').style.width='0%';
  $('liveRecords').textContent='0 / 0 records';
  $('liveElapsed').innerHTML='0.0<small>s</small>';
  $('liveEta').textContent='—';
  $('liveThroughput').innerHTML='0<small>K/s</small>';
  $('liveThreads').textContent='0';
  $('livePhase').textContent='QUEUED';
  $('liveMode').textContent='—';
  $('liveThreadLabel').textContent='Observed OpenMP threads';
  setLiveSummary('Preparing the comparison','The app will run the same calculation first with one CPU thread and then with the selected OpenMP thread count.');
}
function addActivity(text,type='info'){
  const log=$('activityLog');
  if(log.querySelector('.empty-small'))log.innerHTML='';
  const row=document.createElement('div');
  row.className='activity-entry';
  row.innerHTML=`<i></i><span>${escapeHtml(text)}</span><time>${new Date().toLocaleTimeString()}</time>`;
  log.prepend(row);
  while(log.children.length>35)log.lastChild.remove();
}
function stopBenchmarkPhaseTimer(){
  if(state.benchmarkPhaseTimer){clearInterval(state.benchmarkPhaseTimer);state.benchmarkPhaseTimer=null;}
  state.benchmarkPhaseStartedAt=0;state.benchmarkPhaseBaseElapsed=0;
}
function startBenchmarkPhaseTimer(baseElapsed=0){
  stopBenchmarkPhaseTimer();
  state.benchmarkPhaseBaseElapsed=Math.max(0,Number(baseElapsed)||0);
  state.benchmarkPhaseStartedAt=performance.now();
  state.benchmarkPhaseTimer=setInterval(()=>{
    if(!state.benchmarkPhaseStartedAt||state.benchmarkTerminal)return;
    const elapsed=state.benchmarkPhaseBaseElapsed+(performance.now()-state.benchmarkPhaseStartedAt)/1000;
    $('liveElapsed').innerHTML=`${elapsed.toFixed(1)}<small>s</small>`;
  },200);
}

function applyMedianComparisonMetrics(result){
  if(!result||!result.sequential||!result.parallel)return;
  for(const [mode,prefix] of [['sequential','seq'],['parallel','par']]){
    const summary=result[mode]||{};
    const metrics=summary.display_metrics||{};
    const compute=Number(metrics.compute_seconds??summary.median_wall_seconds);
    const calculation=Number(metrics.calculation_seconds??summary.median_kernel_seconds);
    const throughput=Number(metrics.throughput_records_per_second??summary.median_throughput);
    const runs=Number(metrics.runs_included??summary.runs?.length??0);
    const workers=Number(summary.representative_observed_threads??(mode==='sequential'?1:result.config?.threads??0));
    const representative=Number(summary.representative_repetition||0);
    $(`${prefix}Wall`).textContent=fmtSec(compute);
    $(`${prefix}Kernel`).textContent=fmtSec(calculation);
    $(`${prefix}Rate`).textContent=`${fmtNum(throughput)} records/s`;
    $(`${prefix}WallLabel`).textContent='MEDIAN COMPUTE TIME';
    $(`${prefix}KernelLabel`).textContent='MEDIAN NATIVE TIME';
    $(`${prefix}RateLabel`).textContent='MEDIAN THROUGHPUT';
    $(`${prefix}Status`).textContent=`Median of ${runs} runs · ${workers} thread(s) observed${representative?` · Run ${representative} was closest to median`:''}`;
  }
  const parallelMedian=Number(result.parallel.display_metrics?.compute_seconds??result.parallel.median_wall_seconds);
  $('liveElapsed').innerHTML=`${parallelMedian.toFixed(3)}<small>s</small>`;
  $('liveElapsedLabel').textContent='PARALLEL MEDIAN TIME';
  $('liveProgress').innerHTML='100<small>%</small>';
  $('liveProgressBar').style.width='100%';
  $('liveRecords').textContent=`${fmtNum(result.dataset_rows)} / ${fmtNum(result.dataset_rows)} records`;
}
function showBenchmarkExplanation(result){
  const analysis=result?.analysis||{};
  const box=$('livePlainSummary');
  if(!box)return;
  const title=analysis.headline||'Comparison complete';
  const explanation=analysis.audience_explanation||'Both processing methods have completed.';
  const validation=analysis.recommendation_validation||result?.recommendation_validation||{};
  let suggestion='';
  if(['verified','verified_next_size'].includes(validation.status)&&validation.recommended_configuration){
    const config=validation.recommended_configuration;
    suggestion=`Tested and verified: ${fmtNum(config.threads)} threads · ${fmtNum(config.dataset_rows)} records (${Number(config.speedup_vs_single).toFixed(2)}× faster; ${config.measured_runs} measured runs).`;
  }else if(validation.status==='skipped_unstable'){
    suggestion=`Next: repeat the same dataset and thread settings while the computer is idle (timing varied up to ${Number(validation.max_baseline_spread_percent||0).toFixed(1)}%); no untested setup is recommended.`;
  }else if(validation.status==='skipped_unverified'){
    suggestion='Next: resolve result verification first; no performance recommendation is available.';
  }else if(validation.status==='skipped_insufficient_repetitions'){
    suggestion='Next: repeat with at least 3 measured runs; no thread setting has been ranked yet.';
  }else if(validation.status==='skipped_cancelled'){
    suggestion='The benchmark result is saved; automatic recommendation testing was cancelled.';
  }else if(validation.status==='skipped_size_limit'){
    suggestion=validation.message||'The next dataset size was not trialled; no larger-size winner is claimed.';
  }else if(validation.status==='skipped_resources'||validation.status==='skipped_template'||validation.status==='skipped_no_workers'||validation.status==='skipped_probe_error'){
    suggestion=validation.message||'The next-size trial could not be completed; no untested setting is recommended.';
  }else if(validation.status==='no_larger_preset'){
    suggestion='The largest supported dataset is already selected; no larger-size recommendation is available.';
  }else{
    const trial=validation.next_size_trial;
    suggestion=trial?.status==='no_verified_parallel_win_at_next_size'
      ?`The next size (${fmtNum(trial.next_dataset_rows)} rows) was also tested; no multi-thread setting won reliably. No faster setup is verified.`
      :'No tested multi-thread setting beat one thread reliably. No untested size or thread count is being recommended.';
  }
  box.dataset.tone=result?.verified?'success':'error';
  box.innerHTML=`<strong>${escapeHtml(title)}</strong><p>${escapeHtml(explanation)}</p><p class="live-analysis-next"><b>Verified next step:</b> ${escapeHtml(suggestion)}</p>`;
}

function handleBenchmarkEvent(e){
  if(!e)return;
  if(e.type==='heartbeat')return;
  const eventId=Number(e.event_id||0);
  if(eventId&&eventId<=state.lastBenchmarkEventId)return;
  if(eventId)state.lastBenchmarkEventId=eventId;
  if(e.type==='job_status'){
    $('executionStatus').textContent=`Benchmark ${e.status}`;
    $('executionSubstatus').textContent=e.message||'';
    addActivity(e.message||`Job ${e.status}`);
    if(e.status==='running')setLiveSummary('Benchmark is running','The app is measuring the selected calculation on your computer. The two methods will be compared after both runs finish.');
    if(['failed','cancelled'].includes(e.status)){
      stopBenchmarkPhaseTimer();
      const total=Number(e.total_records||state.activeJob?.dataset?.rows||0);
      $('liveProgress').innerHTML='0<small>%</small>';
      $('liveProgressBar').style.width='0%';
      $('liveProgressBar').parentElement.setAttribute('aria-valuenow','0');
      $('liveRecords').textContent=`0 / ${fmtNum(total)} records benchmarked`;
      $('liveThroughput').innerHTML='0<small>K/s</small>';
      $('liveEta').textContent='—';
      $('liveThreads').textContent='—';
      $('livePhase').textContent=e.status==='failed'?'FAILED':'CANCELLED';
      $('liveMode').textContent=e.phase||e.status.toUpperCase();
      $('seqStatus').textContent='Not completed · benchmark stopped';
      $('parStatus').textContent='Not completed · benchmark stopped';
      finishBenchmarkUI(e.status,e.message||'No performance result was produced.');
    }
    return;
  }
  if(e.type==='preparation_start'||e.type==='preparation_progress'||e.type==='preparation_complete'){
    const total=Number(e.total_records||0),done=Number(e.records_processed||0);
    if(e.type==='preparation_start')startBenchmarkPhaseTimer(0);
    else if(e.type==='preparation_progress')startBenchmarkPhaseTimer(Number(e.elapsed_seconds)||0);
    else stopBenchmarkPhaseTimer();
    const progress=Math.max(0,Math.min(100,Number(e.progress||0)));
    $('executionStatus').textContent=e.type==='preparation_complete'?'Dataset ready for measurement':'Preparing benchmark data';
    $('executionSubstatus').textContent=e.message||'Reading and preparing the dataset once for both engines.';
    $('liveMode').textContent='PREPARATION';
    $('livePhase').textContent='PREPARING';
    $('liveProgress').innerHTML=`${progress.toFixed(1)}<small>%</small>`;
    $('liveProgressBar').style.width=`${progress}%`;
    $('liveProgressBar').parentElement.setAttribute('aria-valuenow',String(progress));
    $('liveRecords').textContent=`${fmtNum(done)} / ${fmtNum(total)} records`;
    $('liveElapsed').innerHTML=`${Number(e.elapsed_seconds||0).toFixed(2)}<small>s</small>`;
    $('liveEta').textContent=e.estimated_remaining_seconds==null?'—':fmtSec(e.estimated_remaining_seconds);
    $('liveThroughput').innerHTML=`${(Number(e.records_per_second||0)/1000).toFixed(1)}<small>K/s</small>`;
    $('liveThreads').textContent='—';
    $('liveThreadLabel').textContent=e.storage_mode||'CSV preparation';
    setLiveSummary('Preparing the dataset once',`${fmtNum(done)} of ${fmtNum(total)} records prepared. Both processing engines will use this same data, so the comparison is fair.`);
    return;
  }
  if(e.type==='recommendation_generation_progress'){
    const p=Math.max(0,Math.min(100,Number(e.progress||0)));
    $('executionStatus').textContent='Generating trial data to verify the next size';
    $('executionSubstatus').textContent=e.message||'Creating temporary test data before suggesting a larger dataset.';
    $('liveMode').textContent='AUTO TEST';
    $('livePhase').textContent='GENERATING';
    $('liveProgress').innerHTML=`${p.toFixed(1)}<small>%</small>`;
    $('liveProgressBar').style.width=`${p}%`;
    $('liveProgressBar').parentElement.setAttribute('aria-valuenow',String(p));
    $('liveRecords').textContent=`${fmtNum(e.generated_rows||0)} / ${fmtNum(e.total_rows||e.candidate_rows||0)} trial records`;
    $('liveElapsed').innerHTML=`${Number(e.elapsed_seconds||0).toFixed(1)}<small>s</small>`;
    $('liveThroughput').innerHTML=`${(Number(e.records_per_second||0)/1000).toFixed(1)}<small>K/s</small>`;
    $('liveEta').textContent='';
    $('liveThreads').textContent='—';
    $('liveThreadLabel').textContent='Temporary data · removed after verification';
    setLiveSummary('Testing the next dataset size',e.message||'The system is generating a temporary dataset and will remove it after the trial.');
    return;
  }
  if(['recommendation_preparation_start','recommendation_preparation_progress','recommendation_preparation_complete'].includes(e.type)){
    const p=Math.max(0,Math.min(100,Number(e.progress||0)));
    const done=Number(e.records_processed||0),total=Number(e.total_records||0);
    $('executionStatus').textContent='Preparing trial data for recommendation';
    $('executionSubstatus').textContent=e.message||'Preparing the generated trial dataset once for both engines.';
    $('liveMode').textContent='AUTO TEST';
    $('livePhase').textContent='PREPARING';
    $('liveProgress').innerHTML=`${p.toFixed(1)}<small>%</small>`;
    $('liveProgressBar').style.width=`${p}%`;
    $('liveProgressBar').parentElement.setAttribute('aria-valuenow',String(p));
    $('liveRecords').textContent=`${fmtNum(done)} / ${fmtNum(total)} trial records prepared`;
    $('liveElapsed').innerHTML=`${Number(e.elapsed_seconds||0).toFixed(1)}<small>s</small>`;
    $('liveThroughput').innerHTML=`${(Number(e.records_per_second||0)/1000).toFixed(1)}<small>K/s</small>`;
    $('liveEta').textContent=e.estimated_remaining_seconds==null?'—':fmtSec(e.estimated_remaining_seconds);
    $('liveThreads').textContent='—';
    $('liveThreadLabel').textContent=e.storage_mode||'Temporary trial data';
    setLiveSummary('Preparing the trial dataset',e.message||'Converting the temporary dataset for a fair single-thread and parallel comparison.');
    return;
  }
  if(['recommendation_probe_start','recommendation_probe_progress','recommendation_probe_complete'].includes(e.type)){
    $('executionStatus').textContent='Verifying suggested thread settings';
    $('executionSubstatus').textContent=e.message||'Testing thread-count alternatives on the same prepared dataset.';
    $('liveMode').textContent='AUTOMATIC TEST';
    $('livePhase').textContent=e.type==='recommendation_probe_complete'?'CHECK COMPLETE':'VERIFYING';
    $('liveThreadLabel').textContent=e.candidate_threads?`${e.candidate_threads} threads under test`:'Same dataset · alternative thread counts';
    setLiveSummary('Checking the next configuration',e.message||'The system is measuring alternatives before making a recommendation.');
    return;
  }
  if(e.type==='phase_start'){
    startBenchmarkPhaseTimer(0);
    const modeName=e.mode==='parallel'?'multi-thread OpenMP':'single-thread sequential';
    $('executionStatus').textContent=`${e.mode==='parallel'?'Multi-thread':'Single-thread'} run · ${e.repetition}/${e.repetitions}`;
    $('executionSubstatus').textContent=e.message||'Preparing the data reader…';
    $('liveMode').textContent=e.mode==='parallel'?'PARALLEL':'SEQUENTIAL';
    $('livePhase').textContent=e.mode==='parallel'?'MULTI-THREAD':'SINGLE-THREAD';
    $('liveProgress').innerHTML='0<small>%</small>';
    $('liveProgressBar').style.width='0%';
    $('liveProgressBar').parentElement.setAttribute('aria-valuenow','0');
    $('liveRecords').textContent=`0 / ${fmtNum(e.total_records)} records`;
    $('liveThreads').textContent=e.mode==='parallel'?'—':'1';
    $('liveThreadLabel').textContent=e.mode==='parallel'?`${e.configured_threads||'Configured'} threads requested · waiting for measured activity`:'One CPU thread · sequential baseline';
    $('parallelChip').textContent=`${e.configured_threads||1} THREADS`;
    setLiveSummary(
      e.mode==='parallel'?'Now testing multiple CPU threads':'First test: one CPU thread',
      e.mode==='parallel'
        ? `OpenMP is splitting the same ${fmtNum(e.total_records)} records across up to ${e.configured_threads} configured CPU threads. We will compare its measured time with the single-thread run.`
        : `One CPU thread is processing ${fmtNum(e.total_records)} records from start to finish. This is the baseline used to judge the multi-thread run.`
    );
    addActivity(e.message||`Starting ${modeName} run.`);
    if(e.mode==='sequential')$('seqStatus').textContent=`Running repetition ${e.repetition}`;
    else $('parStatus').textContent=`Running repetition ${e.repetition}`;
    return;
  }
  if(e.type==='phase_progress'){
    startBenchmarkPhaseTimer(Number(e.elapsed_seconds)||0);
    const p=Math.max(0,Math.min(100,Number(e.progress||0)));
    const modeName=e.mode==='parallel'?'Multi-thread':'Single-thread';
    $('liveProgress').innerHTML=`${p.toFixed(1)}<small>%</small>`;
    $('liveProgressBar').style.width=`${p}%`;
    $('liveProgressBar').parentElement.setAttribute('aria-valuenow',String(p.toFixed(1)));
    $('liveRecords').textContent=`${fmtNum(e.records_processed)} / ${fmtNum(e.total_records)} records`;
    $('liveElapsed').innerHTML=`${Number(e.elapsed_seconds||0).toFixed(2)}<small>s</small>`;
    $('liveEta').textContent=e.estimated_remaining_seconds==null?'—':fmtSec(e.estimated_remaining_seconds);
    $('liveThroughput').innerHTML=`${(Number(e.records_per_second||0)/1000).toFixed(1)}<small>K/s</small>`;
    $('liveThreads').textContent=e.observed_threads_in_last_chunk??0;
    $('liveThreadLabel').textContent=e.mode==='parallel'
      ?`${e.configured_threads} configured · ${e.observed_threads_in_last_chunk??0} observed in latest chunk`
      :'One CPU thread';
    $('livePhase').textContent=modeName.toUpperCase();
    setLiveSummary(
      `${modeName} run is in progress`,
      `${fmtNum(e.records_processed)} of ${fmtNum(e.total_records)} records processed (${p.toFixed(1)}%). Current rate: ${fmtNum(e.records_per_second)} records per second.`
    );
    return;
  }
  if(e.type==='phase_complete'){
    stopBenchmarkPhaseTimer();
    $('liveElapsed').innerHTML=`${Number(e.wall_seconds||0).toFixed(3)}<small>s</small>`;
    $('liveThroughput').innerHTML=`${(Number(e.throughput_records_per_second||0)/1000).toFixed(1)}<small>K/s</small>`;
    $('liveThreads').textContent=String(e.observed_threads_max??(e.mode==='sequential'?1:0));
    $('liveRecords').textContent=`${fmtNum(e.records_processed)} / ${fmtNum(e.records_processed)} records`;
    $('liveProgress').innerHTML='100<small>%</small>';
    $('liveProgressBar').style.width='100%';
    const prefix=e.mode==='sequential'?'seq':'par';
    $('liveProgress').innerHTML='100<small>%</small>';
    $('liveProgressBar').style.width='100%';
    $('liveRecords').textContent=`${fmtNum(e.records_processed)} / ${fmtNum(e.records_processed)} records`;
    $(prefix+'Wall').textContent=fmtSec(e.wall_seconds);
    $(prefix+'Kernel').textContent=fmtSec(e.kernel_seconds);
    $(prefix+'Rate').textContent=`${fmtNum(e.throughput_records_per_second)} records/s`;
    $(`${prefix}Status`).textContent=`Run ${e.repetition} complete · ${e.observed_threads_max} thread(s) observed`;
    if(e.mode==='sequential'){
      setLiveSummary('Single-thread baseline complete',`The one-thread calculation finished in ${fmtSec(e.wall_seconds)}. The multi-thread OpenMP test will show whether using more CPU threads helps this workload.`);
    }else{
      setLiveSummary('Multi-thread run complete',`The OpenMP calculation finished in ${fmtSec(e.wall_seconds)} with ${e.observed_threads_max} thread(s) observed. The final comparison will use the measured overall times and verify that both answers match.`);
    }
    addActivity(`${e.mode} run ${e.repetition} completed in ${fmtSec(e.wall_seconds)} compute time / ${fmtSec(e.kernel_seconds)} calculation time.`);
    return;
  }
  if(e.type==='job_complete'){
    state.lastResult=e.result;
    renderResult();
    applyMedianComparisonMetrics(e.result);
    showBenchmarkExplanation(e.result);
    finishBenchmarkUI(e.status);
    addActivity('Benchmark completed. Sequential and parallel outputs were compared.');
    toast(e.status==='completed'?'Benchmark complete and results verified.':'Benchmark complete, but result verification failed.',e.status==='completed'?'success':'error');
    loadHistory();
  }
}
function finishBenchmarkUI(status, detail=''){
  if(state.benchmarkTerminal)return;
  if(['completed','verification_failed'].includes(status)&&state.lastResult){applyMedianComparisonMetrics(state.lastResult);showBenchmarkExplanation(state.lastResult);}
  stopBenchmarkPhaseTimer();
  state.benchmarkTerminal=true;
  state.benchmarkStarting=false;
  startPolling();
  if(state.benchmarkPollTimer)clearTimeout(state.benchmarkPollTimer);
  $('cancelBenchmark').classList.add('hidden');
  $('cancelBenchmark').disabled=false;
  $('cancelBenchmark').textContent='Cancel benchmark';
  $('liveNavPulse').classList.remove('show');
  $('viewResultsAction').classList.toggle('hidden',!['completed','verification_failed'].includes(status));
  $('executionStatus').textContent=status==='completed'?'Benchmark completed':status==='verification_failed'?'Result verification needs review':`Benchmark ${status}`;
  if(status==='completed'){
    const r=state.lastResult||{};
    const speed=Number(r.speedup_wall);
    const note=Number.isFinite(speed)
      ?(speed>1.02?`The full parallel run was ${speed.toFixed(2)}× as fast as the single-thread run.`:speed<0.98?`The single-thread run was faster for this workload. Parallel speedup was ${speed.toFixed(2)}×.`:'Both methods took almost the same overall time.')
      :'The measurements are ready to review.';
    if(!state.lastResult)setLiveSummary('Benchmark finished',note,r.verified?'success':'error');
    $('executionSubstatus').textContent='The measured comparison is ready. The explanation below shows why this outcome occurred and what to test next.';
  }else if(status==='verification_failed'){
    if(!state.lastResult)setLiveSummary('The answer check needs review','Both processing methods finished, but their calculated answers differed. Do not treat the speed comparison as valid until the mismatch is understood.','error');
    $('executionSubstatus').textContent='Both engines finished, but their outputs did not match. The speed comparison is withheld until the mismatch is understood.';
  }else{
    const heading=status==='cancelled'?'Benchmark cancelled':'Benchmark stopped';
    const technical=detail||$('executionSubstatus').textContent||`The benchmark ${status}.`;
    setLiveSummary(heading,`${technical} No performance conclusion should be drawn from an incomplete run.`,status==='cancelled'?'info':'error');
    $('executionSubstatus').textContent=technical;
  }
  const b=$('startBenchmark');
  if(b){b.disabled=false;b.textContent='Start benchmark →';}
  if(status==='completed'||status==='verification_failed')toast(status==='completed'?'Benchmark completed. Results are ready.':'The benchmark finished, but result verification needs review.',status==='completed'?'success':'error');
}
async function pollBenchmark(id){
  if(!id||id!==state.benchmarkId||state.benchmarkTerminal)return;
  try{
    const d=await api(`/api/benchmarks/${id}`);
    if(d.progress_state?.type)handleBenchmarkEvent(d.progress_state);
    if(d.latest)handleBenchmarkEvent(d.latest);
    if(d.result){
      state.lastResult=d.result;
      renderResult();
      finishBenchmarkUI(d.status);
    }else if(['failed','cancelled','verification_failed'].includes(d.status)){
      $('executionSubstatus').textContent=d.error||d.latest?.message||`Benchmark ${d.status}.`;
      finishBenchmarkUI(d.status);
    }else if(!state.benchmarkTerminal){
      state.benchmarkPollTimer=setTimeout(()=>pollBenchmark(id),400);
    }
  }catch(e){
    $('executionSubstatus').textContent=`Reconnecting to benchmark status: ${e.message}`;
    if(!state.benchmarkTerminal)state.benchmarkPollTimer=setTimeout(()=>pollBenchmark(id),1200);
  }
}
$('cancelBenchmark').addEventListener('click',async()=>{if(!state.benchmarkId||state.benchmarkTerminal)return;$('cancelBenchmark').disabled=true;$('cancelBenchmark').textContent='Cancelling…';$('executionSubstatus').textContent='Cancellation requested. The current read/compute chunk will finish, then the run will stop.';try{await api(`/api/benchmarks/${state.benchmarkId}/cancel`,{method:'POST'});toast('Cancellation requested.','info');}catch(e){$('cancelBenchmark').disabled=false;$('cancelBenchmark').textContent='Cancel benchmark';toast(e.message,'error');}});

async function refreshMonitor(){if(state.monitorRefreshPending)return;state.monitorRefreshPending=true;const benchmarkActive=!!(state.benchmarkId&&!state.benchmarkTerminal);try{const d=await api(benchmarkActive?'/api/hardware/live?light=true':'/api/hardware/live');renderMonitor(d);}catch(e){console.warn('Hardware telemetry refresh failed',e);const status=$('liveTelemetryStatus');if(status){status.dataset.connected='false';status.textContent=`LOCAL SYSTEM TELEMETRY UNAVAILABLE · ${e.message||'Could not read CPU / memory metrics'}`;}}finally{state.monitorRefreshPending=false;}}
function renderMonitor(d){state.live=d;const cpu=d.cpu||{},mem=d.memory||{};$('monCpu').innerHTML=`${Number(cpu.total_percent||0).toFixed(1)}<small>%</small>`;$('monCpuMeta').textContent=`${cpu.per_core_percent?.length||0} logical processors`;$('monRam').innerHTML=`${Number(mem.percent||0).toFixed(1)}<small>%</small>`;$('monRamMeta').textContent=`${fmtBytes(mem.used_bytes)} / ${fmtBytes(mem.total_bytes)}`;$('monProcCpu').innerHTML=`${Number(cpu.process_percent||0).toFixed(1)}<small>%</small>`;$('monProcRam').innerHTML=`${(mem.process_rss_bytes/1024/1024||0).toFixed(1)}<small> MB</small>`;$('monProcThreads').textContent=`${mem.process_threads||0} process threads`;
$('coreGrid').innerHTML=(cpu.per_core_percent||[]).map((v,i)=>`<div class="core-item"><span>CPU ${i+1}</span><strong>${Number(v).toFixed(0)}%</strong><div class="core-track"><i style="width:${Math.min(100,v)}%"></i></div></div>`).join('')||'<div class="empty-small">Per-core data unavailable.</div>';
$('memoryBreakdown').innerHTML=[['Used',mem.used_bytes,mem.percent],['Available',mem.available_bytes,mem.total_bytes?mem.available_bytes/mem.total_bytes*100:0],['Application RSS',mem.process_rss_bytes,mem.total_bytes?mem.process_rss_bytes/mem.total_bytes*100:0]].map(([label,bytes,pct])=>`<div class="resource-line"><span>${label}</span><div class="resource-bar"><i style="width:${Math.min(100,Number(pct)||0)}%"></i></div><strong>${fmtBytes(bytes)}</strong></div>`).join('');
$('storageTable').innerHTML=(d.storage||[]).map(v=>`<tr><td>${escapeHtml(v.mountpoint)}</td><td>${escapeHtml(v.filesystem||'—')}</td><td>${fmtBytes(v.used_bytes)}</td><td>${fmtBytes(v.free_bytes)}</td><td>${Number(v.percent).toFixed(1)}%</td></tr>`).join('')||'<tr><td colspan="5" class="empty-cell">Storage information unavailable.</td></tr>';
$('gpuPanel').innerHTML=(d.gpu_devices||[]).map(g=>`<div class="gpu-device"><strong>GPU ${g.index}: ${escapeHtml(g.name)}</strong><small>GPU ${g.gpu_util_percent}% · VRAM ${fmtBytes(g.memory_used_bytes)} / ${fmtBytes(g.memory_total_bytes)}</small><small>Temperature ${g.temperature_c??'—'} °C · Power ${g.power_watts??'—'} W</small></div>`).join('')||'<div class="empty-small">No supported NVIDIA NVML device detected, or telemetry is unavailable. GPU monitoring is optional.</div>';
$('netDown').textContent=fmtRate(d.network?.bytes_recv_per_sec);$('netUp').textContent=fmtRate(d.network?.bytes_sent_per_sec);$('netReceived').textContent=fmtBytes(d.network?.bytes_recv_total);$('netSent').textContent=fmtBytes(d.network?.bytes_sent_total);$('diskRead').textContent=fmtRate(d.disk_io?.read_bytes_per_sec);$('diskWrite').textContent=fmtRate(d.disk_io?.write_bytes_per_sec);$('appDiskRead').textContent=fmtRate(d.application_io?.read_bytes_per_sec);$('appDiskWrite').textContent=fmtRate(d.application_io?.write_bytes_per_sec);$('appDiskReadTotal').textContent=fmtBytes(d.application_io?.read_bytes_total);$('appDiskWriteTotal').textContent=fmtBytes(d.application_io?.write_bytes_total);
$('processTable').innerHTML=(d.processes||[]).map(p=>`<tr><td>${escapeHtml(p.name)}</td><td>${p.pid}</td><td>${Number(p.cpu_percent||0).toFixed(1)}%</td><td>${fmtBytes(p.memory_bytes)}</td><td>${escapeHtml(p.status||'—')}</td></tr>`).join('')||'<tr><td colspan="5" class="empty-cell">Process information unavailable.</td></tr>';
$('ovCpu').innerHTML=`${Number(cpu.total_percent||0).toFixed(1)}<small>%</small>`;$('ovCpuBar').style.width=`${Math.min(100,cpu.total_percent||0)}%`;$('ovRam').innerHTML=`${(mem.used_bytes/1024**3||0).toFixed(1)}<small> GB</small>`;$('ovRamSub').textContent=`${fmtBytes(mem.available_bytes)} available`;$('ovRamBar').style.width=`${Math.min(100,mem.percent||0)}%`;$('ovAppRam').innerHTML=`${(mem.process_rss_bytes/1024**2||0).toFixed(0)}<small> MB</small>`;$('ovAppThreads').textContent=`${mem.process_threads||0} process threads`;$('ovAppRamBar').style.width=`${Math.min(100,mem.process_rss_bytes/Math.max(1,mem.total_bytes)*100)}%`;
const logicalCount=Math.max(1,Number(state.hardware?.logical_processors||cpu.per_core_percent?.length||1));const appCpuShare=Math.min(100,Math.max(0,Number(cpu.process_percent||0)/logicalCount));const appRamShare=mem.total_bytes?Math.min(100,Number(mem.process_rss_bytes||0)/Number(mem.total_bytes)*100):0;pushHistory(state.cpuHistory,cpu.total_percent);pushHistory(state.ramHistory,mem.percent);pushHistory(state.processCpuHistory,appCpuShare);pushHistory(state.processRamHistory,appRamShare);$('liveCpuCurrent').textContent=`${Number(cpu.total_percent||0).toFixed(1)}%`;$('liveRamCurrent').textContent=`${Number(mem.percent||0).toFixed(1)}%`;$('liveCpuSource').textContent=`${state.hardware?.hostname||'This computer'} · whole-system average across ${logicalCount} logical processors`;$('liveRamSource').textContent=`${state.hardware?.hostname||'This computer'} · physical RAM used by all local processes`;$('liveCpuSample').textContent=`App CPU share: ${appCpuShare.toFixed(1)}% of total machine capacity · process CPU: ${Number(cpu.process_percent||0).toFixed(1)}% of one core`;$('liveRamSample').textContent=`App memory: ${(Number(mem.process_rss_bytes||0)/1024**2).toFixed(1)} MB RSS · total RAM: ${fmtBytes(mem.total_bytes)}`;const stamp=Number(d.sampled_at||d.timestamp||0);const clock=stamp?new Date(stamp*1000).toLocaleTimeString():'just now';$('liveTelemetryStatus').textContent=`LIVE FROM ${String(state.hardware?.hostname||'THIS COMPUTER').toUpperCase()} · OS SAMPLE UPDATED ${clock} · REFRESHING EVERY 1 SECOND`;$('liveTelemetryStatus').dataset.connected='true';window.dispatchEvent(new CustomEvent('pbda:telemetry',{detail:{live:d,appCpuShare,hardware:state.hardware}}));}

async function loadHistory(){try{const d=await api('/api/history?limit=200');state.history=d.experiments||[];$('historyTable').innerHTML=state.history.map(e=>`<tr><td><strong>${escapeHtml(e.experiment_id)}</strong></td><td>${escapeHtml(e.dataset_name)}</td><td>${escapeHtml(e.operation)}</td><td>${new Date(e.created_at).toLocaleString()}</td><td>${escapeHtml(e.status)}</td><td>${e.verified?'<span class="check-label">✓ Yes</span>':'<span class="muted">No</span>'}</td><td><button class="table-action" data-report="${e.experiment_id}">JSON report</button></td></tr>`).join('')||'<tr><td colspan="7" class="empty-cell">No experiments have been recorded yet.</td></tr>';$('historyTable').querySelectorAll('[data-report]').forEach(b=>b.addEventListener('click',()=>location.href=`/api/reports/${b.dataset.report}/json`));$('recentExperiments').innerHTML=state.history.slice(0,4).map(e=>`<div class="recent-row"><div class="recent-row-left"><strong>${escapeHtml(e.dataset_name)}</strong><small>${escapeHtml(e.operation)} · ${new Date(e.created_at).toLocaleDateString()}</small></div><div class="recent-row-right"><strong>${e.verified?'VERIFIED':'CHECK'}</strong><small>${escapeHtml(e.experiment_id)}</small></div></div>`).join('')||'<div class="empty-small">No completed experiments yet.</div>';}catch(e){toast(e.message,'error');}}
$('refreshHistory').addEventListener('click',loadHistory);
function renderResult(){
  const r=state.lastResult;
  if(!r){$('resultPlaceholder').classList.remove('hidden');$('resultContent').classList.add('hidden');return;}
  const computeSpeed=Number(r.speedup_wall);
  const kernelSpeed=Number(r.speedup_kernel);
  const prep=Number(r.preparation_seconds||0);
  const endToEnd=Number(r.end_to_end_speedup);
  const speedText=!Number.isFinite(computeSpeed)
    ?'The compute speedup could not be calculated from these measurements.'
    :computeSpeed>1.02
      ?`The multi-thread calculation was ${computeSpeed.toFixed(2)}× as fast as the single-thread calculation.`
      :computeSpeed<0.98
        ?`The single-thread calculation was faster for this workload. Multi-thread speedup was ${computeSpeed.toFixed(2)}×, so OpenMP overhead outweighed the benefit on this test.`
        :'Both calculation methods took almost the same time.';
  const kernelNote=Number.isFinite(kernelSpeed)?` Native-kernel-only speedup: ${kernelSpeed.toFixed(2)}×.`:'';
  const prepNote=`The CSV was read and prepared once in ${fmtSec(prep)} before timing; both engines reused the same prepared data.`;
  const e2eNote=Number.isFinite(endToEnd)?` Including that shared preparation, end-to-end speedup was ${endToEnd.toFixed(2)}×.`:'';
  if($('resultInsight'))$('resultInsight').textContent=`${r.analysis?.audience_explanation||speedText}${kernelNote} ${prepNote}${e2eNote} ${r.verified?'Both engines returned matching results.':'The result check did not pass; review the result status before drawing conclusions.'}`;
  if(r.analysis?.recommended_next_test&&$('resultInsight'))$('resultInsight').textContent+=` Suggested next test: ${r.analysis.recommended_next_test}`;
  $('resultPlaceholder').classList.add('hidden');$('resultContent').classList.remove('hidden');
  $('verificationBanner').classList.toggle('is-failed',!r.verified);
  ;
  $('verificationBanner').innerHTML=`<span>${r.verified?'✓':'!'}</span><div><strong>${r.verified?'Sequential and parallel outputs verified':'Result verification failed'}</strong><small>${r.verified?'Both engines processed the same prepared dataset and returned equivalent results within numeric tolerance.':'Review the run details before interpreting performance.'}</small></div>`;
  $('resultSpeedup').innerHTML=`${r.speedup_wall==null?'—':Number(r.speedup_wall).toFixed(2)}<small>x</small>`;
  $('resultEfficiency').innerHTML=`${Number(r.parallel_efficiency_percent||0).toFixed(1)}<small>%</small>`;
  $('resultSaved').innerHTML=`${Number(r.time_saved_percent||0).toFixed(1)}<small>%</small>`;
  $('resultVerified').textContent=r.verified?'PASS':'FAIL';
  $('resultJson').textContent=JSON.stringify(r.result,null,2);
  const seq=r.sequential,par=r.parallel;
  drawBarChart('resultTimeChart',[
    {label:'Sequential compute',value:seq.median_wall_seconds,color:cv('--base')},
    {label:'Parallel compute',value:par.median_wall_seconds,color:cv('--combine')},
    {label:'Sequential kernel',value:seq.median_kernel_seconds,color:cv('--max')},
    {label:'Parallel kernel',value:par.median_kernel_seconds,color:cv('--divide')}
  ]);
  drawBarChart('resultThroughputChart',[
    {label:'Sequential',value:seq.median_throughput,color:cv('--base')},
    {label:'Parallel',value:par.median_throughput,color:cv('--combine')}
  ]);
  const all=[...seq.runs,...par.runs];
  $('runTable').innerHTML=all.map(x=>`<tr><td>${x.mode}</td><td>${x.repetition}</td><td>${fmtSec(x.wall_seconds)}</td><td>${fmtSec(x.kernel_seconds)}</td><td>${fmtNum(x.throughput_records_per_second)} r/s</td><td>${x.observed_threads_max}</td></tr>`).join('');
  $('exportJsonBtn').disabled=false;$('exportCsvBtn').disabled=false;$('printReportBtn').disabled=false;
}
function drawBarChart(id,items){const canvas=$(id);if(!canvas)return;const box=canvas.parentElement.getBoundingClientRect();const w=Math.max(260,Math.floor(box.width||600)),h=Math.max(120,Math.floor(box.height||200)),dpr=Math.min(window.devicePixelRatio||1,2);canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);const c=canvas.getContext('2d');c.setTransform(dpr,0,0,dpr,0,0);c.clearRect(0,0,w,h);const max=Math.max(1,...items.map(x=>Number(x.value)||0));const left=135,top=18,bottom=20,barH=Math.min(24,(h-top-bottom-((items.length-1)*14))/items.length),gap=14;items.forEach((it,i)=>{const y=top+i*(barH+gap);c.fillStyle=cv('--fg2');c.font='12px system-ui';c.fillText(it.label,0,y+barH*.68);c.fillStyle=cv('--line');c.fillRect(left,y,w-left-95,barH);const bw=Math.max(1,(Number(it.value)||0)/max*(w-left-95));c.fillStyle=it.color;c.fillRect(left,y,bw,barH);c.fillStyle=cv('--fg');c.font='12px system-ui';c.fillText((Number(it.value)>=1000?fmtNum(it.value):Number(it.value).toFixed(3)),Math.min(w-90,left+bw+6),y+barH*.68);});}
$('exportJsonBtn').addEventListener('click',()=>{if(state.lastResult)location.href=`/api/reports/${state.lastResult.experiment_id}/json`;});$('exportCsvBtn').addEventListener('click',()=>{if(state.lastResult)location.href=`/api/reports/${state.lastResult.experiment_id}/csv`;});$('printReportBtn').addEventListener('click',()=>{if(!state.lastResult)return;window.print();});
async function restoreActiveTasks(){
  try{
    const g=await api('/api/datasets/generation/active');
    if(g.generation_id&&['queued','generating','cancelling'].includes(g.status)){
      state.generationId=g.generation_id;state.generationTerminal=false;state.generationCancelPending=g.status==='cancelling';
      $('generationProgress').classList.remove('hidden');$('generateBtn').disabled=true;
      $('cancelGeneration').classList.remove('hidden');$('cancelGeneration').disabled=state.generationCancelPending;
      $('cancelGeneration').textContent=state.generationCancelPending?'Cancelling…':'Cancel generation';
      updateGeneration(g.progress_state||{generated_rows:0,total_rows:g.config?.rows||0,progress:0});
      $('generationTitle').textContent=state.generationCancelPending?'Cancelling generation':'Generation in progress';
      navigate('generator');
      const proto=location.protocol==='https:'?'wss':'ws';
      const ws=new WebSocket(`${proto}://${location.host}/ws/generation/${g.generation_id}`);state.generationSocket=ws;
      ws.onmessage=ev=>{try{handleGenerationEvent(JSON.parse(ev.data));}catch(e){console.error(e);}};
      pollGeneration(g.generation_id);
      return true;
    }
  }catch(e){console.warn('Could not restore generation state',e);}
  try{
    const active=await api('/api/benchmarks/active');
    if(active?.job_id&&['queued','running'].includes(active.status)){
      connectBenchmarkJob(active);
      return true;
    }
  }catch(e){console.warn('Could not restore benchmark state',e);}
  return false;
}
function startPolling(){if(state.monitorRefreshTimer)clearInterval(state.monitorRefreshTimer);refreshMonitor();const benchmarkActive=!!(state.benchmarkId&&!state.benchmarkTerminal);state.monitorRefreshTimer=setInterval(refreshMonitor,benchmarkActive?3000:1500);} 
window.addEventListener('resize',()=>{if(state.live)renderMonitor(state.live);if(state.lastResult)renderResult();});
(async function init(){await loadHealth();await loadGenerationOptions();await loadTemplates();await loadDatasets();await loadHistory();await restoreActiveTasks();startPolling();})();
