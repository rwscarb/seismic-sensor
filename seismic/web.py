import json
import threading
import time

from seismic.config import (
    WEB_PORT, THRESHOLD, N_CONSENSUS, STATIONS, SEEDLINK_SERVER,
    USGS_MIN_MAG, EMSC_MIN_MAG, UMAMI_SITE_ID, LOC_MIN_STA,
    CONSENSUS_WINDOW, USGS_SIG_MIN_MAG, SLACK_SIGNING_SECRET,
    SERVER_START_TIME,
)
from seismic.localize import station_coords, locate_epicenter
from seismic.state import sensor_state
from seismic.watcher import _expected_p_arrival, _find_matching_detection

# ── Web UI ────────────────────────────────────────────────────────────────────
_WEB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seismic Sensor — %(app_title)s</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%230d1117'/%3E%3Cpolyline points='1,16 5,16 7,10 9,22 11,8 13,24 15,13 17,19 19,16 23,16 25,11 27,16 31,16' fill='none' stroke='%23f85149' stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/%3E%3C/svg%3E">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:'Courier New',monospace;font-size:13px}
*{scrollbar-width:thin;scrollbar-color:#30363d transparent}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#30363d;border-radius:2px}
::-webkit-scrollbar-thumb:hover{background:#484f58}
header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:15px;color:#58a6ff;letter-spacing:1px}
#cfg{color:#6e7681;font-size:11px}
[title]{cursor:help}
#status-dot{width:8px;height:8px;border-radius:50%;background:#238636;animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#last-update{color:#6e7681;font-size:11px;margin-left:auto}
#last-event-summary{font-size:11px;color:#8b949e;border-left:1px solid #30363d;padding-left:12px}
.grid{display:grid;grid-template-columns:220px 1fr 320px;gap:12px;padding:12px;height:calc(100vh - 50px)}
.panel{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px;min-width:0}
.panel-hdr{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px}
.panel-hdr h2{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}
.det-count{font-size:10px;color:#6e7681;background:#21262d;border-radius:8px;padding:1px 6px}
.station{padding:6px 0;border-bottom:1px solid #21262d}
.station:last-child{border-bottom:none}
.sta-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px}
.sta-name{color:#58a6ff;font-weight:bold}
.sta-conf{font-size:11px}
.conf-bar{height:4px;border-radius:2px;background:#21262d;margin-top:2px}
.conf-fill{height:100%;border-radius:2px;transition:width .5s}
.det{display:flex;flex-direction:column;padding:7px 12px;border-bottom:1px solid #21262d;font-size:11px;min-width:0}
.det:last-child{border-bottom:none}
.det-selected{background:#0d2a15!important;box-shadow:inset 2px 0 0 #3fb950}
.det:hover:not(.det-selected){background:#1c2128}
.det-muted{opacity:0.55}
.det-verified{position:relative;z-index:1;border:1px solid #2ea043!important}
.det-verified+.det-verified{margin-top:-1px}
.det-row1{display:flex;justify-content:space-between;align-items:baseline;gap:4px}
.det-row2{display:flex;align-items:center;gap:4px;margin-top:3px;min-width:0}
.det-time{color:#8b949e;font-size:10px;white-space:nowrap;font-variant-numeric:tabular-nums}
.det-stas{color:#58a6ff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;font-size:10px}
.det-chips-inline{display:flex;gap:3px;flex-shrink:0;align-items:center;margin-left:auto}
.det-usgs-icon{flex-shrink:0;font-size:12px;line-height:1;cursor:default}.det-usgs-icon[href]{cursor:pointer}
.det-age{color:#6e7681;font-size:10px;white-space:nowrap;letter-spacing:.2px}
.det-deploy-sep{display:flex;align-items:center;gap:6px;padding:5px 0;color:#d29922;font-size:10px;letter-spacing:.5px}
.det-deploy-sep::before,.det-deploy-sep::after{content:'';flex:1;border-top:1px solid #2a1f00}
.chip{font-size:10px;border-radius:3px;padding:1px 5px;font-weight:bold;white-space:nowrap}
.fault-tip{background:#1a1209;border:1px solid #e36209;color:#e8c07a;font-size:10px;padding:2px 6px;border-radius:3px;white-space:nowrap}
.det-tip{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:7px 10px;box-shadow:0 4px 16px rgba(0,0,0,.6);font-size:11px;color:#c9d1d9;white-space:nowrap;pointer-events:none}
.det-tip .tip-time{color:#8b949e;font-size:10px;letter-spacing:.3px;font-variant-numeric:tabular-nums}
.det-tip .tip-mb{font-size:14px;font-weight:600;letter-spacing:.05em}
.det-tip .tip-mb.high{color:#f85149}.det-tip .tip-mb.mid{color:#d29922}.det-tip .tip-mb.low{color:#58a6ff}
.det-tip .tip-stas{color:#8b949e;font-size:10px;margin-top:3px}
.det-tip .tip-loc{color:#6e7681;font-size:10px;margin-top:2px}
.leaflet-tooltip.det-tip::before{display:none}
.sta-tip{background:#0d1117;border:1px solid #21262d;border-radius:5px;padding:5px 9px;box-shadow:0 3px 12px rgba(0,0,0,.5);font-size:10px;color:#c9d1d9;white-space:nowrap;pointer-events:none}
.sta-tip .tip-key{color:#58a6ff;font-weight:600;font-size:11px}
.sta-tip .tip-conf{margin-top:2px}
.leaflet-tooltip.sta-tip::before{display:none}
.chip-mb-low{color:#3fb950;background:#0d2a15}
.chip-mb-mid{color:#d29922;background:#2a1f00}
.chip-mb-high{color:#f85149;background:#2d1216}
.chip-mb-approx{opacity:.8;font-style:italic}
.chip-epi{color:#d29922;background:#2a1f00}
.chip-usgs{color:#a371f7;background:#1e1129}
.chip-emsc{color:#39c5cf;background:#0d1f21}
#left-panel{overflow-y:auto;min-height:0}
#map{height:100%;border-radius:4px;background:#000}
#map-wrap{position:relative;border-radius:4px;overflow:hidden;min-height:0}
.right-col{display:flex;flex-direction:column;min-height:0;background:#161b22;border:1px solid #30363d;border-radius:6px;overflow:hidden}
.no-data{color:#6e7681;font-style:italic;font-size:11px}
/* fullscreen map */
#fs-btn{position:absolute;top:6px;right:6px;z-index:1000;background:#161b22cc;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer;backdrop-filter:blur(4px)}
#fs-btn:hover{color:#e6edf3;border-color:#58a6ff}
body.fs-mode .grid{display:block}
body.fs-mode #fs-btn{right:350px}
body.fs-mode .right-col{position:fixed!important;top:50px;right:10px;bottom:10px;width:330px;z-index:1001;background:#161b22bb;backdrop-filter:blur(10px);border:1px solid #30363d;border-radius:8px;overflow:hidden;display:flex!important;flex-direction:column}
body.fs-mode .right-col .panel-hdr{background:transparent}
body.fs-mode .right-col{border:none;border-radius:8px}
body.fs-mode #left-panel{display:none}
body.fs-mode #map-wrap{position:fixed;inset:0;z-index:500;border-radius:0;margin:0;top:50px}
body.fs-mode #map{height:100%!important;border-radius:0}
body.fs-mode header{z-index:502;position:relative}
/* fullscreen overlay */
#fs-overlay{display:none;position:fixed;top:55px;left:10px;z-index:1001;background:#161b22cc;border:1px solid #30363d;border-radius:6px;padding:10px 14px;min-width:220px;max-width:280px;backdrop-filter:blur(6px);font-size:11px;pointer-events:none}
body.fs-mode #fs-overlay{display:block}
#fs-overlay h3{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.fso-sta{display:flex;justify-content:space-between;margin-bottom:4px;align-items:center}
.fso-bar{height:3px;border-radius:2px;background:#21262d;margin-top:2px;margin-bottom:6px}
.fso-bar-fill{height:100%;border-radius:2px}
.fso-det{margin-top:8px;border-top:1px solid #21262d;padding-top:8px}
</style>
<script defer src="//u.fib896.com/script.js" data-website-id="%(umami_id)s"></script>
</head>
<body>
<header>
  <div id="status-dot"></div>
  <h1>&#127757; Seismic Sensor</h1>
  <span id="cfg" title="SeedLink: %(seedlink)s">%(cfg_text)s</span>
  <span id="last-event-summary"></span>
  <span id="last-update">connecting...</span>
  <button id="faults-btn" title="Toggle active fault overlay (GEM Global Active Faults)" style="background:#161b22;border:1px solid #30363d;color:#6e7681;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;margin-left:4px">&#9889; faults</button>
  <button id="mute-btn" title="Toggle audio alerts" style="background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;margin-left:4px">&#128266; on</button>
  <select id="tz-sel" title="Display timezone" style="background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:2px 5px;font-size:11px;cursor:pointer;margin-left:6px">
    <option value="auto">Auto TZ</option>
    <option value="UTC">UTC</option>
    <option value="America/Los_Angeles">Pacific</option>
    <option value="America/Denver">Mountain</option>
    <option value="America/Chicago">Central</option>
    <option value="America/New_York">Eastern</option>
    <option value="Europe/London">London</option>
    <option value="Europe/Paris">Paris/Berlin</option>
    <option value="Europe/Helsinki">Helsinki/Athens</option>
    <option value="Europe/Moscow">Moscow</option>
    <option value="Asia/Dubai">Dubai</option>
    <option value="Asia/Kolkata">India</option>
    <option value="Asia/Bangkok">Bangkok</option>
    <option value="Asia/Tokyo">Tokyo</option>
    <option value="Australia/Sydney">Sydney</option>
  </select>
</header>
<div class="grid">
  <div class="panel" id="left-panel">
    <div class="panel-hdr"><h2>Stations</h2></div>
    <div id="stations"></div>
  </div>
  <div id="map-wrap">
    <button id="fs-btn" title="Toggle fullscreen map">&#x26F6;</button>
    <div id="map"></div>
    <div id="fs-overlay">
      <h3>Stations</h3>
      <div id="fso-stations"></div>
      <div id="fso-det" class="fso-det"></div>
    </div>
  </div>
  <div class="right-col">
    <div class="panel-hdr" style="padding:10px 12px;border-bottom:1px solid #21262d;margin-bottom:0"><h2>Detections</h2><span id="det-count" class="det-count"></span><button id="filter-btn" title="Show confirmed catalog matches only" style="margin-left:auto;background:#0d1117;border:1px solid #30363d;color:#6e7681;border-radius:4px;padding:2px 7px;font-size:10px;cursor:pointer">✓ confirmed</button></div>
    <div style="flex:1;overflow-y:auto;min-height:0">
      <div id="detections"></div>
    </div>
    <div id="det-more" style="display:none;padding:6px 8px;border-top:1px solid #21262d"><button onclick="showMoreDets()" style="width:100%;background:#21262d;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:4px 10px;font-size:10px;cursor:pointer">↓ show older</button></div>
  </div>
</div>
<script>
const map = L.map('map', {zoomControl:false}).setView([45,10],2);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {attribution:'&copy; OSM &copy; CARTO',subdomains:'abcd',maxZoom:19}).addTo(map);
const staMarkers={}, detMarkers=[];
let sCoords=%(station_coords_json)s;
let lastFlyTs=null, selectedDetTs=null, _pulseIv=null, _pulsePhase=0;
let filterConfirmed=false, detDisplayLimit=20;
// fault overlay — lazy loaded from GEM Global Active Faults dataset
let _faultLayer=null, _faultLoading=false, _faultOn=false;
const _FAULT_URL='https://raw.githubusercontent.com/GEMScienceTools/gem-global-active-faults/master/geojson/gem_active_faults.geojson';
const _faultsBtn=document.getElementById('faults-btn');
function _setFaultBtnState(){
  _faultsBtn.style.color=_faultOn?'#d29922':'#6e7681';
  _faultsBtn.style.borderColor=_faultOn?'#d29922':'#30363d';
  _faultsBtn.textContent=_faultLoading?'⟳ loading…':(_faultOn?'⚡ faults ✓':'⚡ faults');
}
_faultsBtn.addEventListener('click',async()=>{
  if(_faultLoading)return;
  _faultOn=!_faultOn;
  if(!_faultOn){
    if(_faultLayer){map.removeLayer(_faultLayer);}
    _setFaultBtnState();return;
  }
  if(_faultLayer){_faultLayer.addTo(map);_setFaultBtnState();return;}
  _faultLoading=true;_setFaultBtnState();
  try{
    const r=await fetch(_FAULT_URL);
    const geojson=await r.json();
    _faultLayer=L.geoJSON(geojson,{
      style:{color:'#e36209',weight:1,opacity:0.45},
      onEachFeature:(f,layer)=>{
        const n=f.properties&&(f.properties.name||f.properties.fault_name||f.properties.FaultName||'');
        if(n)layer.bindTooltip(n,{sticky:true,className:'fault-tip'});
      }
    }).addTo(map);
  }catch(e){_faultOn=false;alert('Failed to load fault data: '+e.message);}
  _faultLoading=false;_setFaultBtnState();
});
function showMoreDets(){detDisplayLimit+=50;}
(()=>{
  const btn=document.getElementById('filter-btn');
  if(!btn)return;
  btn.addEventListener('click',()=>{
    filterConfirmed=!filterConfirmed;
    detDisplayLimit=100;
    btn.style.color=filterConfirmed?'#3fb950':'#6e7681';
    btn.style.borderColor=filterConfirmed?'#3fb950':'#30363d';
  });
})();
function confColor(c){return c>=0.835?'#3fb950':c>=0.5?'#d29922':'#6e7681'}
function fmtAge(ts){const s=Math.round(Date.now()/1000-ts);return s<60?s+'s':s<3600?Math.round(s/60)+'m':Math.round(s/3600)+'h'}
const _browserTz=Intl.DateTimeFormat().resolvedOptions().timeZone;
let _userTz=localStorage.getItem('tz')||'auto';
function _activeTz(){return _userTz==='auto'?_browserTz:_userTz;}
function _tzAbbr(){
  try{return new Intl.DateTimeFormat('en',{timeZone:_activeTz(),timeZoneName:'short'}).formatToParts(new Date()).find(p=>p.type==='timeZoneName').value;}
  catch(e){return _activeTz();}
}
function fmtLocal(isoStr){
  const d=new Date(isoStr);
  const tz=_activeTz();
  const timeStr=d.toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:tz});
  const today=new Date().toLocaleDateString('en',{timeZone:tz});
  const detDay=d.toLocaleDateString('en',{timeZone:tz});
  const prefix=today===detDay?'':d.toLocaleDateString('en',{timeZone:tz,month:'short',day:'numeric'})+' ';
  return `${prefix}${timeStr} ${_tzAbbr()}`;
}
// Timezone selector
(()=>{
  const sel=document.getElementById('tz-sel');
  if(!sel)return;
  sel.value=_userTz;
  if(!sel.value)sel.value='auto';
  sel.addEventListener('change',()=>{_userTz=sel.value;localStorage.setItem('tz',_userTz);});
})();
// fullscreen toggle — uses browser Fullscreen API (hides browser chrome like F11)
const _fsBtn=document.getElementById('fs-btn');
function _applyFsMode(on){
  document.body.classList.toggle('fs-mode',on);
  _fsBtn.textContent=on?'✕':'⛶';
  _fsBtn.title=on?'Exit fullscreen':'Toggle fullscreen';
  setTimeout(()=>map.invalidateSize(),150);
}
_fsBtn.addEventListener('click',()=>{
  if(!document.fullscreenElement){
    document.documentElement.requestFullscreen().catch(()=>{
      // Fallback: browser denied fullscreen (e.g. iframe sandbox) — use CSS-only mode
      _applyFsMode(true);
    });
  } else {
    document.exitFullscreen();
  }
});
document.addEventListener('fullscreenchange',()=>{
  _applyFsMode(!!document.fullscreenElement);
});
// Esc is handled by the browser when in native fullscreen; cover the CSS-only fallback case
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!document.fullscreenElement)_applyFsMode(false);});
let _pulseTs=null;
// Zoom-scaled radius: full size at zoom ≥ 8, progressively smaller below that.
function zoomR(base){
  const z=map.getZoom();
  const f=Math.max(0.35,Math.min(1.0,(z-1)/7));
  return Math.max(2,base*f);
}
function applyMarkerSelection(){
  detMarkers.forEach(({m,ts,r})=>{
    if(ts===selectedDetTs){
      m.setStyle({color:'#2ea043',weight:1,fillColor:'#3fb950',fillOpacity:.95});
      if(_pulseTs!==selectedDetTs){
        if(_pulseIv){clearInterval(_pulseIv);_pulseIv=null;}
        _pulseTs=selectedDetTs;_pulsePhase=0;
        _pulseIv=setInterval(()=>{
          _pulsePhase=(_pulsePhase+0.15)%(2*Math.PI);
          const p=Math.abs(Math.sin(_pulsePhase));
          m.setRadius(zoomR(r)+p*5);
          m.setStyle({fillOpacity:.55+p*.4});
        },40);
      }
    } else {
      m.setRadius(zoomR(r));
      m.setStyle({color:'#c0392b',fillColor:'#f85149',fillOpacity:selectedDetTs?0.2:0.85});
    }
  });
  if(!selectedDetTs&&_pulseIv){clearInterval(_pulseIv);_pulseIv=null;_pulseTs=null;}
}
map.on('zoomend',()=>applyMarkerSelection());
function applyRowSelection(){
  document.querySelectorAll('.det[data-ts]').forEach(el=>{
    if(el.dataset.ts===selectedDetTs)el.classList.add('det-selected');
    else el.classList.remove('det-selected');
  });
}
function flyToEpi(lat,lon,ts){
  selectedDetTs=ts||null;
  applyRowSelection();
  applyMarkerSelection();
  map.getPane('overlayPane').style.visibility='hidden';
  map.flyTo([lat,lon],5,{duration:1.0,easeLinearity:0.5});
  map.once('moveend',()=>{map.getPane('overlayPane').style.visibility='';applyMarkerSelection();});
}
// audio alert
let audioEnabled=true;
let lastDetTs=null;
let audioCtx=null;
const muteBtn=document.getElementById('mute-btn');
muteBtn.addEventListener('click',()=>{
  audioEnabled=!audioEnabled;
  muteBtn.textContent=audioEnabled?'🔔 on':'🔕 off';
  muteBtn.style.color=audioEnabled?'#8b949e':'#6e7681';
  if(!audioCtx)audioCtx=new(window.AudioContext||window.webkitAudioContext)();
});
function playDetectionAlert(){
  if(!audioEnabled)return;
  try{
    if(!audioCtx)audioCtx=new(window.AudioContext||window.webkitAudioContext)();
    if(audioCtx.state==='suspended')audioCtx.resume();
    [[880,0],[660,0.18],[440,0.32]].forEach(([freq,t])=>{
      const osc=audioCtx.createOscillator();
      const gain=audioCtx.createGain();
      osc.connect(gain);gain.connect(audioCtx.destination);
      osc.frequency.value=freq;osc.type='sine';
      gain.gain.setValueAtTime(0.25,audioCtx.currentTime+t);
      gain.gain.exponentialRampToValueAtTime(0.001,audioCtx.currentTime+t+0.16);
      osc.start(audioCtx.currentTime+t);
      osc.stop(audioCtx.currentTime+t+0.18);
    });
  }catch(e){}
}
// browser desktop notification
if('Notification' in window && Notification.permission==='default'){
  Notification.requestPermission();
}
function showDesktopNotification(det){
  if(!audioEnabled)return;
  if(!('Notification' in window)||Notification.permission!=='granted')return;
  const mbStr=det.mb!=null?(det.mb_local?'local':det.mb_approx?`mb~${det.mb.toFixed(1)}`:`mb=${det.mb.toFixed(1)}`):'mb computing';
  new Notification('🌍 Seismic Detection',{
    body:`${det.stations.join(' · ')} | ${mbStr}`,
    tag:'seismic-det',
    renotify:true,
    silent:true,
  });
}
function update(){
  fetch('/api/state').then(r=>r.json()).then(d=>{
    document.getElementById('last-update').textContent='updated '+new Date().toLocaleTimeString();
    if(d.station_coords)Object.assign(sCoords,d.station_coords);
    // stations
    const sDiv=document.getElementById('stations');
    let sHtml='';
    Object.entries(d.stations).sort((a,b)=>b[1].conf-a[1].conf).forEach(([k,s])=>{
      const pct=Math.round(s.conf*100);
      const col=confColor(s.conf);
      const coord=sCoords[k]?`${sCoords[k][0].toFixed(2)}°N ${sCoords[k][1].toFixed(2)}°E`:'coords unknown';
      const cardTitle=`${k}\n${coord}\nconf: ${s.conf.toFixed(4)}\nlast sample: ${fmtLocal(new Date(s.last_ts*1000).toISOString())}`;
      const barTitle=`threshold: %(threshold)s | current: ${s.conf.toFixed(3)}`;
      sHtml+=`<div class="station" title="${cardTitle}">
        <div class="sta-row"><span class="sta-name">${k}</span>
        <span class="sta-conf" style="color:${col}">${s.conf.toFixed(3)}</span></div>
        <div class="conf-bar" title="${barTitle}"><div class="conf-fill" style="width:${pct}%;background:${col}"></div></div>
        <div style="color:#6e7681;font-size:10px">${coord} &mdash; ${fmtAge(s.last_ts)} ago</div>
      </div>`;
      if(sCoords[k] && !staMarkers[k]){
        const [lat,lon]=sCoords[k];
        staMarkers[k]=L.circleMarker([lat,lon],{radius:4,color:'#3a6fa8',weight:1,fillColor:'#58a6ff',fillOpacity:.9})
          .bindTooltip(`<div class="sta-tip"><span class="tip-key">${k}</span><div class="tip-conf">${coord}</div></div>`,
            {permanent:false,direction:'top',className:'sta-tip'}).addTo(map);
      }
      if(staMarkers[k]){
        const mc=confColor(s.conf);
        if(staMarkers[k].options.fillColor!==mc)
          staMarkers[k].setStyle({color:mc,fillColor:mc,fillOpacity:.9});
        const confPct=Math.round(s.conf*100);
        const confColor2=confColor(s.conf);
        const tip=`<div class="sta-tip"><span class="tip-key">${k}</span><div class="tip-conf">${coord}</div>`
          +`<div style="margin-top:3px;color:${confColor2};font-size:10px">conf ${confPct}%`
          +(s.last_ts?` &middot; ${fmtAge(s.last_ts)} ago`:'')+'</div></div>';
        if(staMarkers[k]._tooltip&&staMarkers[k]._tooltip._content!==tip)
          staMarkers[k].setTooltipContent(tip);
      }
    });
    if(sDiv.innerHTML!==sHtml)sDiv.innerHTML=sHtml;
    // detections
    const dDiv=document.getElementById('detections');
    const dets=[...d.detections].reverse();
    const filteredDets=filterConfirmed?dets.filter(det=>det.usgs):dets;
    const cntEl=document.getElementById('det-count');
    if(cntEl)cntEl.textContent=filterConfirmed
      ?`${filteredDets.length} confirmed`
      :(d.detections.length?`${d.detections.length} total`:'');
    // alert on new detection
    if(dets.length){
      const newest=dets[0];
      if(lastDetTs!==null && newest.ts!==lastDetTs){
        playDetectionAlert();
        showDesktopNotification(newest);
      }
      lastDetTs=newest.ts;
    }
    // last-event summary in header
    const sumEl=document.getElementById('last-event-summary');
    if(sumEl&&dets.length){
      const ld=dets[0];
      const mbStr=ld.mb!=null?(ld.mb_local?'local':ld.mb_approx?`mb~${ld.mb.toFixed(1)}`:`mb=${ld.mb.toFixed(1)}`):'mb…';
      sumEl.textContent=`Last: ${mbStr} · ${fmtAge(ld.unix_ts)} ago`;
    }
    if(!dets.length){dDiv.innerHTML='<div class="no-data">No detections yet</div>';return}
    const escAttr=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
    const mbChipClass=mb=>mb>=5?'chip-mb-high':mb>=4?'chip-mb-mid':'chip-mb-low';
    const serverStart=d.server_start||0;
    const deployLabel=(()=>{const dt=new Date(serverStart*1000);return dt.toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',hour12:false,timeZone:_activeTz()})+' '+_tzAbbr();})();
    let sepInserted=false;
    const moreEl=document.getElementById('det-more');
    if(moreEl)moreEl.style.display=filteredDets.length>detDisplayLimit?'block':'none';
    const newHtml=filteredDets.slice(0,detDisplayLimit).map(det=>{
      let sep='';
      if(!sepInserted && det.unix_ts < serverStart){
        sepInserted=true;
        sep=`<div class="det-deploy-sep" title="Process restarted / new version deployed at ${fmtLocal(new Date(serverStart*1000).toISOString())}">deployed ${deployLabel}</div>`;
      }
      // time — local time in card, full UTC in tooltip
      const tPart=fmtLocal(det.ts);
      // mb chip (center)
      let mbChip='';
      if(det.mb!=null){
        if(det.mb_local){
          mbChip=`<span class="chip chip-mb-approx" title="Amplitude ratio between stations suggests a local/regional source; IASPEI mb unreliable">local</span>`;
        } else {
          const lbl=(det.mb_approx?'mb~':'mb=')+det.mb.toFixed(1);
          const cls=mbChipClass(det.mb)+(det.mb_approx?' chip-mb-approx':'');
          mbChip=`<span class="chip ${cls}" title="${det.mb_approx?'approx, assumed distance 45 deg':'IASPEI body-wave'}">${lbl}</span>`;
        }
      } else {
        mbChip=`<span class="chip" style="color:#6e7681;background:#161b22">mb…</span>`;
      }
      // epicenter chip — clickable to fly map to location
      let epiChip='';
      if(det.epicenter){
        if(det.teleseismic){
          epiChip=`<span class="chip chip-epi" title="Localization unreliable (high residual) — likely distant teleseismic source" style="opacity:.7">&#x1F310; teleseismic</span>`;
        } else {
          const [la,lo]=det.epicenter;
          const ns=la>=0?'N':'S', ew=lo>=0?'E':'W';
          epiChip=`<button class="chip chip-epi" onclick="event.stopPropagation();flyToEpi(${la},${lo},'${det.ts}')" title="${Math.abs(la).toFixed(2)}°${ns} ${Math.abs(lo).toFixed(2)}°${ew}" style="cursor:pointer;border:none;font-family:inherit">&#x1F4CD;</button>`;
        }
      }
      // catalog icon — right-aligned checkmark/cross
      let usgsIcon='';
      let usgsTitle='';
      if(det.usgs){
        const place=det.usgs.place||'';
        const mt=det.usgs.magType||'';
        const src=det.usgs.source||'usgs';
        const srcLabel=src==='emsc'?'EMSC':'USGS';
        const iconColor=src==='emsc'?'#39c5cf':'#a371f7';
        usgsTitle=`${srcLabel}: M${det.usgs.mag}${mt} — ${place}`;
        const eid=det.usgs.event_id||'';
        const href=eid?(src==='emsc'
          ?`https://www.seismicportal.eu/eventdetails.html?unid=${encodeURIComponent(eid)}`
          :`https://earthquake.usgs.gov/earthquakes/eventpage/${encodeURIComponent(eid)}/executive`):'';
        const inner=`<span style="color:${iconColor}">&#10003;</span>`;
        usgsIcon=href
          ?`<a class="det-usgs-icon" href="${href}" target="_blank" rel="noopener" title="${escAttr(usgsTitle)}" style="text-decoration:none" onclick="event.stopPropagation()">${inner}</a>`
          :`<span class="det-usgs-icon" title="${escAttr(usgsTitle)}">${inner}</span>`;
      } else if(det.usgs_checked){
        usgsTitle=`No match in USGS (M%(usgs_min_mag)s+) or EMSC (M%(emsc_min_mag)s+) for this window`;
        usgsIcon=`<span class="det-usgs-icon" style="color:#30363d" title="${escAttr(usgsTitle)}">&#10007;</span>`;
      } else {
        usgsIcon=`<span class="det-usgs-icon" style="color:#6e7681" title="Catalog lookup pending">&#8943;</span>`;
      }
      // full tooltip
      const place=det.usgs?(det.usgs.place||''):'';
      const magType=det.usgs?(det.usgs.magType||''):'';
      const mbNote=det.mb!=null?(det.mb_local?'local source (amp ratio > 5x)':det.mb_approx?`mb~${det.mb.toFixed(1)} IASPEI Δ≈45°`:`mb=${det.mb.toFixed(1)} IASPEI`):'mb pending';
      const detTitle=`${det.ts}\n${det.stations.join(', ')}\nconf: ${det.conf.toFixed(4)}  gap: ${(det.logit_gap||0).toFixed(1)}`
        +(det.epicenter?`\nepi: ${det.epicenter[0].toFixed(2)}N ${det.epicenter[1].toFixed(2)}E`:'')
        +`\n${mbNote}`
        +(det.usgs?(()=>{const src=(det.usgs.source||'usgs').toUpperCase();return `\n${src}: M${det.usgs.mag}${magType} — ${place}`;})():det.usgs_checked?`\nNo catalog match (USGS M%(usgs_min_mag)s+ / EMSC M%(emsc_min_mag)s+)`:`\nCatalog lookup pending`);
      const selCls=det.ts===selectedDetTs?' det-selected':'';
      const canClick=!det.teleseismic&&det.epicenter;
      const mutedCls=canClick?'':' det-muted';
      const verifCls=(det.usgs&&canClick)?' det-verified':'';
      const rowClick=canClick
        ?`onclick="flyToEpi(${det.epicenter[0]},${det.epicenter[1]},'${det.ts}')" style="cursor:pointer"`:'';
      return sep+`<div class="det${selCls}${mutedCls}${verifCls}" data-ts="${det.ts}" ${rowClick} title="${escAttr(detTitle)}">
        <div class="det-row1"><span class="det-time">${tPart}</span><span class="det-age">${fmtAge(det.unix_ts)}</span></div>
        <div class="det-row2"><span class="det-stas">${det.stations.join(' · ')}</span><span class="det-chips-inline">${mbChip}${epiChip}${usgsIcon}</span></div>
      </div>`;
    }).join('');
    if(dDiv.innerHTML!==newHtml)dDiv.innerHTML=newHtml;
    // epicenter markers — diff by ts to avoid destroying open popups/pulse
    const epiDets=d.detections.filter(det=>det.epicenter&&!det.teleseismic);
    const epiTsSet=new Set(epiDets.map(det=>det.ts));
    const keptTs=new Set();
    // remove stale markers
    detMarkers.forEach(({m,ts})=>{if(!epiTsSet.has(ts))map.removeLayer(m);else keptTs.add(ts);});
    const kept=detMarkers.filter(({ts})=>keptTs.has(ts));
    // add new markers
    let markersChanged=kept.length!==detMarkers.length;
    epiDets.forEach(det=>{
      if(keptTs.has(det.ts))return;
      markersChanged=true;
      const [la,lo]=det.epicenter;
      const mb=det.mb||4;
      const r=Math.max(4,Math.min(14,(mb-2)*3+4));
      const mbLabel=det.mb?(det.mb_local?'local':det.mb_approx?'mb~'+det.mb.toFixed(1):'mb='+det.mb.toFixed(1)):'mb pending';
      const mbClass=mb>=5?'high':mb>=4?'mid':'low';
      const locStr=det.epicenter?`${Math.abs(det.epicenter[0]).toFixed(2)}°${det.epicenter[0]>=0?'N':'S'} `
        +`${Math.abs(det.epicenter[1]).toFixed(2)}°${det.epicenter[1]>=0?'E':'W'}`:'';
      const tipHtml=`<div class="det-tip">`
        +`<div class="tip-time">${fmtLocal(det.ts)}</div>`
        +`<div class="tip-mb ${mbClass}">${mbLabel}</div>`
        +`<div class="tip-stas">${det.stations.join(' · ')}</div>`
        +(locStr?`<div class="tip-loc">${locStr}</div>`:'')
        +'</div>';
      const m=L.circleMarker([la,lo],{radius:zoomR(r),color:'#c0392b',weight:1,fillColor:'#f85149',fillOpacity:.85})
        .bindTooltip(tipHtml,{sticky:false,direction:'top',className:'det-tip'}).addTo(map);
      kept.push({m,ts:det.ts,r});
    });
    detMarkers.length=0;kept.forEach(x=>detMarkers.push(x));
    if(markersChanged)applyMarkerSelection();
    // flyTo newest non-teleseismic epicenter when it first appears
    const newestEpi=dets.find(det=>det.epicenter&&!det.teleseismic);
    if(newestEpi && newestEpi.ts!==lastFlyTs){
      lastFlyTs=newestEpi.ts;
      selectedDetTs=newestEpi.ts;
      applyRowSelection();
      const [la,lo]=newestEpi.epicenter;
      map.getPane('overlayPane').style.visibility='hidden';
      map.flyTo([la,lo],5,{duration:1.0,easeLinearity:0.5});
      map.once('moveend',()=>{map.getPane('overlayPane').style.visibility='';applyMarkerSelection();});
    }
    // fullscreen overlay: station list + latest detection
    const fsoSta=document.getElementById('fso-stations');
    const fsoDet=document.getElementById('fso-det');
    if(fsoSta){
      fsoSta.innerHTML=Object.entries(d.stations).sort((a,b)=>b[1].conf-a[1].conf).map(([k,s])=>{
        const col=confColor(s.conf);
        const pct=Math.round(s.conf*100);
        return `<div class="fso-sta"><span style="color:#58a6ff">${k}</span><span style="color:${col}">${s.conf.toFixed(3)}</span></div>
          <div class="fso-bar"><div class="fso-bar-fill" style="width:${pct}%;background:${col}"></div></div>`;
      }).join('');
    }
    if(fsoDet&&dets.length){
      const ld=dets[0];
      const mbStr=ld.mb!=null?(ld.mb_local?'local':ld.mb_approx?`mb~${ld.mb.toFixed(1)}`:`mb=${ld.mb.toFixed(1)}`):'mb…';
      const usgsStr=ld.usgs?(()=>{const src=(ld.usgs.source||'usgs').toUpperCase();return `${src}: M${ld.usgs.mag} ${(ld.usgs.place||'').split(',')[0]}`;})():ld.usgs_checked?'no catalog match':'catalog pending';
      fsoDet.innerHTML=`<div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Latest Detection</div>
        <div style="color:#e6edf3">${fmtLocal(ld.ts)}</div>
        <div style="color:#58a6ff;margin:2px 0">${ld.stations.join(' · ')}</div>
        <div style="color:#d29922">${mbStr}</div>
        ${ld.epicenter?`<div style="color:#d29922">${ld.epicenter[0].toFixed(2)}N ${ld.epicenter[1].toFixed(2)}E</div>`:''}
        <div style="color:#a371f7;margin-top:2px">${usgsStr}</div>`;
    }
  }).catch(()=>{document.getElementById('status-dot').style.background='#f85149'});
}
update();setInterval(update,3000);
</script>
</body>
</html>"""


def start_web_server():
    if WEB_PORT == 0:
        return
    try:
        from flask import Flask, jsonify
    except ImportError:
        print("flask not installed — web UI disabled (pip install flask)", flush=True)
        return

    coords_json = json.dumps({k: list(v) for k, v in station_coords.items()})
    sta_list = ', '.join(f"{n}.{s}" for n, s in STATIONS)
    cfg_text = f"threshold {THRESHOLD} | {N_CONSENSUS}/{len(STATIONS)} consensus | {CONSENSUS_WINDOW:.0f}s window"
    app_title = f"{sta_list} | fra"
    html = (
        _WEB_HTML
        .replace('%(station_coords_json)s', coords_json)
        .replace('%(app_title)s',           app_title)
        .replace('%(cfg_text)s',            cfg_text)
        .replace('%(seedlink)s',            SEEDLINK_SERVER)
        .replace('%(threshold)s',           str(THRESHOLD))
        .replace('%(usgs_min_mag)s',        str(USGS_MIN_MAG))
        .replace('%(emsc_min_mag)s',        str(EMSC_MIN_MAG))
        .replace('%(umami_id)s',            UMAMI_SITE_ID)
    )

    app = Flask(__name__)
    import logging
    from flask import request, Response
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    # Simple in-memory per-IP rate limiter: max 60 req/min on API endpoint
    _rate_buckets = {}
    _rate_lock = threading.Lock()
    _RATE_LIMIT = 60    # requests
    _RATE_WINDOW = 60.0  # seconds

    def _check_rate(ip):
        now = time.time()
        with _rate_lock:
            if ip not in _rate_buckets:
                _rate_buckets[ip] = []
            bucket = _rate_buckets[ip]
            # prune old entries
            _rate_buckets[ip] = [t for t in bucket if now - t < _RATE_WINDOW]
            if len(_rate_buckets[ip]) >= _RATE_LIMIT:
                return False
            _rate_buckets[ip].append(now)
            # prune stale IPs periodically
            if len(_rate_buckets) > 500:
                cutoff = now - _RATE_WINDOW
                for k in list(_rate_buckets):
                    if all(t < cutoff for t in _rate_buckets[k]):
                        del _rate_buckets[k]
            return True

    @app.route('/')
    def index():
        return html

    @app.route('/health')
    def health():
        return Response('ok', status=200, mimetype='text/plain')

    @app.route('/api/state')
    def state():
        data = sensor_state.to_dict()
        data['station_coords'] = {k: list(v) for k, v in station_coords.items()}
        return jsonify(data)

    @app.route('/api/localize', methods=['POST'])
    def localize():
        """Compute epicenter from station arrival times.

        Body (JSON):
          {"arrivals": [["NET.STA", unix_ts], ...]}

        Returns:
          {"lat": float, "lon": float, "rms": float, "n": int}
          or {"error": "..."} on failure.
        """
        try:
            body = request.get_json(force=True)
            arrivals = [(str(k), float(t)) for k, t in body.get('arrivals', [])]
        except Exception as e:
            return jsonify({'error': f'bad request: {e}'}), 400
        if len(arrivals) < LOC_MIN_STA:
            return jsonify({'error': f'need at least {LOC_MIN_STA} arrivals, got {len(arrivals)}'}), 422
        result = locate_epicenter(arrivals)
        if result is None:
            return jsonify({'error': 'localization failed (optimizer did not converge)'}), 422
        lat, lon, rms = result
        return jsonify({'lat': round(lat, 4), 'lon': round(lon, 4), 'rms': round(rms, 3), 'n': len(arrivals)})

    # ── Slack slash command endpoint ───────────────────────────────────────────
    @app.route('/slack/command', methods=['POST'])
    def slack_command():
        import hashlib
        import hmac
        # Verify Slack request signature
        if SLACK_SIGNING_SECRET:
            ts = request.headers.get('X-Slack-Request-Timestamp', '')
            sig = request.headers.get('X-Slack-Signature', '')
            body_bytes = request.get_data()
            base = f'v0:{ts}:{body_bytes.decode()}'.encode()
            expected = 'v0=' + hmac.new(
                SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, sig):
                return Response('invalid signature', status=403, mimetype='text/plain')
        else:
            body_bytes = request.get_data()

        text = request.form.get('text', '').strip().lower()
        parts = text.split()
        sub = parts[0] if parts else 'status'
        now = time.time()

        if sub in ('status', ''):
            snap = sensor_state.to_dict()
            uptime = int(now - SERVER_START_TIME)
            h, m = divmod(uptime // 60, 60)
            sta_lines = []
            for key, s in sorted(snap['stations'].items()):
                age = int(now - s['last_ts'])
                sta_lines.append(f"`{key}` conf={s['conf']:.3f} age={age}s")
            det_count = len(snap['detections'])
            last_det = snap['detections'][-1]['ts'] if snap['detections'] else 'none'
            sta_text = '*Stations:*\n' + '\n'.join(sta_lines) if sta_lines else '*Stations:* none active'
            blocks = [
                {'type': 'header', 'text': {'type': 'plain_text', 'text': '🌍 Seismic Sensor Status'}},
                {'type': 'section', 'fields': [
                    {'type': 'mrkdwn', 'text': f'*Uptime:* {h}h {m}m'},
                    {'type': 'mrkdwn', 'text': f'*Detections:* {det_count} total'},
                    {'type': 'mrkdwn', 'text': f'*Last detection:* {last_det}'},
                    {'type': 'mrkdwn', 'text': f'*Threshold:* {THRESHOLD}'},
                ]},
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': sta_text}},
            ]
            return jsonify({'response_type': 'in_channel', 'blocks': blocks})

        elif sub == 'recent':
            n = 5
            if len(parts) > 1 and parts[1].isdigit():
                n = min(int(parts[1]), 20)
            dets = sensor_state.to_dict()['detections'][-n:]
            if not dets:
                return jsonify({'response_type': 'in_channel', 'text': 'No detections on record.'})
            lines = []
            for d in reversed(dets):
                mb_str = f"mb={d['mb']:.1f}" if d.get('mb') is not None else 'mb=?'
                epi_str = ''
                if d.get('epicenter'):
                    lat, lon = d['epicenter']
                    ns = 'N' if lat >= 0 else 'S'
                    ew = 'E' if lon >= 0 else 'W'
                    epi_str = f" | {abs(lat):.1f}°{ns} {abs(lon):.1f}°{ew}"
                usgs_str = ''
                if d.get('usgs'):
                    u = d['usgs']
                    usgs_str = f" → M{u['mag']} {u['place']}"
                lines.append(f"`{d['ts']}` {mb_str} conf={d['conf']:.3f}{epi_str}{usgs_str}")
            return jsonify({
                'response_type': 'in_channel',
                'text': f'*Last {len(dets)} detections:*\n' + '\n'.join(lines),
            })

        elif sub == 'usgs':
            # Show recent events from the sig-watcher's seen set
            import urllib.request as ureq
            try:
                url = (
                    f'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
                    f'&minmagnitude={USGS_SIG_MIN_MAG}'
                    f'&orderby=time-asc'
                    f'&starttime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))}'
                    f'&endtime={time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}'
                    f'&limit=10'
                )
                with ureq.urlopen(url, timeout=15) as resp:
                    data = json.loads(resp.read())
                feats = data.get('features', [])[-8:]
                if not feats:
                    return jsonify({
                        'response_type': 'in_channel',
                        'text': f'No M{USGS_SIG_MIN_MAG}+ events in past 24h.',
                    })
                lines = []
                for f in reversed(feats):
                    p = f['properties']
                    c = f['geometry']['coordinates']
                    ts = time.strftime('%H:%MZ', time.gmtime(p['time'] / 1000))
                    exp = _expected_p_arrival(c[1], c[0], p['time'] / 1000)
                    matched = _find_matching_detection(exp)
                    status = '✅' if matched else '❌'
                    lines.append(f"{status} `{ts}` M{p['mag']} {p.get('place', '?')}")
                return jsonify({
                    'response_type': 'in_channel',
                    'text': (
                        f'*M{USGS_SIG_MIN_MAG}+ events past 24h (✅=detected ❌=missed):*\n'
                        + '\n'.join(lines)
                    ),
                })
            except Exception as e:
                return jsonify({'response_type': 'ephemeral', 'text': f'USGS fetch failed: {e}'})

        elif sub == 'help':
            return jsonify({'response_type': 'ephemeral', 'text': (
                '*Seismic Sensor slash commands:*\n'
                '`/seismic status` — station health, uptime, detection count\n'
                '`/seismic recent [N]` — last N detections (default 5, max 20)\n'
                '`/seismic usgs` — M5.5+ events past 24h with detection status\n'
                '`/seismic help` — this message'
            )})

        else:
            return jsonify({
                'response_type': 'ephemeral',
                'text': f'Unknown subcommand `{sub}`. Try `/seismic help`.',
            })

    t = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=WEB_PORT, threaded=True),
        daemon=True,
        name='web-ui',
    )
    t.start()
    print(f"Web UI: http://0.0.0.0:{WEB_PORT}", flush=True)
