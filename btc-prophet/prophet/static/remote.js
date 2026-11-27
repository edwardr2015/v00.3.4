'use strict';
let mainSettlement=false;
const $=id=>document.getElementById(id);
const money=n=>n==null?'—':(n<0?'-$':'$')+Math.abs(n).toFixed(2);
const tone=(el,n)=>{el.classList.remove('pnl-up','pnl-down');if(n>0)el.classList.add('pnl-up');if(n<0)el.classList.add('pnl-down');};
async function api(path,method='GET'){
  const r=await fetch(path,{method,headers:{'X-Prophet-Control':'local-dashboard'},credentials:'same-origin'});
  if(r.status===401){location.href='/login?next=/remote';throw Error('Sign in required');}
  const data=await r.json().catch(()=>({}));
  if(!r.ok)throw Error(data.detail||('Error '+r.status));
  return data;
}
function say(t){$('message').textContent=t;}
async function state(){
  try{
    const s=await api('/api/state');
    const mode=s.mode||s.paper_mode||'—';
    $('r-mode').textContent=s.emergency?'Safety lock on':mode==='SIMULATE'?'Running (paper)':mode==='OFF'?'Paused':mode;
    $('r-updated').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){$('r-mode').textContent='Not reachable';$('r-updated').textContent=e.message;}
  try{
    const g=await api('/api/unified');
    $('r-cash').textContent=money(g.balance);
    $('r-games').textContent=`${g.played} games · ${g.wins}W ${g.losses}L`;
    $('r-net').textContent=money(g.net);tone($('r-net'),g.net);
    $('r-all').textContent=`${g.all_time.played} · ${g.all_time.wins}W ${g.all_time.losses}L · ${money(g.all_time.net)}`;
    $('r-pos').textContent=g.position?`${g.position.side||''} ${g.position.status||'open'}`:'None';
    const rows=[];
    const add=(name,a)=>{if(!a)return;const d=document.createElement('div');d.className='row';
      const n=document.createElement('span');n.textContent=name;const v=document.createElement('b');
      v.textContent=`${money(a.balance)} · ${a.wins}W ${a.losses}L · ${money(a.net)}`;tone(v,a.net);d.append(n,v);rows.push(d);};
    add('Selective $50',g.selective50);add('Selective $1,000 at $50',g.selective1000);
    for(const r of g.shadow_rules||[])add(r.name,r);
    for(const r of g.fresh_accounts||[])add(r.name,r);
    const f=g.freshness||{};
    $('f-lag').textContent=f.rounds?`${f.typical_shift>0?'+':''}${f.typical_shift}s typical shift · ${f.exact_at_zero}/${f.rounds} rounds exact at 0`:'Waiting for settled rounds';
    $('f-spot').textContent=f.brti_minus_spot==null?'—':`$${f.brti_minus_spot.toFixed(2)} avg gap · BRTI age ${(f.reference_age||0).toFixed(1)}s`;
    $('f-main').textContent=g.main_settlement?'Official rule (switch ON)':'Standard (switch off)';
    mainSettlement=!!g.main_settlement;$('f-toggle').textContent=mainSettlement?'Main: back to standard':'Main: use official rule';
    if(rows.length)$('r-accounts').replaceChildren(...rows);
  }catch(e){}
}
function showUpdate(u){
  $('u-build').textContent='dev'+u.build;
  let text=u.message||'Not checked yet.';
  if(u.checked)text+=' (checked '+new Date(u.checked*1000).toLocaleTimeString()+')';
  if(!u.supervised&&u.build)text+=' Restart from the phone works after you reopen Start Prophet once.';
  $('u-status').textContent=text;
  $('u-install').disabled=!u.pending;
}
async function updates(){try{showUpdate(await api('/api/remote/status'));}catch(e){}}
async function act(path,done){try{const d=await api(path,'POST');say(done||d.message||'Done.');}catch(e){say(e.message);}state();updates();}
$('r-pause').onclick=()=>act('/api/control/pause','Paused. Open positions settle as normal.');
$('r-resume').onclick=()=>act('/api/control/resume','Resumed.');
$('u-check').onclick=async()=>{say('Checking GitHub…');try{showUpdate(await api('/api/remote/check','POST'));say('');}catch(e){say(e.message);}};
$('u-install').onclick=()=>{if(confirm('Back up the database, install the update and restart?'))act('/api/remote/install');};
$('u-restart').onclick=()=>{if(confirm('Restart the bot now? Pause first.'))act('/api/remote/restart');};
state();updates();setInterval(state,5000);setInterval(updates,15000);
$('f-toggle').onclick=()=>{if(confirm(mainSettlement?'Switch the main account back to the standard forecast?':'Switch the main $1,000 paper account to the official-rule forecast?'))act('/api/remote/main-model/'+(mainSettlement?'standard':'settlement'));};
