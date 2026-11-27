'use strict';
const $ = id => document.getElementById(id);
const money = n => n == null ? '—' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(n);
const ageText = n => n == null ? 'No observation yet' : n < 60 ? `${Math.floor(n)}s old` : `${Math.floor(n / 60)}m ${Math.floor(n % 60)}s old`;
const genericError = value => (value||'').split('\n')[0].replace(/https?:\/\/[^\s'"<>]+/gi,'[endpoint]').replace(/CF Benchmarks|Polymarket US|Coinbase(?: Exchange)?|Kraken|BRTI|cpc-btc-updown-5m-[\w-]+/gi,'market feed');
const cents = n => n == null ? '—' : `${(n * 100).toFixed(1)}¢`;
let interval = 15, line = null, chart = null, series = null, initialized = false, latest = null, receivedAt = 0;
try {
  chart = LightweightCharts.createChart($('chart'), {
    autoSize:true, layout:{background:{color:'#101825'},textColor:'#8995a8',fontSize:10},
    grid:{vertLines:{color:'#1a2535'},horzLines:{color:'#1a2535'}},
    rightPriceScale:{borderColor:'#243043'},timeScale:{borderColor:'#243043',timeVisible:true,secondsVisible:true},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    localization:{timeFormatter:t=>new Date(t*1000).toISOString().slice(11,19)+' UTC'}
  });
  series = chart.addCandlestickSeries({upColor:'#4bd6a0',downColor:'#ef7887',borderVisible:false,wickUpColor:'#4bd6a0',wickDownColor:'#ef7887',priceLineColor:'#e8b45a'});
} catch (e) { $('chart-empty').textContent = 'Chart could not load. Market values remain available below.'; }

function textRow(parent, label, value, className='') {
  const el = document.createElement('div'); el.className=className;
  const a=document.createElement('span'), b=document.createElement('span');
  a.textContent=label; b.textContent=value; el.append(a,b); parent.append(el); return el;
}

function render(s) {
  latest=s; receivedAt=Date.now();
  $('reference-price').textContent=money(s.reference?.price);
  $('spot-price').textContent=money(s.spot?.price);
  $('reference-age').textContent=`${s.health.reference.status.toUpperCase()} · ${ageText(s.health.reference.age_seconds)} · source timestamp`;
  $('spot-age').textContent=`${s.health.spot.status.toUpperCase()} · ${ageText(s.health.spot.age_seconds)} · ${s.spot?.fallback?'backup feed':'spot price'}`;
  const m=s.market, t=m?.assetPriceTerms;
  $('market-dot').classList.toggle('online',s.market_active && s.health.market.status==='connected');
  $('market-window').textContent=t ? `${new Date(t.windowStart).toISOString().slice(11,16)}–${new Date(t.windowEnd).toISOString().slice(11,16)} UTC · 5-minute window` : 'No verified active contract';
  $('price-to-beat').textContent=money(t?.priceToBeat?.value);
  $('contract-status').textContent=s.market_active ? 'Verified contract · currently open' : 'No verified open contract · last observation shown';
  const book=s.book, bid=book?.bids[0]?.price, ask=book?.asks[0]?.price;
  $('up-price').textContent=cents(ask);
  $('down-price').textContent=cents(bid == null ? null : 1-bid);
  $('spread').textContent=bid == null || ask == null ? '—' : cents(ask-bid);
  $('book-status').textContent=`${s.execution_state||s.health.market.status.toUpperCase()} · ${ageText(s.health.market.age_seconds)}`;
  $('book-levels').replaceChildren();
  if (book) for(let i=0;i<3;i++) {
    const a=book.bids[i],b=book.asks[i];
    const row=textRow($('book-levels'),a?`${cents(a.price)} · ${a.quantity.toFixed(2)}`:'—',b?`${cents(b.price)} · ${b.quantity.toFixed(2)}`:'—');
    row.children[0].className='book-bid';row.children[1].className='book-ask';
  }
  if(series){
    if(line)series.removePriceLine(line);
    line=t?.priceToBeat ? series.createPriceLine({price:Number(t.priceToBeat.value),color:'#8faad9',lineWidth:1,lineStyle:2,axisLabelVisible:true,title:'Open ref'}) : null;
  }
  $('connections').replaceChildren();
  for(const [key,label] of [['reference','Settlement reference'],['market','Order book'],['spot','BTC/USD spot']]){
    const h=s.health[key],row=textRow($('connections'),label,h.status.toUpperCase(),'connection');
    const small=document.createElement('small');small.textContent=ageText(h.age_seconds);row.children[0].append(small);
    row.children[1].className=`badge ${h.status}`;if(h.error)row.title=genericError(h.error);
  }
  if($('research-results-dialog').open) renderCollectorEvents(s.events);
  const names = ['reference','market','spot'];
  const status = ['disconnected','refreshing','delayed','connected'].find(
    value=>['reference','market'].some(name=>s.health[name].status===value));
  setFeedIndicator(s.collecting ? status : 'paused');
  $('feed-diagnostics').replaceChildren();
  for(const name of names){
    const h=s.health[name];
    textRow($('feed-diagnostics'),name,
      `${h.status} · ${ageText(h.age_seconds)} · last success ${h.last_success ? new Date(h.last_success*1000).toISOString() : '—'}${h.error ? ' · '+genericError(h.error) : ''}`,
      'connection');
  }
  $('notice').classList.toggle('warning',s.emergency);
  $('notice').textContent=s.emergency?'Emergency stop active. Review the request in Settings.':
    s.mode==='OFF'?'Paper paused':
    s.mode==='LEARN'?'Learning only':!s.collecting?'Collection paused':
    s.execution_state==='EXECUTION BLOCKED'?'Paper locked · see Settings':
    status==='connected'?'Paper running':'Paper running · waiting for fresh data';
  const toggle=$('toggle-paper');
  toggle.textContent=s.mode==='SIMULATE'?'Pause paper':'Resume paper';
  toggle.disabled=window.prophetRole==='viewer' || Boolean(s.emergency);
  document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===s.mode));
  const primary=s.primary_signal;
  const call=primary?.side ? {decision:primary.side} : null;
  renderDirection($('ai-decision'),$('ai-tally'),{call});
  renderBenchmark($('ai-results'),s.benchmark_results);
  $('direction-quotes').hidden=!call;
  $('quote-pair').hidden=!call;
  $('model-probabilities').hidden=!call || primary.up==null;
  $('ai-reason').textContent=call
    ? `${money(primary.stake)} paper · ${primary.model || 'Unknown model'}`
    : s.mode==='OFF'?'Paper paused':(window.prophetSkipNote||'Waiting for a qualifying entry');
  $('model-probabilities').textContent=call && primary.up!=null
    ? `${primary.kind==='ENTRY'?'At entry · ':''}${(primary.up*100).toFixed(1)}% UP / ${((1-primary.up)*100).toFixed(1)}% DOWN`
    : 'Unavailable';
  tick();
}

