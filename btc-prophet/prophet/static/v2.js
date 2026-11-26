'use strict';
const pct=n=>n==null?'UNAVAILABLE':`${(n*100).toFixed(1)}%`;
const utcTime=t=>t==null?'NOT AVAILABLE':new Date(t*1000).toISOString();
const adminHeaders={'Content-Type':'application/json','X-Prophet-Control':'local-dashboard'};
function streakText(s){return `Current ${s.current||'none'} × ${s.length} · longest ${Object.entries(s.longest).map(([k,v])=>`${k} ${v}`).join(' / ')}`;}
function tileBoard(id,values){const node=$(id);node.replaceChildren();for(const v of values){const tile=document.createElement('div');tile.className=`history-tile ${v.value}`;tile.textContent=v.value;tile.title=v.slug;node.append(tile);}}
async function board(){if(!$('research-results-dialog').open)return;try{const h=await getJSON('/api/history');tileBoard('market-tiles',h.markets.map(m=>({value:m.outcome,slug:m.slug})));$('market-history-summary').textContent=`${h.count}/50 verified settled rounds · UP ${h.up_percent==null?'UNAVAILABLE':h.up_percent.toFixed(1)+'%'} / DOWN ${h.down_percent==null?'UNAVAILABLE':h.down_percent.toFixed(1)+'%'}${h.complete?'':' · INSUFFICIENT DATA'}`;$('directional-streaks').textContent=streakText(h.directional_streaks);tileBoard('trade-tiles',h.trade_tiles);$('trade-history-summary').textContent=`${h.total_trades} settled trades · ${pct(h.win_rate)} win rate · SKIP never resets settled-trade streaks`;$('trade-streaks').textContent=streakText(h.trade_streaks);}catch(e){$('market-history-summary').textContent='History unavailable';}}
$('analyze-trends').onclick=async()=>{try{const r=await fetch('/api/analyze-trends',{method:'POST',headers:adminHeaders});const a=await r.json();if(!r.ok)throw new Error(a.detail);$('trend-result').textContent=`${a.status}: ${a.explanation} Prediction accuracy ${pct(a.accuracy)} (${a.prediction_count} recorded predictions); Brier ${a.brier==null?'INSUFFICIENT DATA':a.brier.toFixed(4)}. Recent log momentum ${a.recent_momentum==null?'UNAVAILABLE':a.recent_momentum.toFixed(5)}; volatility ${a.volatility==null?'UNAVAILABLE':a.volatility.toFixed(5)}. Calibration: ${a.calibration_status}.`;}catch(e){$('trend-result').textContent=e.message;}};
async function strategy(){if(!$('research-results-dialog').open)return;try{const s=await getJSON('/api/strategies'),a=s.active;$('active-model').textContent=`${a.model} · strategy ${a.id}`;$('strategy-detail').textContent=`Adaptive entry · hold to settlement · $1 / $2 / $3 accounts · activated ${utcTime(a.time)} · ${Math.max(0,Math.floor((Date.now()/1000-a.time)/60))}m active`;$('strategy-reason').textContent=`Previous: ${a.previous||'NOT AVAILABLE'} · ${a.reason} · Git ${a.git_commit||'NOT AVAILABLE'} · $1 account in this segment ${s.performance?money(s.performance.net_profit):'UNAVAILABLE'} / ${s.performance?.trades||0} settled trades`;const priorSelection=$('primary-history-select').value;$('primary-history-select').replaceChildren();for(const prior of s.history.slice(1)){const option=document.createElement('option');option.value=prior.id;option.textContent=`Strategy ${prior.id}: ${prior.model} · ${utcTime(prior.time)}`;$('primary-history-select').append(option);}if([...$('primary-history-select').options].some(o=>o.value===priorSelection))$('primary-history-select').value=priorSelection;$('strategy-history').replaceChildren();for(const r of s.history){const p=document.createElement('p');p.className='event';p.textContent=`${utcTime(r.time)} · ${r.action} · ${r.previous||'none'} → ${r.model} · ${r.reason}`;$('strategy-history').append(p);}}catch(e){$('strategy-detail').textContent='Strategy status unavailable';}}
$('strategy-history-button').onclick=()=>{$('strategy-history').hidden=!$('strategy-history').hidden;};
async function compare(){try{const r=await getJSON('/api/models');$('lab-warning').textContent=r.warning;$('model-catalog').replaceChildren();for(const m of r.models){const el=document.createElement('div');const title=document.createElement('strong'),detail=document.createElement('p');title.textContent=m.id;const latestModel=m.versions[0],metadata=latestModel?JSON.parse(latestModel.metadata):{},test=metadata.test;detail.textContent=`${m.status} · ${m.versions.length} saved versions${test?` · historical accuracy ${pct(test.accuracy)} · Brier ${test.brier?.toFixed(4)} (${test.n} examples), not profitability`:''}`;el.append(title,detail);$('model-catalog').append(el);}$('leaderboard').replaceChildren();const chosen=$('promotion-config').value;$('promotion-config').replaceChildren();for(const c of r.configurations){const tr=document.createElement('tr');for(const value of [c.version,`${c.checkpoint}s`,c.exit,`$${c.stake}`,c.net_profit==null?'UNKNOWN':money(c.net_profit),pct(c.win_rate),c.maximum_drawdown==null?'UNKNOWN':money(c.maximum_drawdown),`${c.trades} / ${c.skips}`,`${c.status}; ${c.data_completeness}`]){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('leaderboard').append(tr);if(c.status==='FORWARD SHADOW'&&c.exit==='HOLD'){const o=document.createElement('option');o.value=c.id;o.textContent=c.id;$('promotion-config').append(o);}}if([...$('promotion-config').options].some(o=>o.value===chosen))$('promotion-config').value=chosen;$('leaderboard-panel').open=true;}catch(e){$('lab-warning').textContent=`Comparison unavailable: ${e.message}`;}}
$('compare-models').onclick=compare;
async function promotePrimary(rollback){try{const r=await fetch('/api/promote',{method:'POST',headers:adminHeaders,body:JSON.stringify({configuration:$('promotion-config').value,reason:$('promotion-reason').value,strategy_id:Number($('primary-history-select').value),rollback})});const v=await r.json();if(!r.ok)throw new Error(v.detail);$('promotion-status').textContent='Configuration saved. Decisions OFF; bankroll and previous history preserved.';strategy();}catch(e){$('promotion-status').textContent=e.message;}}
$('promote-primary').onclick=()=>promotePrimary(false);$('rollback-primary').onclick=()=>promotePrimary(true);
$('logout').onclick=async()=>{await fetch('/api/logout',{method:'POST',headers:adminHeaders});location.href='/login';};
function timing(){if(!latest)return;const now=latest.server_time+(Date.now()-receivedAt)/1000;const t=latest.market?.assetPriceTerms;const start=t?Date.parse(t.windowStart)/1000:null;const elapsed=start==null?null:now-start;$('round-progress').value=elapsed==null?0:Math.max(0,Math.min(300,elapsed));const next=[15,30,45,60,75,90].find(c=>c>elapsed);$('next-checkpoint').textContent=elapsed==null?'Next checkpoint: UNAVAILABLE':next?`Next shadow checkpoint +${next}s · in ${Math.max(0,Math.ceil(next-elapsed))}s`:'Shadow checkpoints complete';const b=latest.book,bid=b?.bids[0]?.price,ask=b?.asks[0]?.price;$('quote-pair').textContent=bid==null||ask==null?'Bid/ask: UNAVAILABLE':`UP ${cents(bid)} / ${cents(ask)} · DOWN ${cents(1-ask)} / ${cents(1-bid)} · implied UP ${pct((bid+ask)/2)} (midpoint, not executable)`;}
setInterval(timing,1000);setInterval(board,5000);setInterval(strategy,10000);board();strategy();

getJSON('/api/session').then(s=>{window.prophetRole=s.role;$('logout').hidden=!s.auth_required;if(s.role==='viewer'){for(const selector of ['[data-mode]','#toggle-paper','#start','#formal-start','#stop','#pause','#resume','#collector-pause','#collector-resume','#diagnose','#repair-feed','#alternate-repair','#test-update','#stage-update','#activate-update','#rollback-update','#request-training','#emergency','#reset','#analyze-trends','#promote-primary','#rollback-primary','#sms-test','#rollback','#adaptive-shadow','#adaptive-approved']){document.querySelectorAll(selector).forEach(button=>{button.disabled=true;button.title='Read-only account';});}}}).catch(()=>{});



async function modelPerformance(){
  try{
    const [models,picks]=await Promise.all([getJSON('/api/competition'),getJSON('/api/pick-history')]);
    const cards=document.createDocumentFragment();
    const rows=[...models].sort((a,b)=>b.realized-a.realized);
    for(const item of rows){
      const article=document.createElement('article');
      const name=document.createElement('h3');name.textContent=item.label;article.append(name);
      const p=item.prediction;
      textRow(article,'Model status',item.status);
      const share = item.vote ? item.vote.up_share : p?.up;
      textRow(article,item.vote ? 'Last vote share':'Last prediction',share==null?'—':`${pct(share)} UP / ${pct(1-share)} DOWN`);
      if(item.vote){
        const v=item.vote;
        textRow(article,'Latest model votes',`${v.up_votes} UP / ${v.down_votes} DOWN · ${v.ties} tied · ${v.missing.length} missing`);
        textRow(article,'Vote checkpoint',`+${v.checkpoint}s · ${utcTime(v.time)}`);
      }
      textRow(article,'Games / W / L / skip',`${item.games} / ${item.wins} / ${item.losses} / ${item.skips}`);
      textRow(article,'Win rate',pct(item.win_rate));
      textRow(article,'Cash / equity',`${money(item.cash)} / ${money(item.equity)}`);
      textRow(article,'Realized / unrealized / net',`${money(item.realized)} / ${money(item.unrealized)} / ${money(item.net_pnl)}`);
      textRow(article,'Exposure / drawdown',`${money(item.exposure)} / ${money(item.drawdown)}`);
      textRow(article,'Current / longest win / loss',`${item.streak.current||'—'} ${item.streak.length} / ${item.streak.longest.WIN} / ${item.streak.longest.LOSS}`);
      const details=document.createElement('details'),summary=document.createElement('summary'),version=document.createElement('p');
      summary.textContent='Model details';version.textContent=item.version||'No saved version';details.append(summary,version);
      if(p)textRow(details,'Forecast time',utcTime(p.time));
      if(item.model_detail)textRow(details,'Detail',item.model_detail);
      article.append(details);
      cards.append(article);
    }
    const panel=$('competition-cards');
    if(!panel.querySelector('details[open]'))panel.replaceChildren(cards);
    const items=document.createDocumentFragment();
    for(const pick of picks){
      const button=document.createElement('button');
      button.type='button';button.className=`history-tile ${pick.side}`;button.textContent=pick.side;
      button.onclick=()=>{$('prediction-detail').textContent=`${pick.slug} · ${utcTime(pick.time)} · ${pick.model} · ${pct(pick.up)} UP · ${pick.reason} · outcome ${pick.outcome||'pending'}`;};
      items.append(button);
    }
    $('prediction-tiles').replaceChildren(items);
  }catch(error){
    $('prediction-detail').textContent='Participant or prediction results could not refresh.';
  }
}


function explainSkip(reason){
  const labels={
    NO_POSITIVE_EDGE:'Estimated profit too small after costs',
    MODEL_MARKET_DISAGREEMENT:'Model and market favor different sides or are tied',
    NO_EXECUTABLE_EDGE:'No eligible fill meets the minimum expected return',
    HOLD_TO_SETTLEMENT_POLICY:'Holding until the official result',
    NO_EXECUTABLE_DEPTH:'Not enough usable orders at the quoted price',
    UNDERSIZED_FILL:'Too little liquidity to fill the intended amount',
    ONE_CENT_QUOTE:'One-cent quote excluded',
    MISSED_CHECKPOINT:'Fixed entry time missed',
    MODEL_PREDICTION_UNAVAILABLE:'Model had no usable prediction',
    INCOMPLETE_FEATURE_WINDOW:'Waiting for enough complete price data',
    ONE_POSITION_LIMIT:'Previous entry is still open',
    INSUFFICIENT_CASH:'Insufficient virtual cash'
  };
  return [...new Set(reason.split(','))].map(code=>labels[code]||code.toLowerCase().replaceAll('_',' ')).join(' · ');
}

async function refreshSkipDiagnostics(){
  if(!$('skip-details').open)return;
  try{
    const data=await getJSON('/api/research-diagnostics');
    const panel=$('skip-diagnostics');
    panel.replaceChildren();
    for(const item of data.adaptive){
      textRow(panel,item.account,`${item.rounds} · ${explainSkip(item.reason)}`,'connection');
    }
    for(const item of data.fixed){
      textRow(panel,item.model,`${item.attempts} · ${explainSkip(item.reason)}`,'connection');
    }
    if(!panel.childElementCount){
      panel.textContent=data.adaptive_rounds?'No skips recorded in this sample.':'Waiting for completed adaptive rounds.';
    }
  }catch(error){
    $('skip-diagnostics').textContent='Skip reasons could not refresh.';
  }
}
$('skip-details').addEventListener('toggle',refreshSkipDiagnostics);
setInterval(refreshSkipDiagnostics,10000);

async function autonomousAccounts(){
  const table=$('autonomous-accounts');
  try {
    const accounts=await getJSON('/api/model-paper');
    const rows=document.createDocumentFragment();
    for(const account of accounts){
      const row=document.createElement('tr');
      const last=account.latest;
      const action=last?.action==='CASH_OUT'?'CASH OUT':last?.action;
      for(const value of [account.label, action || 'Waiting', pct(last?.up), `${account.open} / ${account.closed}`,
        `${account.wins} / ${account.losses}`,account.skips,money(account.cash),money(account.pnl)]){
        const cell=document.createElement('td');
        cell.textContent=value;
        row.append(cell);
      }
      if(last)row.children[1].title=`${explainSkip(last.reason)} · ${utcTime(last.time)}`;
      rows.append(row);
    }
    table.replaceChildren(rows);
  } catch {
    const row=document.createElement('tr');
    const cell=document.createElement('td');
    cell.colSpan=8;
    cell.textContent='Paper account data unavailable';
    row.append(cell);
    table.replaceChildren(row);
  }
}



async function pooledConsensus() {
  const result=$('pooled-consensus-result'), round=$('pooled-consensus-round');
  try {
    const state=await getJSON('/api/pooled-consensus');
    result.textContent=`${state.settled} settled · ${state.wins} wins / ${state.losses} losses · ${state.skips} skips · Net ${money(state.net_pnl)}`;
    const comparisons = document.createDocumentFragment();
    for (const strategy of state.comparisons) {
      const row = document.createElement('tr');
      for (const value of [strategy.label, `${strategy.wins} / ${strategy.losses}`, strategy.skips, money(strategy.net_pnl)]) {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.append(cell);
      }
      comparisons.append(row);
    }
    $('pooled-comparison').replaceChildren(comparisons);
    if (state.paired) result.textContent+=` · Matched $3 ${money(state.paired_baseline_pnl)} / $100 ${money(state.paired_pooled_pnl)}`;
    if (!state.latest) {
      round.textContent='Waiting for an observed round.';
      return;
    }
    const {status,reason,side,outcome,up,down,ties,missing}=state.latest;
    const votes=`${up} UP / ${down} DOWN / ${ties} tied / ${missing.length} missing`;
    const descriptions={
      POOL_MODELS_NOT_READY:'Waiting for model forecasts',
      POOL_NOT_UNANIMOUS:'Waiting for unanimous agreement',
    };
    round.textContent=status==='FILLED'
      ? `${side} · ${outcome ? 'Settled ' + outcome : 'Holding until official settlement'} · ${votes}`
      : `${status==='SKIP' ? 'Round skipped' : 'Evaluating'} · ${descriptions[reason] || explainSkip(reason)} · ${votes}`;
  } catch {
    round.textContent='Pooled results could not refresh.';
  }
}
const resultsDialog=$('research-results-dialog');
let updatingResearch=false;

async function refreshResearchResults() {
  if(!resultsDialog.open || updatingResearch)return;
  updatingResearch=true;
  try {
    await Promise.allSettled([modelPerformance(),autonomousAccounts(),pooledConsensus(),challengerMatch(),selective90(),correctiveComparison()]);
  } finally {
    updatingResearch=false;
  }
}

async function correctiveComparison(){
  try {
    const r=await getJSON('/api/corrective-research');
    const a=r.policies.find(p=>p.policy==='gap_agreement_40_70'&&p.cost_case==='standard');
    const primary=r.primary;
    $('corrective-comparison').textContent=`Reference Gap Agreement 40–70¢ · ${a.wins}–${a.losses} · ${money(a.pnl)} · ${a.trades}/${r.target} settled fills. Primary on the same observed markets · ${primary.wins}–${primary.losses} · ${money(primary.pnl)}.`;
    const p=r.latest_panel;
    if(p){
      const v=p.payload;
      const missing=Object.entries(v.missing_reasons).map(([name,reason])=>`${name} (${reason})`);
      $('corrective-vote').textContent=`+${p.checkpoint}s panel · ${v.up_votes} UP / ${v.down_votes} DOWN / ${v.ties} tied${missing.length?' · Missing '+missing.join(', '):''}`;
    }
  } catch {
    $('corrective-comparison').textContent='Forward research temporarily unavailable.';
  }
}

async function challengerMatch(){
  const data=await getJSON('/api/research-comparison');
  const c=data.challenger,g=data.large;
  $('challenger-match').textContent=c.matched
    ? `${c.matched} matched forecasts · Baseline ${c.baseline_right} correct · Challenger ${c.challenger_right} correct · Brier ${c.baseline_brier.toFixed(3)} vs ${c.challenger_brier.toFixed(3)}`
    : 'Waiting for verified outcomes from shared forecast inputs.';
  $('large-progress').textContent=`$100 paper · ${g.qualified?'Eligibility met':'Shadow only'} · ${g.rounds}/100 distinct settled stress-test fills · ${money(g.stress_net)} after higher costs`;
}

$('open-research-results').onclick=()=>{
  resultsDialog.showModal();
  refreshResearchResults();
  board();
  strategy();
  research();
  paper();
  if(latest)renderCollectorEvents(latest.events);
};
setInterval(refreshResearchResults,5000);


async function selective90(){
  try {
    const r=await getJSON('/api/selective-forecast');
    const current=r.latest?.slug===latest?.market?.slug && latest?.market_active ? r.latest : null;
    const status=current?.status==='CALL' ? current.side : current?.status==='SKIP' ? 'Skip' : 'Waiting';
    $('selective-90-latest').textContent=`90-second shadow · ${status}`;
    $('selective-90-results').textContent=`90-second shadow · ${r.right}/${r.settled_calls} correct · ${r.skips} skips · ${r.settled_fills} settled paper fills · ${money(r.net_pnl)} net paper P&L`;
  } catch {
    $('selective-90-latest').textContent='90-second shadow · Unavailable';
  }
}

