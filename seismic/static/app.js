const map = L.map('map', {zoomControl:false}).setView([45,10],2);
const _darkLayer=L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {attribution:'&copy; OSM &copy; CARTO',subdomains:'abcd',maxZoom:19}).addTo(map);
// Satellite layer — Mapbox satellite-streets when token present, ESRI fallback
const _mbToken=window.SEISMIC_CONFIG.mapboxToken;
let _satBase, _satLabels;
if(_mbToken){
  _satBase=L.tileLayer(
    `https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/256/{z}/{x}/{y}@2x?access_token=${_mbToken}`,
    {attribution:'&copy; <a href="https://www.mapbox.com/">Mapbox</a> &copy; OpenStreetMap',
     maxZoom:20,tileSize:256});
  _satLabels=null; // labels are baked into the satellite-streets style
} else {
  _satBase=L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxZoom:19});
  _satLabels=L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
    {attribution:'',maxZoom:19,opacity:0.85});
}
let _satOn=false;
const _satBtn=document.getElementById('sat-btn');
function _applySat(on){
  _satOn=on;
  document.body.classList.toggle('sat-mode',on);
  if(_satOn){
    map.removeLayer(_darkLayer);
    _satBase.addTo(map);
    if(_satLabels)_satLabels.addTo(map);
    _satBtn.style.color='#58a6ff';
    _satBtn.style.borderColor='#58a6ff';
  } else {
    if(map.hasLayer(_satBase))map.removeLayer(_satBase);
    if(_satLabels&&map.hasLayer(_satLabels))map.removeLayer(_satLabels);
    if(!map.hasLayer(_darkLayer))_darkLayer.addTo(map);
    _satBtn.style.color='#6e7681';
    _satBtn.style.borderColor='#30363d';
  }
}
_satBtn.addEventListener('click',()=>{_applySat(!_satOn);_saveSettings({satOn:_satOn});});
if(_loadSettings().satOn)_applySat(true);
const staMarkers={}, detMarkers=[];
let sCoords=window.SEISMIC_CONFIG.sCoords;
let lastFlyTs=null, lastFlyLat=null, lastFlyLon=null, selectedDetTs=null, _pulseIv=null, _pulsePhase=0;
let _lastDets=[];
let _pinnedMarker=null;
function _pinMarker(m){
  if(_pinnedMarker&&_pinnedMarker!==m)_pinnedMarker.closeTooltip();
  _pinnedMarker=m;
  m.openTooltip();
}
function _unpinMarker(){
  if(_pinnedMarker){_pinnedMarker.closeTooltip();_pinnedMarker=null;}
}
map.on('click',()=>_unpinMarker());
// Event log — polls /api/logs and renders backend stdout in the map overlay
const _logEl=document.getElementById('event-log');
let _logSeen=new Set();
function _classifyLog(msg){
  if(/CANDIDATE|CONSENSUS|DETECTED/.test(msg))return 'elog-det';
  if(/usgs|emsc|USGS|EMSC|M[0-9]\.[0-9].*—/.test(msg))return 'elog-usgs';
  if(/conf=|mag=|station/.test(msg))return 'elog-sta';
  return 'elog-info';
}
function _pollLogs(){
  fetch('/api/logs').then(r=>r.json()).then(d=>{
    const entries=d.entries||[];
    const atBottom=_logEl.scrollHeight-_logEl.scrollTop<=_logEl.clientHeight+4;
    // strip leading ISO timestamp bracket if present: "[2026-08-11T04:29:16Z] msg" → "msg"
    const clean=s=>s.replace(/^\[?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]?\s*/,'');
    _logEl.innerHTML=entries.slice(-60).map(e=>`<div class="elog ${_classifyLog(e.msg)}">${e.t} ${clean(e.msg)}</div>`).join('');
    if(atBottom)_logEl.scrollTop=_logEl.scrollHeight;
  }).catch(()=>{});
}
setInterval(_pollLogs,1500);
let filterConfirmed=false, filterMinMb=0, filterLocal=false, detDisplayLimit=20;
let staSortMode=_loadSettings().staSortMode||'name'; // 'name' | 'conf'
const _mbPendingNotify=new Set(); // ts values awaiting mb before firing desktop notification
// Persist settings to localStorage
const _SETTINGS_KEY='seismic_settings';
function _loadSettings(){
  try{return JSON.parse(localStorage.getItem(_SETTINGS_KEY)||'{}');}catch(e){return {};}
}
function _saveSettings(patch){
  try{
    const s=_loadSettings();
    localStorage.setItem(_SETTINGS_KEY,JSON.stringify(Object.assign(s,patch)));
  }catch(e){}
}
// Apply persisted settings (URL params override below if present)
(()=>{
  const s=_loadSettings();
  if(s.filterConfirmed!=null)filterConfirmed=!!s.filterConfirmed;
  if(s.filterMinMb!=null)filterMinMb=parseFloat(s.filterMinMb)||0;
  if(s.filterLocal!=null)filterLocal=!!s.filterLocal;
})();
// Read initial state from URL params
const _deepLinkTs=(()=>{
  const p=new URLSearchParams(window.location.search);
  const det=p.get('det');
  const hasFilterParams=p.has('conf')||p.has('mb')||p.has('local');
  if(p.has('conf'))filterConfirmed=p.get('conf')==='1';
  if(p.has('mb'))filterMinMb=parseFloat(p.get('mb'))||0;
  if(p.has('local'))filterLocal=p.get('local')==='1';
  if(det){
    // Slack deep link (no filter params) → clear all filters so target is always visible
    if(!hasFilterParams){filterConfirmed=false;filterMinMb=0;filterLocal=false;}
    detDisplayLimit=500;
    return parseFloat(det);
  }
  return null;
})();
// Push current filter state into the URL (preserves shareable view)
function _syncFiltersToUrl(){
  const p=new URLSearchParams(window.location.search);
  if(filterConfirmed){p.set('conf','1');}else{p.delete('conf');}
  if(filterMinMb>0){p.set('mb',filterMinMb.toFixed(1));}else{p.delete('mb');}
  if(filterLocal){p.set('local','1');}else{p.delete('local');}
  history.replaceState(null,'',window.location.pathname+(p.toString()?'?'+p.toString():''));
}
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
// Load faults on by default
_faultsBtn.click();
function showMoreDets(){detDisplayLimit+=50;}
(()=>{
  const staSortBtn=document.getElementById('sta-sort-btn');
  function _applyStaSort(){
    staSortBtn.textContent=staSortMode==='name'?'A→Z':'conf↓';
    staSortBtn.style.color=staSortMode==='conf'?'#d29922':'#6e7681';
    staSortBtn.style.borderColor=staSortMode==='conf'?'#d29922':'#30363d';
  }
  _applyStaSort();
  if(staSortBtn) staSortBtn.addEventListener('click',()=>{
    staSortMode=staSortMode==='name'?'conf':'name';
    _applyStaSort();
    _saveSettings({staSortMode});
    update();
  });
  const btn=document.getElementById('filter-btn');
  if(btn) btn.addEventListener('click',()=>{
    filterConfirmed=!filterConfirmed;
    detDisplayLimit=100;
    btn.style.color=filterConfirmed?'#3fb950':'#6e7681';
    btn.style.borderColor=filterConfirmed?'#3fb950':'#30363d';
    _syncFiltersToUrl();_saveSettings({filterConfirmed});update();
  });
  const localBtn=document.getElementById('filter-local-btn');
  if(localBtn) localBtn.addEventListener('click',()=>{
    filterLocal=!filterLocal;
    detDisplayLimit=100;
    localBtn.style.color=filterLocal?'#d29922':'#6e7681';
    localBtn.style.borderColor=filterLocal?'#d29922':'#30363d';
    _syncFiltersToUrl();_saveSettings({filterLocal});update();
  });
  const mbSel=document.getElementById('mb-filter-sel');
  if(mbSel) mbSel.addEventListener('change',()=>{
    filterMinMb=parseFloat(mbSel.value)||0;
    detDisplayLimit=100;
    mbSel.style.color=filterMinMb>0?'#58a6ff':'#8b949e';
    mbSel.style.borderColor=filterMinMb>0?'#58a6ff':'#30363d';
    _syncFiltersToUrl();_saveSettings({filterMinMb});update();
  });
  // Apply initial visual state to match actual filter values (possibly from URL params)
  if(btn){btn.style.color=filterConfirmed?'#3fb950':'#6e7681';btn.style.borderColor=filterConfirmed?'#3fb950':'#30363d';}
  if(localBtn){localBtn.style.color=filterLocal?'#d29922':'#6e7681';localBtn.style.borderColor=filterLocal?'#d29922':'#30363d';}
  if(mbSel){if(filterMinMb>0){mbSel.value=filterMinMb.toFixed(1);}else{mbSel.selectedIndex=0;}mbSel.style.color=filterMinMb>0?'#58a6ff':'#8b949e';mbSel.style.borderColor=filterMinMb>0?'#58a6ff':'#30363d';}
})();
function confColor(c){return c>=0.835?'#3fb950':c>=0.5?'#d29922':'#6e7681'}
// Server clock offset (seconds; positive = server ahead of browser)
let _serverClockOffset=0;
function _serverNow(){return Date.now()/1000+_serverClockOffset;}
function fmtAge(ts){
  const s=Math.round(_serverNow()-ts);
  if(s<0)return '—';
  if(s<60)return s+'s';
  if(s<3600)return Math.round(s/60)+'m';
  if(s<86400)return Math.round(s/3600)+'h';
  return Math.round(s/86400)+'d';
}
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
  sel.addEventListener('change',()=>{
    _userTz=sel.value;localStorage.setItem('tz',_userTz);
    detMarkers.forEach(({m})=>map.removeLayer(m));
    detMarkers.length=0;
    update();
  });
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
// Arrow keys navigate between detection rows; Esc exits CSS fullscreen
function _navDet(dir){
  const rows=[...document.querySelectorAll('.det[data-ts]')];
  if(!rows.length)return;
  const idx=rows.findIndex(r=>r.dataset.ts===selectedDetTs);
  const next=rows[idx<0?(dir>0?0:rows.length-1):Math.max(0,Math.min(rows.length-1,idx+dir))];
  if(!next||next.dataset.ts===selectedDetTs)return;
  next.click();
  if(!next.onclick)next.scrollIntoView({behavior:'smooth',block:'center'});
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&!document.fullscreenElement){_applyFsMode(false);return;}
  if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'||e.target.isContentEditable)return;
  if(e.key==='ArrowLeft'){e.preventDefault();_navDet(-1);}
  else if(e.key==='ArrowRight'){e.preventDefault();_navDet(1);}
  else if(e.key==='s'){_satBtn.click();}
});
let _pulseTs=null;
// ── Epicenter visualization: station lines + P-wave ring ─────────────────────
let _epiLines=[], _pWaveCircle=null, _pWaveRingIv=null;
function _clearEpiViz(){
  _epiLines.forEach(l=>map.removeLayer(l));
  _epiLines=[];
  if(_pWaveRingIv){clearInterval(_pWaveRingIv);_pWaveRingIv=null;}
  if(_pWaveCircle){map.removeLayer(_pWaveCircle);_pWaveCircle=null;}
}
function _drawEpiViz(det,epicLat,epicLon){
  _clearEpiViz();
  if(!det)return;
  const pVel=(window.SEISMIC_CONFIG.pVelKmS||8.0)*1000; // m/s
  // Lines from each firing station to epicenter
  (det.stations||[]).forEach(key=>{
    const coord=sCoords[key];
    if(!coord)return;
    const line=L.polyline([[coord[0],coord[1]],[epicLat,epicLon]],{
      color:'#58a6ff',weight:1.5,opacity:0.55,dashArray:'6,4',interactive:false
    }).addTo(map);
    _epiLines.push(line);
  });
  // Expanding P-wave ring anchored to detection time
  const initR=Math.max(0,(_serverNow()-det.unix_ts)*pVel);
  _pWaveCircle=L.circle([epicLat,epicLon],{
    radius:initR,color:'#f85149',weight:1.5,
    fillOpacity:0,opacity:0.75,interactive:false
  }).addTo(map);
  _pWaveRingIv=setInterval(()=>{
    if(!_pWaveCircle)return;
    const r=Math.max(0,(_serverNow()-det.unix_ts)*pVel);
    _pWaveCircle.setRadius(r);
    // Fade as ring expands past ~5000 km
    const fade=Math.max(0,0.75-(r/5000000)*0.65);
    _pWaveCircle.setStyle({opacity:fade});
    if(r>20100000){clearInterval(_pWaveRingIv);_pWaveRingIv=null;
      if(_pWaveCircle){map.removeLayer(_pWaveCircle);_pWaveCircle=null;}}
  },100);
}
// Station card hover — pulse the map marker
let _staHoverIv=null, _staHoverKey=null, _staHoverPhase=0;
function _staHoverIn(key){
  if(_staHoverIv){clearInterval(_staHoverIv);_staHoverIv=null;}
  const m=staMarkers[key];
  if(!m)return;
  _staHoverKey=key;
  _staHoverPhase=0;
  m.openTooltip();
  _staHoverIv=setInterval(()=>{
    _staHoverPhase=(_staHoverPhase+0.18)%(2*Math.PI);
    const p=Math.abs(Math.sin(_staHoverPhase));
    m.setRadius(4+p*10);
    m.setStyle({fillOpacity:0.55+p*0.45,weight:1+p*2.5});
  },40);
}
function _staHoverOut(key){
  if(_staHoverIv){clearInterval(_staHoverIv);_staHoverIv=null;}
  _staHoverKey=null;
  const m=staMarkers[key];
  if(!m)return;
  m.closeTooltip();
  m.setRadius(4);
  m.setStyle({fillOpacity:.9,weight:1});
}
// Zoom-scaled radius: full size at zoom ≥ 8, progressively smaller below that.
function zoomR(base){
  const z=map.getZoom();
  const f=Math.max(0.35,Math.min(1.0,(z-1)/7));
  return Math.max(2,base*f);
}
function _markerBaseStyle(usgs,dimmed){
  // usgs-corrected = purple; sensor-only = red dashed (unreliable location)
  if(usgs) return {color:'#7048aa',weight:1.5,fillColor:'#a371f7',fillOpacity:dimmed?.25:.85,dashArray:null};
  return {color:'#6e3010',weight:1,fillColor:'#f85149',fillOpacity:dimmed?.15:.5,dashArray:'5,3'};
}
function applyMarkerSelection(skipPulse){
  detMarkers.forEach(({m,ts,r,usgs})=>{
    if(ts===selectedDetTs){
      m.setStyle({color:'#2ea043',weight:1.5,fillColor:'#3fb950',fillOpacity:.95,dashArray:null});
      if(!skipPulse&&_pulseTs!==selectedDetTs){
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
      m.setStyle(_markerBaseStyle(usgs,!!selectedDetTs));
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
  if(ts){const _det=_lastDets.find(d=>d.ts===ts);_drawEpiViz(_det,lat,lon);}
  else{_clearEpiViz();}
  applyRowSelection();
  // Scroll selected row into view in detections panel
  if(ts){
    const row=document.querySelector(`.det[data-ts="${CSS.escape(ts)}"]`);
    if(row)row.scrollIntoView({behavior:'smooth',block:'nearest'});
  }
  // Stop pulse before flight — setRadius during flyTo fights Leaflet's SVG transform
  if(_pulseIv){clearInterval(_pulseIv);_pulseIv=null;_pulseTs=null;}
  applyMarkerSelection(true);  // style only — no pulse until map settles
  map.flyTo([lat,lon],5,{duration:1.0,easeLinearity:0.5});
  map.once('moveend',()=>{applyMarkerSelection();});  // now start pulse
  // Update URL so this view is bookmarkable/shareable (includes filter state)
  const row=ts?document.querySelector(`.det[data-ts="${CSS.escape(ts)}"]`):null;
  const unixTs=row?row.dataset.unixTs:null;
  const params=new URLSearchParams(window.location.search);
  if(unixTs){params.set('det',Math.round(unixTs));}else{params.delete('det');}
  if(filterConfirmed){params.set('conf','1');}else{params.delete('conf');}
  if(filterMinMb>0){params.set('mb',String(filterMinMb));}else{params.delete('mb');}
  if(filterLocal){params.set('local','1');}else{params.delete('local');}
  history.replaceState(null,'',window.location.pathname+(params.toString()?'?'+params.toString():''));
}
// audio alert
const _initS=_loadSettings();
let audioEnabled=_initS.audioEnabled!=null?!!_initS.audioEnabled:true;
let desktopNotifEnabled=false; // never auto-enable on load (requires user gesture for permission)
let notifMinMb=_initS.notifMinMb!=null?parseFloat(_initS.notifMinMb)||0:0;
let lastDetTs=null;
let audioCtx=null;
const muteBtn=document.getElementById('mute-btn');
muteBtn.textContent=audioEnabled?'🔔 on':'🔕 off';
muteBtn.style.color=audioEnabled?'#8b949e':'#6e7681';
muteBtn.addEventListener('click',()=>{
  audioEnabled=!audioEnabled;
  muteBtn.textContent=audioEnabled?'🔔 on':'🔕 off';
  muteBtn.style.color=audioEnabled?'#8b949e':'#6e7681';
  if(!audioCtx)audioCtx=new(window.AudioContext||window.webkitAudioContext)();
  _saveSettings({audioEnabled});
});
const notifBtn=document.getElementById('notif-btn');
notifBtn.addEventListener('click',async()=>{
  if(!desktopNotifEnabled){
    if(!('Notification' in window)){return;}
    let perm=Notification.permission;
    if(perm==='default')perm=await Notification.requestPermission();
    if(perm!=='granted'){notifBtn.title='Browser blocked notifications';return;}
    desktopNotifEnabled=true;
    notifBtn.style.color='#58a6ff';
    notifBtn.style.borderColor='#58a6ff';
  } else {
    desktopNotifEnabled=false;
    notifBtn.style.color='#6e7681';
    notifBtn.style.borderColor='#30363d';
  }
});
const notifMbSel=document.getElementById('notif-mb-sel');
if(notifMinMb>0){
  notifMbSel.value=notifMinMb.toFixed(1);
  notifMbSel.style.color='#58a6ff';
  notifMbSel.style.borderColor='#58a6ff';
}
notifMbSel.addEventListener('change',()=>{
  notifMinMb=parseFloat(notifMbSel.value)||0;
  notifMbSel.style.color=notifMinMb>0?'#58a6ff':'#6e7681';
  notifMbSel.style.borderColor=notifMinMb>0?'#58a6ff':'#30363d';
  _saveSettings({notifMinMb});
});
function _notifMbOk(mb){return notifMinMb===0||(mb!=null&&mb>=notifMinMb);}
function playDetectionAlert(mb){
  if(!audioEnabled||!_notifMbOk(mb))return;
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
function showDesktopNotification(det){
  if(!desktopNotifEnabled||!('Notification' in window)||Notification.permission!=='granted')return;
  if(!_notifMbOk(det.mb))return;
  const mbStr=det.mb_local?'local':det.mb_approx?`mb~${det.mb.toFixed(1)}`:`mb=${det.mb.toFixed(1)}`;
  const epiStr=det.epicenter?` · ${det.epicenter[0].toFixed(1)}°${det.epicenter[0]>=0?'N':'S'} ${Math.abs(det.epicenter[1]).toFixed(1)}°${det.epicenter[1]>=0?'E':'W'}`:'';
  new Notification('🌍 Seismic Detection',{
    body:`${det.stations.join(' · ')}\n${mbStr}${epiStr}`,
    tag:'seismic-det',
    renotify:true,
    silent:true,
  });
}
function update(){
  fetch('/api/state').then(r=>{
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  }).then(d=>{
    document.getElementById('status-dot').style.background='';
    try{_updateBody(d);}catch(e){console.error('[seismic] update error:',e);}
  }).catch(()=>{document.getElementById('status-dot').style.background='#f85149'});
}
// ── Replay tick marks ────────────────────────────────────────────────────────
function _updateReplayTicks(dets){
  const el=document.getElementById('replay-ticks');
  if(!el)return;
  const sorted=[...dets].sort((a,b)=>a.unix_ts-b.unix_ts);
  const n=sorted.length;
  if(n<2){el.innerHTML='';return;}
  const minT=sorted[0].unix_ts, maxT=sorted[n-1].unix_ts, span=maxT-minT||1;
  const frag=sorted.map(det=>{
    const pct=((det.unix_ts-minT)/span*100).toFixed(2);
    const color=det.usgs?'#a371f7':det.epicenter?'#58a6ff':'#4e545c';
    return `<div style="position:absolute;left:${pct}%;top:0;width:1px;height:6px;background:${color};transform:translateX(-50%)"></div>`;
  }).join('');
  el.innerHTML=frag;
}
// ── Detection replay ─────────────────────────────────────────────────────────
let _replayActive=false,_replayTs=null,_replayInterval=null;
const _REPLAY_DWELL_MS=2500;  // how long to show tooltip after camera settles
function _replayStop(){
  if(_replayInterval){clearTimeout(_replayInterval);_replayInterval=null;}
  _replayActive=false;
  _unpinMarker();
  const btn=document.getElementById('replay-btn');
  if(btn)btn.textContent='▶ replay';
}
function _replayStart(dets){
  if(!dets||!dets.length)return;
  _replayStop();
  // Only replay detections that have a visible map marker (localized, non-teleseismic)
  const sorted=[...dets]
    .filter(d=>!d.teleseismic&&(d.epicenter||(d.usgs&&d.usgs.lat!=null)))
    .sort((a,b)=>a.unix_ts-b.unix_ts);
  if(!sorted.length)return;
  let idx=0;
  _replayActive=true;
  const btn=document.getElementById('replay-btn');
  if(btn)btn.textContent='⏹ stop';
  const scrub=document.getElementById('replay-scrub');
  if(scrub)scrub.max=Math.max(0,sorted.length-1);
  function _step(){
    if(!_replayActive)return;
    if(idx>=sorted.length){_replayStop();return;}
    const det=sorted[idx++];
    const la=det.usgs&&det.usgs.lat!=null?det.usgs.lat:det.epicenter[0];
    const lo=det.usgs&&det.usgs.lon!=null?det.usgs.lon:det.epicenter[1];
    if(scrub){scrub.value=idx-1;scrub.title=det.ts;}
    flyToEpi(la,lo,det.ts);
    // After camera settles, pin the tooltip, dwell, then advance
    map.once('moveend',()=>{
      if(!_replayActive)return;
      // Find and pin the marker for this detection
      const entry=detMarkers.find(x=>x.ts===det.ts);
      if(entry)_pinMarker(entry.m);
      _replayInterval=setTimeout(()=>{
        _unpinMarker();
        _step();
      },_REPLAY_DWELL_MS);
    });
  }
  _step();
}
// Replay button is wired per-update in _updateBody once filteredDets is available.

function _updateBody(d){
    if(d.detections)_lastDets=[...d.detections];
    const _now=new Date();
    const _localStr=_now.toLocaleTimeString('en',{timeZone:_activeTz(),hour:'2-digit',minute:'2-digit',second:'2-digit'})+' '+_tzAbbr();
    const _utcStr=_now.toLocaleTimeString('en',{timeZone:'UTC',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})+' UTC';
    document.getElementById('last-update').textContent='updated '+_localStr+' ('+_utcStr+')';
    // Sync server clock offset; show skew warning if >30s
    if(d.now){
      const newOff=d.now-Date.now()/1000;
      _serverClockOffset=newOff;
      const skewEl=document.getElementById('clock-skew-banner');
      if(skewEl){
        if(Math.abs(newOff)>120){
          skewEl.style.display='block';
          skewEl.textContent=`⚠ Clock skew: server is ${newOff>0?'+':''}${Math.round(newOff)}s vs browser — check your local clock`;
        } else {
          skewEl.style.display='none';
        }
      }
    }
    if(d.station_coords)Object.assign(sCoords,d.station_coords);
    // stations
    const sDiv=document.getElementById('stations');
    let sHtml='';
    const _staSorted=Object.entries(d.stations).sort((a,b)=>staSortMode==='name'?a[0].localeCompare(b[0]):b[1].conf-a[1].conf);
    _staSorted.forEach(([k,s])=>{
      const pct=Math.round(s.conf*100);
      const col=confColor(s.conf);
      const coord=sCoords[k]?`${sCoords[k][0].toFixed(2)}°N ${sCoords[k][1].toFixed(2)}°E`:'coords unknown';
      const cardTitle=`${k}\n${coord}\nconf: ${s.conf.toFixed(4)}\nlast sample: ${fmtLocal(new Date(s.last_ts*1000).toISOString())}`;
      const barTitle=`threshold: ${window.SEISMIC_CONFIG.threshold} | current: ${s.conf.toFixed(3)}`;
      // Sparkline from conf_history
      const hist=s.conf_history||[];
      let sparkSvg='';
      if(hist.length>1){
        const W=200,H=24,thr=window.SEISMIC_CONFIG.threshold;
        const pts=hist.slice(-120);
        const mn=0,mx=Math.max(1,Math.max(...pts));
        const xs=i=>((i/(pts.length-1))*W).toFixed(1);
        const ys=v=>(H-1-(v-mn)/(mx-mn)*(H-1)).toFixed(1);
        const polyline=pts.map((v,i)=>`${xs(i)},${ys(v)}`).join(' ');
        const thrY=ys(thr);
        sparkSvg=`<div class="spark-wrap"><svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`
          +`<line x1="0" y1="${thrY}" x2="${W}" y2="${thrY}" stroke="#d29922" stroke-width="0.7" stroke-dasharray="3,2"/>`
          +`<polyline points="${polyline}" fill="none" stroke="${col}" stroke-width="1.2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`
          +`</svg><div class="spark-cursor"></div></div>`;
      }
      const flatline=s.flatline||false;
      sHtml+=`<div class="station" title="${cardTitle}${flatline?'\n⚠ Flatline — zero variance, feed may be dead':''}" style="${flatline?'opacity:.55;border-left:2px solid #f85149':''}" onmouseenter="_staHoverIn('${k}')" onmouseleave="_staHoverOut('${k}')">
        <div class="sta-row"><span class="sta-name">${k}</span>
        ${flatline?'<span style="color:#f85149;font-size:9px;letter-spacing:.5px">FLAT</span>':''}
        <span class="sta-conf" style="color:${flatline?'#f85149':col}">${s.conf.toFixed(3)}</span></div>
        <div class="conf-bar" title="${barTitle}"><div class="conf-fill" style="width:${pct}%;background:${flatline?'#f85149':col}"></div></div>
        ${sparkSvg}
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
    // Mirror station HTML into mobile tab panel
    const _mobSta=document.getElementById('mobile-sta-panel');
    if(_mobSta&&_mobSta.innerHTML!==sHtml)_mobSta.innerHTML=sHtml;
    // detections
    const dDiv=document.getElementById('detections');
    const dets=[...d.detections].reverse();
    let filteredDets=dets;
    if(filterConfirmed) filteredDets=filteredDets.filter(det=>det.usgs);
    if(filterLocal) filteredDets=filteredDets.filter(det=>det.epicenter&&!det.teleseismic);
    if(filterMinMb>0) filteredDets=filteredDets.filter(det=>det.mb!=null&&det.mb>=filterMinMb);
    const cntEl=document.getElementById('det-count');
    const activeFilters=filterConfirmed||filterLocal||filterMinMb>0;
    if(cntEl)cntEl.textContent=activeFilters
      ?`${filteredDets.length} / ${d.detections.length}`
      :(d.detections.length?`${d.detections.length} total`:'');
    // alert + log on new detection
    if(dets.length){
      const newest=dets[0];
      if(lastDetTs!==null && newest.ts!==lastDetTs){
        playDetectionAlert(newest.mb);
        if(newest.mb!=null){showDesktopNotification(newest);}
        else{_mbPendingNotify.add(newest.ts);}
      }
      lastDetTs=newest.ts;
      // fire deferred desktop notifications when mb arrives
      dets.forEach(det=>{
        if(_mbPendingNotify.has(det.ts)&&det.mb!=null){
          _mbPendingNotify.delete(det.ts);
          showDesktopNotification(det);
        }
      });
    }
    // last-event summary in header
    const sumEl=document.getElementById('last-event-summary');
    if(sumEl&&dets.length){
      const ld=dets[0];
      const mbStr=ld.mb!=null?(ld.mb_local?'local':ld.mb_approx?`mb~${ld.mb.toFixed(1)}`:`mb=${ld.mb.toFixed(1)}`):'mb…';
      const age=fmtAge(ld.unix_ts);
      sumEl.textContent=`Last: ${mbStr} · ${age==='—'?'future?':age} ago`;
    }
    // Wire replay controls to current filtered det list
    const _replayBtn=document.getElementById('replay-btn');
    if(_replayBtn&&!_replayBtn._wired){
      _replayBtn._wired=true;
      _replayBtn.addEventListener('click',()=>{
        if(_replayActive){_replayStop();return;}
        _replayStart(filteredDets);
      });
    }
    const _scrub=document.getElementById('replay-scrub');
    if(_scrub&&!_scrub._wired){
      _scrub._wired=true;
      _scrub.addEventListener('input',()=>{
        const sorted=[...filteredDets].sort((a,b)=>a.unix_ts-b.unix_ts);
        const det=sorted[parseInt(_scrub.value)];
        if(!det)return;
        selectedDetTs=det.ts;
        applyRowSelection();
        const row=document.querySelector(`.det[data-ts="${CSS.escape(det.ts)}"]`);
        if(row)row.scrollIntoView({behavior:'smooth',block:'center'});
        if(det.epicenter||(det.usgs&&det.usgs.lat!=null)){
          const la=det.usgs&&det.usgs.lat!=null?det.usgs.lat:det.epicenter[0];
          const lo=det.usgs&&det.usgs.lon!=null?det.usgs.lon:det.epicenter[1];
          map.flyTo([la,lo],5,{duration:0.6,easeLinearity:0.5});
          map.once('moveend',()=>applyMarkerSelection());
        }
      });
    }
    if(_scrub){_scrub.max=Math.max(0,filteredDets.length-1);_scrub.value=_scrub.max;}
    _updateReplayTicks(filteredDets);
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
        usgsTitle=`No match in USGS (M${window.SEISMIC_CONFIG.usgsMag}+) or EMSC (M${window.SEISMIC_CONFIG.emscMag}+) for this window`;
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
        +(det.usgs?(()=>{const src=(det.usgs.source||'usgs').toUpperCase();return `\n${src}: M${det.usgs.mag}${magType} — ${place}`;})():det.usgs_checked?`\nNo catalog match (USGS M${window.SEISMIC_CONFIG.usgsMag}+ / EMSC M${window.SEISMIC_CONFIG.emscMag}+)`:`\nCatalog lookup pending`);
      const selCls=det.ts===selectedDetTs?' det-selected':'';
      const pinLat=det.usgs&&det.usgs.lat!=null?det.usgs.lat:det.epicenter?det.epicenter[0]:null;
      const pinLon=det.usgs&&det.usgs.lon!=null?det.usgs.lon:det.epicenter?det.epicenter[1]:null;
      const canClick=!det.teleseismic&&pinLat!=null;
      const mutedCls=canClick?'':' det-muted';
      const verifCls=(det.usgs&&canClick)?' det-verified':'';
      const rowClick=canClick
        ?`onclick="flyToEpi(${pinLat},${pinLon},'${det.ts}')" style="cursor:pointer"`:'';
      // btcvm anchor icon — check batch index first (on-chain), then ledger-only
      const _btcUnixKey=det.unix_ts!=null?det.unix_ts.toFixed(3):null;
      const _btcLedger=_btcUnixKey?_btcvmByUnix[_btcUnixKey]:null;
      const _btcBatch=_btcLedger?_btcvmBatchByHash[_btcLedger.det_hash]:null;
      let btcIcon='';
      if(_btcBatch){
        // on-chain via daily Merkle batch
        const tip=`On-chain (daily batch) · block ${_btcBatch.block_height}\nmerkle root: ${_btcBatch.merkle_root||''}\ntx: ${_btcBatch.tx_hash}\n${_btcBatch.n} detections in batch`;
        btcIcon=`<a class="det-usgs-icon" href="https://blockstream.info/tx/${encodeURIComponent(_btcBatch.tx_hash)}" target="_blank" rel="noopener" title="${escAttr(tip)}" style="text-decoration:none;color:#f7931a" onclick="event.stopPropagation()">₿</a>`;
      } else if(_btcLedger&&_btcLedger.tx_hash){
        // v1 individual broadcast (legacy — on-chain but pre-batch scheme)
        const tip=`On-chain (v1 individual) · block ${_btcLedger.block_height}\ntx: ${_btcLedger.tx_hash}`;
        btcIcon=`<a class="det-usgs-icon" href="https://blockstream.info/tx/${encodeURIComponent(_btcLedger.tx_hash)}" target="_blank" rel="noopener" title="${escAttr(tip)}" style="text-decoration:none;color:#f7931a;opacity:.65" onclick="event.stopPropagation()">₿</a>`;
      } else if(_btcLedger){
        // ledger-only (batch pending) — no tx yet, link to block for context
        const isConf=_btcLedger.label==='confirmed';
        const tip=`Ledger anchor (batch pending) · block ${_btcLedger.block_height}\ncommitment: ${_btcLedger.commitment||''}\n${isConf?'Catalog confirmed':'Raw detection'}`;
        const blockUrl=_btcLedger.block_hash?`https://blockstream.info/block/${encodeURIComponent(_btcLedger.block_hash)}`:'';
        btcIcon=blockUrl
          ?`<a class="det-usgs-icon" href="${blockUrl}" target="_blank" rel="noopener" title="${escAttr(tip)}" style="text-decoration:none;color:#a07830" onclick="event.stopPropagation()">₿</a>`
          :`<span class="det-usgs-icon" style="color:#a07830" title="${escAttr(tip)}">₿</span>`;
      }
      return sep+`<div class="det${selCls}${mutedCls}${verifCls}" data-ts="${det.ts}" data-unix-ts="${det.unix_ts}" ${rowClick} title="${escAttr(detTitle)}">
        <div class="det-row1"><span class="det-time">${tPart}</span><span class="det-age">${fmtAge(det.unix_ts)}</span></div>
        <div class="det-row2"><span class="det-stas">${det.stations.join(' · ')}</span><span class="det-chips-inline">${mbChip}${epiChip}${usgsIcon}${btcIcon}</span></div>
      </div>`;
    }).join('');
    if(dDiv.innerHTML!==newHtml)dDiv.innerHTML=newHtml;
    // epicenter markers — diff by ts; force-recreate when USGS coords arrive after initial placement
    const epiDets=d.detections.filter(det=>!det.teleseismic&&(det.epicenter||(det.usgs&&det.usgs.lat!=null)));
    const epiTsSet=new Set(epiDets.map(det=>det.ts));
    const keptTs=new Set();
    // remove stale markers or those whose coordinates have changed (watcher override)
    detMarkers.forEach(({m,ts,lat,lon})=>{
      const det=epiDets.find(d=>d.ts===ts);
      const usgsC=det&&det.usgs&&det.usgs.lat!=null;
      const newLat=usgsC?det.usgs.lat:det.epicenter?det.epicenter[0]:null;
      const newLon=usgsC?det.usgs.lon:det.epicenter?det.epicenter[1]:null;
      const moved=newLat!=null&&(Math.abs(newLat-lat)>0.5||Math.abs(newLon-lon)>0.5);
      if(!epiTsSet.has(ts)||moved){map.removeLayer(m);}
      else{keptTs.add(ts);}
    });
    const kept=detMarkers.filter(({ts})=>keptTs.has(ts));
    // add new markers
    let markersChanged=kept.length!==detMarkers.length;
    epiDets.forEach(det=>{
      if(keptTs.has(det.ts))return;
      markersChanged=true;
      const usgsCoords=det.usgs&&det.usgs.lat!=null;
      const la=usgsCoords?det.usgs.lat:det.epicenter[0];
      const lo=usgsCoords?det.usgs.lon:det.epicenter[1];
      const mb=det.mb||4;
      const r=Math.max(4,Math.min(14,(mb-2)*3+4));
      const mbLabel=det.mb?(det.mb_local?'local':det.mb_approx?'mb~'+det.mb.toFixed(1):'mb='+det.mb.toFixed(1)):'mb pending';
      const mbClass=mb>=5?'high':mb>=4?'mid':'low';
      const locSrc=usgsCoords?'USGS':'sensor';
      const locStr=`${locSrc}: ${Math.abs(la).toFixed(2)}°${la>=0?'N':'S'} ${Math.abs(lo).toFixed(2)}°${lo>=0?'E':'W'}`;
      const hasOrig=usgsCoords&&det.epicenter&&det.epicenter[0]!=null;
      const origLa=hasOrig?det.epicenter[0]:null;
      const origLo=hasOrig?det.epicenter[1]:null;
      const origStr=hasOrig?`${Math.abs(origLa).toFixed(2)}°${origLa>=0?'N':'S'} ${Math.abs(origLo).toFixed(2)}°${origLo>=0?'E':'W'}`:'';
      const origLink=hasOrig
        ?`<div class="tip-loc-orig"><a href="#" onclick="event.preventDefault();event.stopPropagation();map.flyTo([${origLa},${origLo}],5,{duration:1.0})">sensor: ${origStr}</a></div>`
        :'';
      const tipHtml=`<div class="det-tip">`
        +`<div class="tip-time">${fmtLocal(det.ts)} <span style="color:#6e7681">(${fmtAge(det.unix_ts)} ago)</span></div>`
        +`<div class="tip-mb ${mbClass}">${mbLabel}</div>`
        +`<div class="tip-stas">${det.stations.join(' · ')}</div>`
        +`<div class="tip-loc">${locStr}</div>`
        +origLink
        +'</div>';
      const m=L.circleMarker([la,lo],{radius:zoomR(r),..._markerBaseStyle(usgsCoords,false)})
        .bindTooltip(tipHtml,{sticky:false,direction:'top',className:'det-tip'}).addTo(map);
      m.on('click',(e)=>{
        L.DomEvent.stopPropagation(e);
        if(_pinnedMarker===m){_unpinMarker();}else{_pinMarker(m);}
        // Select and scroll to the matching detection row
        const entry=detMarkers.find(x=>x.m===m);
        if(entry){
          selectedDetTs=entry.ts;
          applyRowSelection();
          const row=document.querySelector(`.det[data-ts="${CSS.escape(entry.ts)}"]`);
          if(row)row.scrollIntoView({behavior:'smooth',block:'center'});
        }
      });
      m.on('mouseout',()=>{if(_pinnedMarker===m)m.openTooltip();});
      kept.push({m,ts:det.ts,r,lat:la,lon:lo,usgs:usgsCoords});
    });
    detMarkers.length=0;kept.forEach(x=>detMarkers.push(x));
    if(markersChanged)applyMarkerSelection();
    // flyTo newest localized detection when it first appears or when USGS corrects
    // sensor coords by >1° (skip if deep link active)
    const newestEpi=dets.find(det=>!det.teleseismic&&(det.epicenter||(det.usgs&&det.usgs.lat!=null)));
    if(!_deepLinkTs && newestEpi){
      const usgsC=newestEpi.usgs&&newestEpi.usgs.lat!=null;
      const la=usgsC?newestEpi.usgs.lat:newestEpi.epicenter[0];
      const lo=usgsC?newestEpi.usgs.lon:newestEpi.epicenter[1];
      const isNew=newestEpi.ts!==lastFlyTs;
      const moved=lastFlyLat!=null&&(Math.abs(la-lastFlyLat)>1||Math.abs(lo-lastFlyLon)>1);
      if(isNew||moved){
        lastFlyTs=newestEpi.ts; lastFlyLat=la; lastFlyLon=lo;
        selectedDetTs=newestEpi.ts;
        applyRowSelection();
        map.flyTo([la,lo],5,{duration:1.0,easeLinearity:0.5});
        map.once('moveend',()=>applyMarkerSelection());
        _drawEpiViz(newestEpi,la,lo);
      }
    }
    // fullscreen overlay: station list + latest detection
    const fsoSta=document.getElementById('fso-stations');
    const fsoDet=document.getElementById('fso-det');
    if(fsoSta){
      fsoSta.innerHTML=Object.entries(d.stations).sort((a,b)=>staSortMode==='name'?a[0].localeCompare(b[0]):b[1].conf-a[1].conf).map(([k,s])=>{
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
}
// btcvm ledger — two indexes:
//   _btcvmByUnix: det_unix → best raw/confirmed entry (ledger-only anchor)
//   _btcvmBatchByHash: det_hash → batch entry (on-chain anchor)
let _btcvmByUnix={};
let _btcvmBatchByHash={};
function _pollBtcvm(){
  fetch('/api/btcvm').then(r=>r.json()).then(d=>{
    const byUnix={};
    const byHash={};
    (d.entries||[]).forEach(e=>{
      if(e.label==='batch'&&e.tx_hash&&Array.isArray(e.det_hashes)){
        e.det_hashes.forEach(h=>{byHash[h]=e;});
      } else if(e.det_unix!=null){
        const k=e.det_unix.toFixed(3);
        if(!byUnix[k]||e.label==='confirmed')byUnix[k]=e;
      }
    });
    _btcvmByUnix=byUnix;
    _btcvmBatchByHash=byHash;
  }).catch(()=>{});
}
_pollBtcvm();setInterval(_pollBtcvm,15000);
// ── Mobile tab switcher ──────────────────────────────────────────────────────
function _mobTab(which, btn){
  document.querySelectorAll('.mob-tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  const dw=document.getElementById('detections-wrap');
  const sp=document.getElementById('mobile-sta-panel');
  if(which==='dets'){dw&&dw.classList.add('active');sp&&sp.classList.remove('active');}
  else{sp&&sp.classList.add('active');dw&&dw.classList.remove('active');}
}
// ── Stations panel resize handle ─────────────────────────────────────────────
(()=>{
  const handle=document.getElementById('sta-resize-handle');
  const grid=document.querySelector('.grid');
  if(!handle||!grid)return;
  const MIN_W=140, MAX_W=480;
  const saved=parseInt(localStorage.getItem('staW'))||220;
  grid.style.setProperty('--sta-w',saved+'px');
  let dragging=false, startX=0, startW=0;
  handle.addEventListener('mousedown',e=>{
    dragging=true;
    startX=e.clientX;
    startW=parseInt(getComputedStyle(grid).getPropertyValue('--sta-w'))||220;
    handle.classList.add('dragging');
    document.body.style.cursor='col-resize';
    document.body.style.userSelect='none';
    e.preventDefault();
  });
  document.addEventListener('mousemove',e=>{
    if(!dragging)return;
    const w=Math.max(MIN_W,Math.min(MAX_W,startW+(e.clientX-startX)));
    grid.style.setProperty('--sta-w',w+'px');
    map.invalidateSize();
  });
  document.addEventListener('mouseup',()=>{
    if(!dragging)return;
    dragging=false;
    handle.classList.remove('dragging');
    document.body.style.cursor='';
    document.body.style.userSelect='';
    const w=parseInt(getComputedStyle(grid).getPropertyValue('--sta-w'))||220;
    localStorage.setItem('staW',w);
  });
})();
// Synchronized sparkline crosshair
(()=>{
  const staPanel=document.getElementById('stations');
  if(!staPanel)return;
  staPanel.addEventListener('mousemove',e=>{
    document.querySelectorAll('.spark-wrap').forEach(wrap=>{
      const rect=wrap.getBoundingClientRect();
      if(!rect.width)return;
      const x=Math.max(0,Math.min(rect.width,e.clientX-rect.left));
      const c=wrap.querySelector('.spark-cursor');
      if(c){c.style.left=x+'px';c.style.display='block';}
    });
  });
  staPanel.addEventListener('mouseleave',()=>{
    document.querySelectorAll('.spark-cursor').forEach(c=>c.style.display='none');
  });
})();
update();setInterval(update,3000);
// Deep-link: ?det=<unix_ts> → highlight row, fly map to it, clear filters
(()=>{
  if(!_deepLinkTs)return;
  // Update filter button visuals to match cleared state
  const _dlInit=()=>{
    const btn=document.getElementById('filter-btn');
    const localBtn=document.getElementById('filter-local-btn');
    const mbSel=document.getElementById('mb-filter-sel');
    if(btn){btn.style.color='#6e7681';btn.style.borderColor='#30363d';}
    if(localBtn){localBtn.style.color='#6e7681';localBtn.style.borderColor='#30363d';}
    if(mbSel){mbSel.selectedIndex=0;mbSel.style.color='#8b949e';mbSel.style.borderColor='#30363d';}
  };
  setTimeout(_dlInit,100);
  let attempts=0;
  const tryHighlight=()=>{
    const rows=document.querySelectorAll('.det[data-unix-ts]');
    for(const row of rows){
      if(Math.abs((parseFloat(row.dataset.unixTs)||0)-_deepLinkTs)<2){
        row.scrollIntoView({behavior:'smooth',block:'center'});
        row.style.transition='outline .2s,box-shadow .2s';
        row.style.outline='2px solid #58a6ff';
        row.style.boxShadow='0 0 10px #58a6ff99';
        setTimeout(()=>{row.style.outline='';row.style.boxShadow='';},4000);
        // Fly map to this detection's epicenter if available
        const ts=row.dataset.ts;
        if(ts){
          const m=detMarkers.find(x=>x.ts===ts);
          if(m){const ll=m.m.getLatLng();map.flyTo([ll.lat,ll.lng],5,{duration:1.2,easeLinearity:0.5});}
        }
        return;
      }
    }
    if(++attempts<12)setTimeout(tryHighlight,600);
  };
  setTimeout(tryHighlight,800);
})();