function setFeedIndicator(status){
  const indicator=$('feed-indicator');
  if(indicator.dataset.status!==status){
    indicator.dataset.status=status;
    indicator.textContent=status.charAt(0).toUpperCase()+status.slice(1);
  }
}

function tick(){
  const now=latest ? latest.server_time+(Date.now()-receivedAt)/1000 : Date.now()/1000;
  $('clock').textContent=new Date(now*1000).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const end=latest?.market?.assetPriceTerms?.windowEnd;
  const n=end?Math.max(0,Math.ceil(Date.parse(end)/1000-now)):null;
  const next=Date.parse(latest?.next_market_start||'')/1000;
  $('next-market').textContent=Number.isFinite(next)&&next>now?`Next market in ${Math.ceil(next-now)}s`:'Next market time unavailable';
  const evaluation=latest?.next_evaluation;
  $('next-evaluation').textContent=evaluation&&latest.mode!=='OFF'?`Next evaluation in ${Math.max(0,Math.ceil(evaluation-now))}s`:'Evaluation paused';
  $('countdown').textContent=n==null?'—:—':`${String(Math.floor(n/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;
  if(receivedAt && Date.now()-receivedAt>10000)setFeedIndicator('disconnected');
  if((receivedAt && Date.now()-receivedAt>10000) || n===0){
    renderDirection($('ai-decision'),$('ai-tally'),null);
    $('direction-quotes').hidden=true;
    $('quote-pair').hidden=true;
    $('model-probabilities').hidden=true;
  }
}
async function getJSON(url){const r=await fetch(url);if(r.status===401){location.href='/login';throw new Error('Sign in required');}if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();}
async function refresh(){
  try{render(await getJSON('/api/state'));}catch(e){setFeedIndicator('disconnected');}
  setTimeout(refresh,2000);
}
async function candles(){
  const requested=interval;
  try{const data=await getJSON(`/api/candles?interval=${requested}`);if(requested!==interval)return;
    if(series){series.setData(data.candles);$('chart-empty').hidden=!!data.candles.length;if(!initialized&&data.candles.length){chart.timeScale().fitContent();initialized=true;}}
    const partial=data.candles.filter(c=>!c.complete).length;
    $('candle-info').textContent=`${data.candles.length} observed candles · ${partial} partial / gapped · UTC`;
  }catch(e){$('candle-info').textContent='Chart refresh failed · last observations retained';}
}
document.querySelectorAll('[data-interval]').forEach(b=>b.addEventListener('click',()=>{interval=Number(b.dataset.interval);initialized=false;document.querySelectorAll('[data-interval]').forEach(x=>x.classList.toggle('selected',x===b));candles();}));
let controlBusy = false;
async function control(action){
  if(controlBusy)return;
  controlBusy=true;
  try{const r=await fetch(`/api/control/${action}`,{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});if(!r.ok)throw new Error((await r.json()).detail||`HTTP ${r.status}`);render(await getJSON('/api/state'));}
  catch(e){$('notice').classList.add('warning');$('notice').textContent=`Control failed: ${e.message}. Check the local service.`;}
  finally{controlBusy=false;}
}
$('toggle-paper').onclick=()=>control(latest?.mode==='SIMULATE'?'stop':'start');
$('emergency').onclick=()=>control('emergency');$('reset').onclick=()=>control('review-reset');
$('formal-start').onclick=async()=>{
  try{
    const r=await fetch('/api/control/formal-start',{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});
    const result=await r.json();
    if(!r.ok) throw new Error(result.detail||`HTTP ${r.status}`);
    $('formal-start-result').textContent='Formal nine-model comparison is running.';
    render(await getJSON('/api/state'));
  }catch(e){$('formal-start-result').textContent=e.message;}
};

$('reset-scoreboard').onclick=async()=>{
  if(!confirm('Reset the Games / Wins / Losses / Net P&L counter to zero?\n\nTrades, cash balance and Excel history are kept.'))return;
  try{
    const r=await fetch('/api/unified/reset-scoreboard',{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});
    if(!r.ok)throw new Error((await r.json()).detail||`HTTP ${r.status}`);
    scoreboard();
  }catch(e){$('notice').classList.add('warning');$('notice').textContent=`Reset failed: ${e.message}.`;}
};
$('collector-pause').onclick=()=>control('collector-pause');
$('collector-resume').onclick=()=>control('collector-resume');

async function runDiagnostic(action){
  try {
    const response=await fetch('/api/diagnostics/'+action,{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});
    const d=await response.json();
    if(!response.ok)throw Error(d.detail);
    $('diagnostic-result').textContent=d.message||`Database ${d.integrity} · disk free ${(d.free_disk_bytes/2**30).toFixed(1)} GB · ${d.incidents.filter(i=>i.resolved==null).length} open incidents`;
  }catch(error){$('diagnostic-result').textContent=error.message;}
}
$('diagnose').onclick=()=>runDiagnostic('diagnose');
$('repair-feed').onclick=()=>runDiagnostic('reconnect');
$('alternate-repair').onclick=()=>runDiagnostic('alternative');
$('settings').onclick=()=>{
  $('settings-dialog').showModal();
  loadInterventions();
  settingsStatus();
};
async function loadInterventions(){
  const panel=$('interventions');
  try{
    const items=await getJSON('/api/interventions');
    panel.replaceChildren();
    if(!items.length){panel.textContent='No requests.';return;}
    for(const item of items){
      const row=document.createElement('section'), text=document.createElement('p');
      text.textContent=`${item.status} · ${item.request}`;
      row.append(text);
      if(item.status==='PENDING'){
        const note=document.createElement('input');
        note.placeholder='Optional response';
        note.setAttribute('aria-label','Optional response to review request');
        note.maxLength=500;
        row.append(note);
        for(const action of ['approve','deny']){
          const button=document.createElement('button');
          button.type='button';
          button.className='button';
          button.textContent=action==='approve'?'Approve':'Deny';
          button.disabled=window.prophetRole==='viewer';
          button.onclick=async()=>{
            try{
              const result=await fetch(`/api/interventions/${item.id}/${action}`,{
                method:'POST',headers:{'Content-Type':'application/json','X-Prophet-Control':'local-dashboard'},
                body:JSON.stringify({note:note.value})});
              const data=await result.json();
              if(!result.ok)throw new Error(data.detail);
              $('intervention-feedback').textContent=data.message;
              await loadInterventions();
            }catch(error){$('intervention-feedback').textContent=error.message;}
          };
          row.append(button);
        }
      }
      panel.append(row);
    }
  }catch(error){panel.textContent='Review requests unavailable.';}
}
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=async()=>{
  try{const r=await fetch(`/api/mode/${b.dataset.mode}`,{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});if(!r.ok)throw new Error((await r.json()).detail);render(await getJSON('/api/state'));}
  catch(e){alert(e.message);}
});
function renderCollectorEvents(events){
  $('events').replaceChildren();
  for(const e of events){
    const el=document.createElement('div');
    el.className='event';
    el.textContent=`${new Date(e.time*1000).toISOString().slice(11,19)} UTC · ${e.level} · ${genericError(e.message)}`;
    $('events').append(el);
  }
}

async function scoreboard(){
  try {
    const game=await getJSON('/api/unified');
    if(!game)return;
    $('games-played').textContent=game.played;
    $('games-won').textContent=game.wins;
    $('games-lost').textContent=game.losses;
    $('games-pnl').textContent=money(game.net);
    $('game-balance').textContent=money(game.balance)+' cash';
    vegas(game);
    const sel=game.selective50;
    if(sel){$('sel50-balance').textContent=money(sel.balance);$('sel50-trades').textContent=sel.trades;
      $('sel50-wins').textContent=sel.wins;$('sel50-losses').textContent=sel.losses;$('sel50-net').textContent=money(sel.net);
      $('sel50-note').textContent=sel.balance<sel.stake?'Stopped: less than $3 left. It never adds money or goes negative.':
        sel.trades?`Win rate ${Math.round(100*sel.wins/sel.trades)}% · ${sel.early_exits} sold early${sel.open?' · 1 trade open':''}`:
        (sel.open?'1 trade open.':'Paper only. Waiting for a round where all three signals agree.');}
    $('games-since').textContent=game.reset
      ? `Counting since ${new Date(game.counting_since*1000).toISOString().slice(0,16).replace('T',' ')} UTC · all-time ${game.all_time.played} games, ${game.all_time.wins}–${game.all_time.losses}, ${money(game.all_time.net)}`
      : 'Counting since the account started';
    const streak=game.streaks;
    $('games-streak').textContent=game.played
      ? `${(100*game.wins/game.played).toFixed(1)}% wins · Current ${streak.current||'flat'} ${streak.length} · Best win ${streak.longest.WIN} · Worst loss ${streak.longest.LOSS}`
      : game.position?'Paper position open.':'Waiting for the first completed paper trade.';
    const markers=game.recent.flatMap(trade=>{
      const entries=[{time:Math.floor(trade.time/interval)*interval,position:trade.side==='UP'?'belowBar':'aboveBar',
                      shape:trade.side==='UP'?'arrowUp':'arrowDown',color:'#e8b45a',text:'Paper '+trade.side}];
      if(trade.status==='SOLD')entries.push({time:Math.floor(trade.closed/interval)*interval,position:'inBar',shape:'circle',color:'#8faad9',text:'Paper exit'});
      return entries;
    });
    if(series)series.setMarkers(markers.sort((a,b)=>a.time-b.time));
  }catch(e){$('games-streak').textContent='Paper scoreboard unavailable.';}
}

async function paper(){
  if(!$('research-results-dialog').open)return;
  try{
    const s=await getJSON('/api/paper');$('accounts').replaceChildren();
    for(const a of s.accounts.filter(a=>a.id.startsWith('adaptive-'))){
      const el=document.createElement('div'),title=document.createElement('span'),balance=document.createElement('strong'),detail=document.createElement('p');
      title.textContent=`ADAPTIVE · $${a.budget} fixed stake`;balance.textContent=money(a.balance);detail.textContent=`Realized ${money(a.realized)} · unrealized ${money(a.unrealized_pnl)} · equity ${money(a.market_equity)} · losses ${a.streak}`;
      el.append(title,balance,detail);if(a.halted){const h=document.createElement('small');h.textContent=a.halted;h.className='loss';el.append(h);}$('accounts').append(el);
    }
    if($('research-results-dialog').open){
      $('paper-summary').textContent=s.experiment?`Seven-day experiment: ${s.experiment.status} · ${new Date(s.experiment.start*1000).toISOString()} to ${new Date(s.experiment.end*1000).toISOString()} · reference model: ${s.experiment.model}`:'Experiment not started. SIMULATE starts the seven-day clock. Entries wait for verified data.';
      $('orders').replaceChildren();
      for(const o of s.orders.filter(o=>o.account.startsWith('adaptive-'))){const tr=document.createElement('tr');for(const v of [new Date(o.time*1000).toISOString().slice(11,19),o.account,o.flipped_side?`${o.side} → ${o.flipped_side}`:o.side,o.quantity.toFixed(2),money(o.cost),o.status,o.pnl==null?'Pending':money(o.pnl)]){const td=document.createElement('td');td.textContent=v;tr.append(td);}$('orders').append(tr);}
      if(!$('orders').children.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.textContent='No paper trades recorded. SKIP is a valid outcome.';tr.append(td);$('orders').append(tr);}
      $('risk-events').replaceChildren();for(const e of s.risk_events){const d=document.createElement('div');d.className='event';d.textContent=`${new Date(e.time*1000).toISOString()} · ${e.account||'All accounts'} · ${e.reason}${e.reviewed?' · reviewed':''}`;$('risk-events').append(d);}
      for(const e of s.skips||[]){const d=document.createElement('div');d.className='event';d.textContent=`${e.account} · ${e.reason} · ${e.slug}`;$('risk-events').append(d);}
    }
  }catch(e){$('paper-summary').textContent='Paper account status unavailable.';}
}
async function research(){
  if(!$('research-results-dialog').open)return;
  try{
    const s=await getJSON('/api/research');$('research-panel').hidden=!s.available;if(!s.available)return;
    $('research-counts').replaceChildren();
    for(const [label,value] of [['15-second reference candles',s.reference_candles],['Resolved US BTC contracts',s.resolved_contracts],['Contracts with quotes',s.contracts_with_quotes]]){
      const el=document.createElement('div'),a=document.createElement('span'),b=document.createElement('strong');a.textContent=label;b.textContent=value.toLocaleString();el.append(a,b);$('research-counts').append(el);
    }
    const report=s.backtest?null:s.challenger||s.baseline;
    let summary=s.coverage?`${s.coverage.start.slice(0,10)} to ${s.coverage.end_exclusive.slice(0,10)} UTC (end excluded). `:'Import in progress. ';
    summary+=`${s.quarantined_quotes} inconsistent quote points quarantined. `;
    if(s.backtest)summary+='Historical execution audit: earlier display-price P&L is withdrawn when timestamps conflict with causal books. ';
    if(report){
      summary+='Chronological split: 4 training days / 1 validation day / 2 test days. ';
      for(const [name,parts] of Object.entries(report.models)){
        if(name==='momentum_baseline_v0.1'||name==='offline-logistic-v0.1')summary+=`${name}: ${(parts.test.directional_accuracy*100).toFixed(1)}% retrospective test direction accuracy (${parts.test.examples} rounds), Brier ${parts.test.brier_score.toFixed(3)}. `;
      }
    }
    summary+='No historical order-book quantities in this dataset; these are not tradable returns.';
    $('research-summary').textContent=summary;
    if(s.backtest){
      if(s.backtest.status?.startsWith('WITHDRAWN')){$('backtest-summary').textContent='WITHDRAWN: historical display prices conflict with entry-time order books. Previous P&L figures are not usable. A causal depth replay is required.';$('backtest-rows').replaceChildren();return;}
      $('backtest-summary').textContent=`Model: ${s.backtest.selected_model}. Test begins ${s.backtest.test_start}. Fill quantities are assumptions. Strict-entry revisions are exploratory; take-profit tests are disabled.`;
      $('backtest-rows').replaceChildren();for(const scenario of s.backtest.scenarios){const tr=document.createElement('tr');const a=scenario.accounts.filter(a=>a.id==='primary'||a.id.startsWith('research-'));for(const v of [scenario.label||`Assumed capacity ${scenario.capacity_fraction*100}%`,...a.map(a=>money(a.realized)),money(scenario.combined_main_pnl)]){const td=document.createElement('td');td.textContent=v;tr.append(td);}$('backtest-rows').append(tr);}
    }
  }catch(e){$('research-summary').textContent='Historical research status unavailable.';}
}
async function settingsStatus(){
  if(!$('settings-dialog').open)return;
  try{
    const alerts=await getJSON('/api/alerts');$('sms-status').textContent=`${alerts.configured?'Configured':'NOT CONFIGURED'} · ${alerts.recipient||'no destination'} · ${alerts.message}`;$('sms-test').disabled=!alerts.configured||window.prophetRole==='viewer';
    const [learning,preflight]=await Promise.all([getJSON('/api/learning'),getJSON('/api/competition/preflight')]);
    const hours=(preflight.learning_seconds/3600).toFixed(2);
    $('competition-preflight').textContent=preflight.ready?`Ready · ${hours} active learning hours · ${preflight.observed.length}/9 forecasts`:`${hours}/48 active learning hours · ${preflight.observed.length}/9 forecasts · ${preflight.reasons.join(' · ')}`;
    $('formal-start').disabled=window.prophetRole==='viewer';
    $('learning-status').textContent=learning.status?`${learning.status.status} · ${learning.status.version||learning.status.message||''} · active model unchanged`:'Waiting for a challenger training result.';
    const chosen=$('shadow-version').value;$('shadow-version').replaceChildren();
    for(const v of learning.versions){const o=document.createElement('option');o.value=v.version;o.textContent=`${v.version} · Brier ${v.brier.toFixed(3)}`;$('shadow-version').append(o);}
    if(chosen&&learning.versions.some(v=>v.version===chosen))$('shadow-version').value=chosen;else if(learning.shadow_version)$('shadow-version').value=learning.shadow_version;
    $('rollback').disabled=!learning.versions.length||window.prophetRole==='viewer';
  }catch(e){$('learning-status').textContent='Settings status unavailable.';}
}
$('sms-test').onclick=async()=>{const r=await fetch('/api/alerts/test',{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});alert((await r.json()).message||'SMS test unavailable');};
$('rollback').onclick=async()=>{const r=await fetch(`/api/shadow-rollback/${encodeURIComponent($('shadow-version').value)}`,{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});if(!r.ok)alert((await r.json()).detail);research();};
setInterval(settingsStatus,10000);setInterval(tick,1000);setInterval(candles,3000);setInterval(paper,5000);setInterval(scoreboard,3000);refresh();candles();scoreboard();


let testedUpdate;
$('update-history').onclick=async()=>{
  try{
    const data=await getJSON('/api/updates');
    $('update-result').textContent=`Installed ${data.current} · ${data.history.length} recent update events. Select a release ZIP to test.`;
  }catch(error){$('update-result').textContent='Update history unavailable.';}
};
for(const id of ['update-file','update-checksum']){
  $(id).onchange=()=>{testedUpdate=null;$('stage-update').disabled=true;$('activate-update').disabled=true;};
}
$('test-update').onclick=async()=>{
  try{
    const f=$('update-file').files[0];
    if(!f)throw Error('Choose a release ZIP first.');
    const response=await fetch('/api/updates/test',{method:'POST',
      headers:{'X-Prophet-Control':'local-dashboard','X-Prophet-Checksum':$('update-checksum').value.trim()},body:f});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail);
    testedUpdate=data.sha256;
    $('stage-update').disabled=false;
    $('update-result').textContent=`Verified ${data.version} · ${data.files} files checked.`;
  }catch(error){$('update-result').textContent=error.message;}
};
$('stage-update').onclick=async()=>{
  try{
    if(!testedUpdate)return;
    const response=await fetch('/api/updates/stage/'+testedUpdate,{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail);
    $('stage-update').disabled=true;
    $('activate-update').disabled=false;
    $('update-result').textContent=`Backup saved as ${data.backup}. New files staged in updates/${data.directory}. Restart instructions are in the README.`;
  }catch(error){$('update-result').textContent=error.message;}
};

async function selectUpdate(digest){
  try{
    const response=await fetch('/api/updates/activate/'+digest,{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail);
    $('update-result').textContent=data.selection?
      'Approved version selected. Quit the app and reopen your original launcher.':
      'Installed version selected for the next restart. The current ledger is preserved.';
  }catch(error){$('update-result').textContent=error.message;}
}
$('activate-update').onclick=()=>selectUpdate(testedUpdate);
$('rollback-update').onclick=()=>selectUpdate('rollback');

$('request-training').onclick=async()=>{
  try{
    const r=await fetch('/api/learning/request',{method:'POST',headers:{'X-Prophet-Control':'local-dashboard'}});
    const result=await r.json();
    $('training-result').textContent=result.message||result.detail;
  }catch(error){$('training-result').textContent='Unable to request training.';}
};

// Vegas panel: win streak, roulette wheel and win song. Display only; never sends controls.
let wheelAngle=0, lastSettled=undefined, overlayTimer=null, songAvailable=false;
const pref=(key,fallback)=>{try{return localStorage.getItem(key)??fallback;}catch(e){return fallback;}};
const setPref=(key,value)=>{try{localStorage.setItem(key,value);}catch(e){}};
$('win-video-on').checked=pref('prophet-win-video','on')!=='off';
$('win-video-on').onchange=e=>setPref('prophet-win-video',e.target.checked?'on':'off');
$('win-length').value=pref('prophet-win-length','30');
$('win-length').onchange=e=>setPref('prophet-win-length',e.target.value);
async function songStatus(){
  try{const s=await getJSON('/api/win-sound/status');songAvailable=s.available;
    $('win-sound-state').textContent=!s.available?'No song file yet':s.built_in?'Jackpot sound ready':'Your song ready';}catch(e){songAvailable=false;}
}
songStatus();setInterval(songStatus,60000);
function closeWin(){
  clearTimeout(overlayTimer);
  const audio=$('win-audio');audio.pause();audio.removeAttribute('src');audio.load();
  $('win-overlay').hidden=true;
}
$('win-audio').addEventListener('ended',closeWin);       // played once: close the screen
function showWin(text){
  if(!$('win-video-on').checked)return;
  $('win-text').textContent=text;$('win-note').textContent='';
  $('win-overlay').hidden=false;
  clearTimeout(overlayTimer);
  const seconds=Number($('win-length').value);
  if(!songAvailable){
    $('win-note').textContent='Add your song as win-sound.mp3 in the data folder to hear it here.';
    overlayTimer=setTimeout(closeWin,6000);return;
  }
  const audio=$('win-audio');
  audio.src='/api/win-sound?v='+Date.now();audio.currentTime=0;   // always the current file
  audio.play().then(()=>{
    overlayTimer=setTimeout(closeWin,seconds>0?seconds*1000:15*60*1000);
  }).catch(()=>{
    $('win-note').textContent='Safari blocked the sound. Click Test once after opening the dashboard to allow it.';
    overlayTimer=setTimeout(closeWin,8000);
  });
}
$('win-close').onclick=closeWin;
$('win-overlay').onclick=e=>{if(e.target===$('win-overlay'))closeWin();};
$('win-video-test').onclick=()=>{const was=$('win-video-on').checked;$('win-video-on').checked=true;showWin('Test celebration');$('win-video-on').checked=was;};
const SKIP_WORDS={PRICE_OUTSIDE_BAND:'price outside 40–70¢',EDGE_TOO_SMALL:'expected profit too small',
  NO_EXECUTABLE_DEPTH:'not enough to buy',NO_LIQUIDITY:'no buyers or sellers',PRIOR_POSITION_OPEN:'a trade was still open',
  INSUFFICIENT_CASH:'not enough cash',WAITING:'no qualifying moment',STALE_BOOK:'stale prices',STALE_REFERENCE:'stale BTC price',
  PROVIDER_ERROR_MARKET:'market feed error',PROVIDER_ERROR_REFERENCE:'BTC feed error',UNDERSIZED_FILL:'not enough to buy'};
function skipWords(reason){
  const codes=[...new Set(String(reason||'WAITING').replace(/^SKIP:\s*/,'').split('/').map(x=>x.split(':').pop().trim()))];
  return codes.map(c=>SKIP_WORDS[c]||c.toLowerCase().replaceAll('_',' ')).join(' · ');
}
function currentAngle(){
  const m=getComputedStyle($('wheel')).transform;
  if(!m||m==='none')return wheelAngle;
  const v=m.match(/matrix\(([^)]+)\)/);if(!v)return wheelAngle;
  const [a,b]=v[1].split(',').map(Number);return (Math.atan2(b,a)*180/Math.PI+360)%360;
}
let wheelMode=null;
function setWheel(mode){                       // mode: 'spin' | 'UP' | 'DOWN' | 'SKIP'
  if(mode===wheelMode)return;
  wheelMode=mode;
  const wheel=$('wheel');
  if(mode==='spin'){wheel.style.setProperty('--from',`${wheelAngle%360}deg`);wheel.classList.add('spinning');return;}
  const from=wheel.classList.contains('spinning')?currentAngle():wheelAngle;
  wheel.classList.remove('spinning');wheel.style.transition='none';wheel.style.transform=`rotate(${from}deg)`;
  void wheel.offsetWidth;                        // continue from where the spin was
  wheel.style.transition='';
  // 12 slices of 30°: green (UP) centres 15+60k°, red (DOWN) centres 45+60k°; pointer at the top.
  // SKIP has no slice: the wheel just coasts to a stop on a slice boundary (neither colour).
  if(mode==='SKIP'){wheelAngle=from+360+((360-((from+Math.floor(Math.random()*12)*30)%360))%360);}
  else{const slice=Math.floor(Math.random()*6)*60+(mode==='UP'?15:45);
       wheelAngle=from+2*360+((360-((from+slice)%360))%360);}
  wheel.style.transform=`rotate(${wheelAngle}deg)`;
}
function renderRound(game){
  const c=game.current, st=$('wheel-state');
  if(!c){setWheel('SKIP');st.textContent='—';st.className='wheel-state';window.prophetPending=null;return;}
  const over=c.now>=c.end;
  const last=(game.history||[])[0];
  // A skipped round is only "final" at its close and the next round starts seconds later:
  // keep showing SKIP for the first 8 seconds of the next round so it is visible.
  const justSkipped=!c.side&&!over&&last&&!last.side&&last.slug!==c.slug&&(c.now-last.end)<8;
  if(justSkipped){setWheel('SKIP');st.textContent='SKIPPED';st.className='wheel-state skip';window.prophetPending='SKIP';}
  else if(c.side){setWheel(c.side);st.textContent=`BOUGHT ${c.side}`;st.className='wheel-state '+c.side.toLowerCase();window.prophetPending=null;}
  else if(over){setWheel('SKIP');st.textContent='SKIPPED';st.className='wheel-state skip';window.prophetPending='SKIP';}
  else{setWheel('spin');st.textContent='MODEL DECIDING…';st.className='wheel-state';window.prophetPending='Deciding…';}
  window.prophetSkipNote=(!c.side&&last&&!last.side)?`Last round skipped · ${skipWords(last.reason)}`:null;
  if(!c.side&&over)window.prophetSkipNote=`Skipped this round · ${skipWords(c.reason)}`;
  // Update the pick card now instead of waiting for the next state refresh (unless it shows a live UP/DOWN).
  const card=$('ai-decision');
  if(card.dataset.direction!=='UP'&&card.dataset.direction!=='DOWN'){
    card.textContent=window.prophetPending||'Waiting';
    card.dataset.direction=window.prophetPending==='SKIP'?'skip':'waiting';
    if(typeof latest!=='undefined'&&latest&&latest.mode!=='OFF')$('ai-reason').textContent=window.prophetSkipNote||'Waiting for a qualifying entry';
  }
}
function renderHistory(history){
  const box=$('vegas-history');box.replaceChildren();
  for(const h of history||[]){
    const cell=document.createElement('div');
    const outcome=h.outcome?h.outcome.toLowerCase():'unknown';
    const won=h.pnl!=null&&h.pnl>0;
    cell.className=`cell ${outcome}${h.side?(won?' win':''):' skip'}`;
    const top=document.createElement('span');top.textContent=h.outcome?h.outcome[0]:'?';
    const bottom=document.createElement('span');
    bottom.textContent=h.side?(h.side==='UP'?'▲':'▼')+(h.pnl==null?'…':won?'✓':'✗'):'S';
    cell.append(top,document.createElement('br'),bottom);
    const t=new Date(h.start*1000).toISOString().slice(11,16);
    cell.title=`${t} UTC · market ${h.outcome||'not settled'} · `+(h.side?`bought ${h.side}${h.pnl!=null?` · ${money(h.pnl)}`:''}`:`skipped (${skipWords(h.reason)})`);
    box.append(cell);
  }
}
function vegas(game){
  renderRound(game);renderHistory(game.history);
  const s=game.streaks||{current:null,length:0,longest:{WIN:0,LOSS:0}};
  $('vegas-streak').textContent=s.current==='WIN'?s.length:0;
  $('vegas-best').textContent=`Best ${s.longest.WIN} · Worst losing run ${s.longest.LOSS}`;
  $('vegas-won').textContent=game.wins;$('vegas-lost').textContent=game.losses;$('vegas-games').textContent=game.played;
  const last=game.last_settled;
  const key=last?last.slug:null;
  if(lastSettled===undefined){lastSettled=key;if(last)showResult(last,false);return;}
  if(!last||key===lastSettled)return;
  lastSettled=key;
  showResult(last,true);
}
function showResult(last,celebrate){
  const won=last.pnl>0;
  const el=$('wheel-result');
  el.textContent=`${won?'WIN':'LOSS'} · bought ${last.side}, market went ${last.outcome||'?'} · ${money(last.pnl)}`;
  el.className='wheel-result '+(won?'win':'loss');
  if(won&&celebrate)showWin(`${money(last.pnl)} · win streak ${$('vegas-streak').textContent}`);
}
