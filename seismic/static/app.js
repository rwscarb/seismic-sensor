// ── Module constants ──────────────────────────────────────────────────────────

const CONFIG = window.SEISMIC_CONFIG;
const SETTINGS_KEY = 'seismic_settings';
const FAULT_GEOJSON_URL = 'https://raw.githubusercontent.com/GEMScienceTools/gem-global-active-faults/master/geojson/gem_active_faults.geojson';
const REPLAY_DWELL_MS = 2500;
const FLY_ZOOM = 3;
const FLY_DURATION = 1.0;


// ── Persistent settings ────────────────────────────────────────────────────────

function loadSettings() {
    try {
        return JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    } catch (ignore) {
        return {};
    }
}

function saveSettings(patch) {
    try {
        const current = loadSettings();
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(Object.assign(current, patch)));
    } catch (ignore) {}
}


// ── Filter and display state ──────────────────────────────────────────────────

const _initSettings = loadSettings();
let filterConfirmed = !!_initSettings.filterConfirmed;
let filterMinMb = parseFloat(_initSettings.filterMinMb) || 0;
let filterLocal = !!_initSettings.filterLocal;
let detDisplayLimit = 20;
let staSortMode = _initSettings.staSortMode || 'name';
let _currentFilteredDets = [];   // updated each render; shared with replay controls


// ── URL parameters (override saved settings) ──────────────────────────────────

const _deepLinkTs = (function () {
    const params = new URLSearchParams(window.location.search);
    const det = params.get('det');
    const hasFilterParams = params.has('conf') || params.has('mb') || params.has('local');

    if (params.has('conf')) { filterConfirmed = params.get('conf') === '1'; }
    if (params.has('mb'))   { filterMinMb = parseFloat(params.get('mb')) || 0; }
    if (params.has('local')){ filterLocal = params.get('local') === '1'; }

    if (det) {
        if (!hasFilterParams) {
            filterConfirmed = false;
            filterMinMb = 0;
            filterLocal = false;
        }
        detDisplayLimit = 500;
        return parseFloat(det);
    }
    return null;
}());

function _syncFiltersToUrl() {
    const params = new URLSearchParams(window.location.search);
    if (filterConfirmed)  { params.set('conf', '1'); }   else { params.delete('conf'); }
    if (filterMinMb > 0)  { params.set('mb', filterMinMb.toFixed(1)); } else { params.delete('mb'); }
    if (filterLocal)      { params.set('local', '1'); }  else { params.delete('local'); }
    const qs = params.toString();
    history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
}


// ── Map setup ─────────────────────────────────────────────────────────────────

const map = L.map('map', { zoomControl: false }).setView([45, 10], 2);

const _darkLayer = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    { attribution: '&copy; OSM &copy; CARTO', subdomains: 'abcd', maxZoom: 19 }
).addTo(map);

const _mbToken = CONFIG.mapboxToken;
let _satBase, _satLabels;

if (_mbToken) {
    _satBase = L.tileLayer(
        'https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/256/{z}/{x}/{y}@2x?access_token=' + _mbToken,
        { attribution: '&copy; <a href="https://www.mapbox.com/">Mapbox</a> &copy; OpenStreetMap', maxZoom: 20, tileSize: 256 }
    );
    _satLabels = null;
} else {
    _satBase = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        { attribution: '&copy; Esri, Maxar, Earthstar Geographics', maxZoom: 19 }
    );
    _satLabels = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
        { attribution: '', maxZoom: 19, opacity: 0.85 }
    );
}

let _satOn = false;
const _satBtn = document.getElementById('sat-btn');

function _applySat(on) {
    _satOn = on;
    document.body.classList.toggle('sat-mode', on);

    if (_satOn) {
        map.removeLayer(_darkLayer);
        _satBase.addTo(map);
        if (_satLabels) { _satLabels.addTo(map); }
        _satBtn.style.color = '#58a6ff';
        _satBtn.style.borderColor = '#58a6ff';
    } else {
        if (map.hasLayer(_satBase)) { map.removeLayer(_satBase); }
        if (_satLabels && map.hasLayer(_satLabels)) { map.removeLayer(_satLabels); }
        if (!map.hasLayer(_darkLayer)) { _darkLayer.addTo(map); }
        _satBtn.style.color = '#6e7681';
        _satBtn.style.borderColor = '#30363d';
    }
}

_satBtn.addEventListener('click', function () {
    _applySat(!_satOn);
    saveSettings({ satOn: _satOn });
});

if (loadSettings().satOn) { _applySat(true); }


// ── Map state ─────────────────────────────────────────────────────────────────

const staMarkers = {};
const detMarkers = [];
let sCoords = CONFIG.sCoords;
let lastFlyTs = null, lastFlyLat = null, lastFlyLon = null;
let selectedDetTs = null;
let _pulseIv = null, _pulsePhase = 0, _pulseTs = null;
let _lastDets = [];
let _pinnedMarker = null;

function _pinMarker(m) {
    if (_pinnedMarker && _pinnedMarker !== m) { _pinnedMarker.closeTooltip(); }
    _pinnedMarker = m;
    m.openTooltip();
}

function _unpinMarker() {
    if (_pinnedMarker) {
        _pinnedMarker.closeTooltip();
        _pinnedMarker = null;
    }
}

map.on('click', _unpinMarker);


// ── Event log ─────────────────────────────────────────────────────────────────

const _logEl = document.getElementById('event-log');

function _classifyLog(msg) {
    if (/CANDIDATE|CONSENSUS|DETECTED/.test(msg)) { return 'elog-det'; }
    if (/usgs|emsc|USGS|EMSC|M[0-9]\.[0-9].*—/.test(msg)) { return 'elog-usgs'; }
    if (/conf=|mag=|station/.test(msg)) { return 'elog-sta'; }
    return 'elog-info';
}

function _pollLogs() {
    const stripTimestamp = function (s) {
        return s.replace(/^\[?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]?\s*/, '');
    };

    fetch('/api/logs').then(function (r) {
        return r.json();
    }).then(function (d) {
        const entries = d.entries || [];
        const atBottom = _logEl.scrollHeight - _logEl.scrollTop <= _logEl.clientHeight + 4;
        _logEl.innerHTML = entries.slice(-60).map(function (e) {
            return '<div class="elog ' + _classifyLog(e.msg) + '">' + e.t + ' ' + stripTimestamp(e.msg) + '</div>';
        }).join('');
        if (atBottom) { _logEl.scrollTop = _logEl.scrollHeight; }
    }).catch(function () {});
}

setInterval(_pollLogs, 1500);


// ── Fault overlay ─────────────────────────────────────────────────────────────

let _faultLayer = null, _faultLoading = false, _faultOn = false;
const _faultsBtn = document.getElementById('faults-btn');

function _setFaultBtnState() {
    _faultsBtn.style.color = _faultOn ? '#d29922' : '#6e7681';
    _faultsBtn.style.borderColor = _faultOn ? '#d29922' : '#30363d';
    _faultsBtn.textContent = _faultLoading ? '⟳ loading…' : (_faultOn ? '⚡ faults ✓' : '⚡ faults');
}

_faultsBtn.addEventListener('click', async function () {
    if (_faultLoading) { return; }
    _faultOn = !_faultOn;

    if (!_faultOn) {
        if (_faultLayer) { map.removeLayer(_faultLayer); }
        _setFaultBtnState();
        return;
    }

    if (_faultLayer) {
        _faultLayer.addTo(map);
        _setFaultBtnState();
        return;
    }

    _faultLoading = true;
    _setFaultBtnState();

    try {
        const r = await fetch(FAULT_GEOJSON_URL);
        const geojson = await r.json();
        _faultLayer = L.geoJSON(geojson, {
            style: { color: '#e36209', weight: 1, opacity: 0.45 },
            onEachFeature: function (f, layer) {
                const props = f.properties || {};
                const name = props.name || props.fault_name || props.FaultName || '';
                if (name) { layer.bindTooltip(name, { sticky: true, className: 'fault-tip' }); }
            }
        }).addTo(map);
    } catch (e) {
        _faultOn = false;
        alert('Failed to load fault data: ' + e.message);
    }

    _faultLoading = false;
    _setFaultBtnState();
});

_faultsBtn.click();


// ── Station sort + filter controls ────────────────────────────────────────────

(function () {
    const staSortBtn = document.getElementById('sta-sort-btn');

    function applyStaSort() {
        staSortBtn.textContent = staSortMode === 'name' ? 'A→Z' : 'conf↓';
        staSortBtn.style.color = staSortMode === 'conf' ? '#d29922' : '#6e7681';
        staSortBtn.style.borderColor = staSortMode === 'conf' ? '#d29922' : '#30363d';
    }

    applyStaSort();

    if (staSortBtn) {
        staSortBtn.addEventListener('click', function () {
            staSortMode = staSortMode === 'name' ? 'conf' : 'name';
            applyStaSort();
            saveSettings({ staSortMode });
            update();
        });
    }

    const confBtn = document.getElementById('filter-btn');
    if (confBtn) {
        confBtn.addEventListener('click', function () {
            filterConfirmed = !filterConfirmed;
            detDisplayLimit = 100;
            confBtn.style.color = filterConfirmed ? '#3fb950' : '#6e7681';
            confBtn.style.borderColor = filterConfirmed ? '#3fb950' : '#30363d';
            _syncFiltersToUrl();
            saveSettings({ filterConfirmed });
            update();
        });
    }

    const localBtn = document.getElementById('filter-local-btn');
    if (localBtn) {
        localBtn.addEventListener('click', function () {
            filterLocal = !filterLocal;
            detDisplayLimit = 100;
            localBtn.style.color = filterLocal ? '#d29922' : '#6e7681';
            localBtn.style.borderColor = filterLocal ? '#d29922' : '#30363d';
            _syncFiltersToUrl();
            saveSettings({ filterLocal });
            update();
        });
    }

    const mbSel = document.getElementById('mb-filter-sel');
    if (mbSel) {
        mbSel.addEventListener('change', function () {
            filterMinMb = parseFloat(mbSel.value) || 0;
            detDisplayLimit = 100;
            mbSel.style.color = filterMinMb > 0 ? '#58a6ff' : '#8b949e';
            mbSel.style.borderColor = filterMinMb > 0 ? '#58a6ff' : '#30363d';
            _syncFiltersToUrl();
            saveSettings({ filterMinMb });
            update();
        });
    }

    // Sync initial visual state to actual filter values
    if (confBtn) {
        confBtn.style.color = filterConfirmed ? '#3fb950' : '#6e7681';
        confBtn.style.borderColor = filterConfirmed ? '#3fb950' : '#30363d';
    }
    if (localBtn) {
        localBtn.style.color = filterLocal ? '#d29922' : '#6e7681';
        localBtn.style.borderColor = filterLocal ? '#d29922' : '#30363d';
    }
    if (mbSel) {
        if (filterMinMb > 0) { mbSel.value = filterMinMb.toFixed(1); } else { mbSel.selectedIndex = 0; }
        mbSel.style.color = filterMinMb > 0 ? '#58a6ff' : '#8b949e';
        mbSel.style.borderColor = filterMinMb > 0 ? '#58a6ff' : '#30363d';
    }
}());


// ── Utility functions ─────────────────────────────────────────────────────────

function confColor(c) {
    return c >= 0.835 ? '#3fb950' : c >= 0.5 ? '#d29922' : '#6e7681';
}

let _serverClockOffset = 0;

function _serverNow() {
    return Date.now() / 1000 + _serverClockOffset;
}

function fmtAge(ts) {
    const s = Math.round(_serverNow() - ts);
    if (s < 0)     { return '—'; }
    if (s < 60)    { return s + 's'; }
    if (s < 3600)  { return Math.round(s / 60) + 'm'; }
    if (s < 86400) { return Math.round(s / 3600) + 'h'; }
    return Math.round(s / 86400) + 'd';
}

const _browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
let _userTz = localStorage.getItem('tz') || 'auto';

function _activeTz() {
    return _userTz === 'auto' ? _browserTz : _userTz;
}

function _tzAbbr() {
    try {
        return new Intl.DateTimeFormat('en', { timeZone: _activeTz(), timeZoneName: 'short' })
            .formatToParts(new Date())
            .find(function (p) { return p.type === 'timeZoneName'; })
            .value;
    } catch (ignore) {
        return _activeTz();
    }
}

function fmtLocal(isoStr) {
    const d = new Date(isoStr);
    const tz = _activeTz();
    const timeStr = d.toLocaleTimeString('en', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: tz
    });
    const today  = new Date().toLocaleDateString('en', { timeZone: tz });
    const detDay = d.toLocaleDateString('en', { timeZone: tz });
    const prefix = today === detDay
        ? ''
        : d.toLocaleDateString('en', { timeZone: tz, month: 'short', day: 'numeric' }) + ' ';
    return prefix + timeStr + ' ' + _tzAbbr();
}


// ── Timezone selector ─────────────────────────────────────────────────────────

(function () {
    const sel = document.getElementById('tz-sel');
    if (!sel) { return; }
    sel.value = _userTz;
    if (!sel.value) { sel.value = 'auto'; }
    sel.addEventListener('change', function () {
        _userTz = sel.value;
        localStorage.setItem('tz', _userTz);
        detMarkers.forEach(function (entry) { map.removeLayer(entry.m); });
        detMarkers.length = 0;
        update();
    });
}());


// ── Fullscreen ────────────────────────────────────────────────────────────────

const _fsBtn = document.getElementById('fs-btn');

function _applyFsMode(on) {
    document.body.classList.toggle('fs-mode', on);
    _fsBtn.textContent = on ? '✕' : '⛶';
    _fsBtn.title = on ? 'Exit fullscreen' : 'Toggle fullscreen';
    setTimeout(function () { map.invalidateSize(); }, 150);
}

_fsBtn.addEventListener('click', function () {
    if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(function () {
            _applyFsMode(true);
        });
    } else {
        document.exitFullscreen();
    }
});

document.addEventListener('fullscreenchange', function () {
    _applyFsMode(!!document.fullscreenElement);
});


// ── Keyboard navigation ───────────────────────────────────────────────────────

function _navDet(dir) {
    const rows = [...document.querySelectorAll('.det[data-ts]')];
    if (!rows.length) { return; }
    const idx  = rows.findIndex(function (r) { return r.dataset.ts === selectedDetTs; });
    const next = rows[
        idx < 0
            ? (dir > 0 ? 0 : rows.length - 1)
            : Math.max(0, Math.min(rows.length - 1, idx + dir))
    ];
    if (!next || next.dataset.ts === selectedDetTs) { return; }
    next.click();
    if (!next.onclick) { next.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
}

document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !document.fullscreenElement) { _applyFsMode(false); return; }
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.isContentEditable) { return; }
    if      (e.key === 'ArrowLeft')  { e.preventDefault(); _navDet(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); _navDet(1); }
    else if (e.key === 's')          { _satBtn.click(); }
});


// ── Epicenter visualization ───────────────────────────────────────────────────

let _epiLines = [], _pWaveCircle = null, _pWaveRingIv = null;
let _epiVizDet = null, _epiVizLat = null, _epiVizLon = null;
let _epiRedrawTimer = null;

function _clearEpiViz() {
    _epiLines.forEach(function (l) { map.removeLayer(l); });
    _epiLines = [];
    if (_pWaveRingIv) { clearInterval(_pWaveRingIv); _pWaveRingIv = null; }
    if (_pWaveCircle) { map.removeLayer(_pWaveCircle); _pWaveCircle = null; }
}

function _drawEpiViz(det, epicLat, epicLon) {
    _clearEpiViz();
    if (!det) { return; }

    _epiVizDet = det;
    _epiVizLat = epicLat;
    _epiVizLon = epicLon;

    const pVelMs = (CONFIG.pVelKmS || 8.0) * 1000;
    const offsets = det.arrival_offsets || {};

    (det.stations || []).forEach(function (key) {
        const coord = sCoords[key];
        if (!coord) { return; }

        const line = L.polyline(
            [[coord[0], coord[1]], [epicLat, epicLon]],
            { color: '#58a6ff', weight: 1.5, opacity: 0.55, dashArray: '6,4', interactive: false }
        ).addTo(map);
        _epiLines.push(line);

        const dt = offsets[key];
        const timing = dt == null ? '' : (dt === 0 ? ' (first)' : ' (' + (dt > 0 ? '+' : '') + dt.toFixed(1) + 's)');
        const label = key + timing;

        const labelLat = epicLat + 0.2 * (coord[0] - epicLat);
        const labelLon = epicLon + 0.2 * (coord[1] - epicLon);
        const p1 = map.latLngToContainerPoint([coord[0], coord[1]]);
        const p2 = map.latLngToContainerPoint([epicLat, epicLon]);
        let ang = Math.atan2(p2.y - p1.y, p2.x - p1.x) * 180 / Math.PI;
        if (ang > 90)  { ang -= 180; }
        if (ang < -90) { ang += 180; }

        const lm = L.marker([labelLat, labelLon], {
            interactive: false,
            icon: L.divIcon({
                className: '',
                iconSize: [400, 24],
                iconAnchor: [200, 12],
                html: '<div style="width:400px;height:24px;display:flex;align-items:center;justify-content:center;'
                    + 'transform:rotate(' + ang.toFixed(1) + 'deg);color:#c9d1d9;font-size:11px;font-weight:500;'
                    + 'letter-spacing:.3px;text-shadow:0 0 4px #0d1117,0 0 4px #0d1117,0 0 6px #0d1117;'
                    + 'white-space:nowrap;pointer-events:none">' + label + '</div>'
            })
        }).addTo(map);
        _epiLines.push(lm);
    });

    const initR = Math.max(0, (_serverNow() - det.unix_ts) * pVelMs);
    _pWaveCircle = L.circle([epicLat, epicLon], {
        radius: initR, color: '#f85149', weight: 1.5, fillOpacity: 0, opacity: 0.75, interactive: false
    }).addTo(map);

    _pWaveRingIv = setInterval(function () {
        if (!_pWaveCircle) { return; }
        const r = Math.max(0, (_serverNow() - det.unix_ts) * pVelMs);
        _pWaveCircle.setRadius(r);
        const fade = Math.max(0, 0.75 - (r / 5000000) * 0.65);
        _pWaveCircle.setStyle({ opacity: fade });
        if (r > 20100000) {
            clearInterval(_pWaveRingIv);
            _pWaveRingIv = null;
            if (_pWaveCircle) { map.removeLayer(_pWaveCircle); _pWaveCircle = null; }
        }
    }, 100);
}

function _scheduleEpiRedraw() {
    if (!_epiVizDet || _epiVizLat == null) { return; }
    clearTimeout(_epiRedrawTimer);
    _epiRedrawTimer = setTimeout(function () {
        _drawEpiViz(_epiVizDet, _epiVizLat, _epiVizLon);
    }, 80);
}

map.on('zoomend moveend', function () {
    applyMarkerSelection();
    _scheduleEpiRedraw();
});


// ── Station card hover ────────────────────────────────────────────────────────

let _staHoverIv = null, _staHoverPhase = 0;

function _staHoverIn(key) {
    if (_staHoverIv) { clearInterval(_staHoverIv); _staHoverIv = null; }
    const m = staMarkers[key];
    if (!m) { return; }
    _staHoverPhase = 0;
    m.openTooltip();
    _staHoverIv = setInterval(function () {
        _staHoverPhase = (_staHoverPhase + 0.18) % (2 * Math.PI);
        const p = Math.abs(Math.sin(_staHoverPhase));
        m.setRadius(4 + p * 10);
        m.setStyle({ fillOpacity: 0.55 + p * 0.45, weight: 1 + p * 2.5 });
    }, 40);
}

function _staHoverOut(key) {
    if (_staHoverIv) { clearInterval(_staHoverIv); _staHoverIv = null; }
    const m = staMarkers[key];
    if (!m) { return; }
    m.closeTooltip();
    m.setRadius(4);
    m.setStyle({ fillOpacity: 0.9, weight: 1 });
}


// ── Marker helpers ────────────────────────────────────────────────────────────

function zoomR(base) {
    const z = map.getZoom();
    const f = Math.max(0.35, Math.min(1.0, (z - 1) / 7));
    return Math.max(2, base * f);
}

function _markerBaseStyle(usgs, dimmed) {
    if (usgs) {
        return { color: '#7048aa', weight: 1.5, fillColor: '#a371f7', fillOpacity: dimmed ? 0.25 : 0.85, dashArray: null };
    }
    return { color: '#6e3010', weight: 1, fillColor: '#f85149', fillOpacity: dimmed ? 0.15 : 0.5, dashArray: '5,3' };
}

function applyMarkerSelection(skipPulse) {
    detMarkers.forEach(function (entry) {
        const { m, ts, r, usgs } = entry;
        if (ts === selectedDetTs) {
            m.setStyle({ color: '#2ea043', weight: 1.5, fillColor: '#3fb950', fillOpacity: 0.95, dashArray: null });
            if (!skipPulse && _pulseTs !== selectedDetTs) {
                if (_pulseIv) { clearInterval(_pulseIv); _pulseIv = null; }
                _pulseTs = selectedDetTs;
                _pulsePhase = 0;
                _pulseIv = setInterval(function () {
                    _pulsePhase = (_pulsePhase + 0.15) % (2 * Math.PI);
                    const p = Math.abs(Math.sin(_pulsePhase));
                    m.setRadius(zoomR(r) + p * 5);
                    m.setStyle({ fillOpacity: 0.55 + p * 0.4 });
                }, 40);
            }
        } else {
            m.setRadius(zoomR(r));
            m.setStyle(_markerBaseStyle(usgs, !!selectedDetTs));
        }
    });

    if (!selectedDetTs && _pulseIv) {
        clearInterval(_pulseIv);
        _pulseIv = null;
        _pulseTs = null;
    }
}

function applyRowSelection() {
    document.querySelectorAll('.det[data-ts]').forEach(function (el) {
        if (el.dataset.ts === selectedDetTs) {
            el.classList.add('det-selected');
        } else {
            el.classList.remove('det-selected');
        }
    });
}

function flyToEpi(lat, lon, ts) {
    selectedDetTs = ts || null;

    if (ts) {
        const det = _lastDets.find(function (d) { return d.ts === ts; });
        _drawEpiViz(det, lat, lon);
    } else {
        _clearEpiViz();
        _epiVizDet = null;
        _epiVizLat = null;
        _epiVizLon = null;
    }

    applyRowSelection();

    if (ts) {
        const row = document.querySelector('.det[data-ts="' + CSS.escape(ts) + '"]');
        if (row) { row.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
    }

    if (_pulseIv) { clearInterval(_pulseIv); _pulseIv = null; _pulseTs = null; }
    applyMarkerSelection(true);
    map.flyTo([lat, lon], FLY_ZOOM, { duration: FLY_DURATION, easeLinearity: 0.5 });
    map.once('moveend', function () { applyMarkerSelection(); });

    const row = ts ? document.querySelector('.det[data-ts="' + CSS.escape(ts) + '"]') : null;
    const unixTs = row ? row.dataset.unixTs : null;
    const params = new URLSearchParams(window.location.search);
    if (unixTs) { params.set('det', Math.round(unixTs)); } else { params.delete('det'); }
    if (filterConfirmed) { params.set('conf', '1'); } else { params.delete('conf'); }
    if (filterMinMb > 0) { params.set('mb', String(filterMinMb)); } else { params.delete('mb'); }
    if (filterLocal)     { params.set('local', '1'); } else { params.delete('local'); }
    const qs = params.toString();
    history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
}


// ── Audio alerts ──────────────────────────────────────────────────────────────

const _initS = loadSettings();
let audioEnabled = _initS.audioEnabled != null ? !!_initS.audioEnabled : true;
let desktopNotifEnabled = false;
let notifMinMb = _initS.notifMinMb != null ? parseFloat(_initS.notifMinMb) || 0 : 0;
let lastDetTs = null;
let audioCtx = null;

const muteBtn = document.getElementById('mute-btn');
muteBtn.textContent = audioEnabled ? '🔔 on' : '🔕 off';
muteBtn.style.color = audioEnabled ? '#8b949e' : '#6e7681';

muteBtn.addEventListener('click', function () {
    audioEnabled = !audioEnabled;
    muteBtn.textContent = audioEnabled ? '🔔 on' : '🔕 off';
    muteBtn.style.color = audioEnabled ? '#8b949e' : '#6e7681';
    if (!audioCtx) { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    saveSettings({ audioEnabled });
});

const notifBtn = document.getElementById('notif-btn');
notifBtn.addEventListener('click', async function () {
    if (!desktopNotifEnabled) {
        if (!('Notification' in window)) { return; }
        let perm = Notification.permission;
        if (perm === 'default') { perm = await Notification.requestPermission(); }
        if (perm !== 'granted') { notifBtn.title = 'Browser blocked notifications'; return; }
        desktopNotifEnabled = true;
        notifBtn.style.color = '#58a6ff';
        notifBtn.style.borderColor = '#58a6ff';
    } else {
        desktopNotifEnabled = false;
        notifBtn.style.color = '#6e7681';
        notifBtn.style.borderColor = '#30363d';
    }
});

const notifMbSel = document.getElementById('notif-mb-sel');
if (notifMinMb > 0) {
    notifMbSel.value = notifMinMb.toFixed(1);
    notifMbSel.style.color = '#58a6ff';
    notifMbSel.style.borderColor = '#58a6ff';
}
notifMbSel.addEventListener('change', function () {
    notifMinMb = parseFloat(notifMbSel.value) || 0;
    notifMbSel.style.color = notifMinMb > 0 ? '#58a6ff' : '#6e7681';
    notifMbSel.style.borderColor = notifMinMb > 0 ? '#58a6ff' : '#30363d';
    saveSettings({ notifMinMb });
});

function _notifMbOk(mb) {
    return notifMinMb === 0 || (mb != null && mb >= notifMinMb);
}

function playDetectionAlert(mb) {
    if (!audioEnabled || !_notifMbOk(mb)) { return; }
    try {
        if (!audioCtx) { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
        if (audioCtx.state === 'suspended') { audioCtx.resume(); }
        [[880, 0], [660, 0.18], [440, 0.32]].forEach(function (tone) {
            const freq = tone[0], t = tone[1];
            const osc  = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.frequency.value = freq;
            osc.type = 'sine';
            gain.gain.setValueAtTime(0.25, audioCtx.currentTime + t);
            gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + t + 0.16);
            osc.start(audioCtx.currentTime + t);
            osc.stop(audioCtx.currentTime + t + 0.18);
        });
    } catch (ignore) {}
}

function showDesktopNotification(det) {
    if (!desktopNotifEnabled || !('Notification' in window) || Notification.permission !== 'granted') { return; }
    if (!_notifMbOk(det.mb)) { return; }
    const mbStr = det.mb_local
        ? 'local'
        : det.mb_approx ? 'mb~' + det.mb.toFixed(1) : 'mb=' + det.mb.toFixed(1);
    const epiStr = det.epicenter
        ? ' · ' + det.epicenter[0].toFixed(1) + '°' + (det.epicenter[0] >= 0 ? 'N' : 'S')
        + ' ' + Math.abs(det.epicenter[1]).toFixed(1) + '°' + (det.epicenter[1] >= 0 ? 'E' : 'W')
        : '';
    new Notification('🌍 Seismic Detection', {
        body: det.stations.join(' · ') + '\n' + mbStr + epiStr,
        tag: 'seismic-det',
        renotify: true,
        silent: true
    });
}


// ── Replay controls ───────────────────────────────────────────────────────────

let _replayActive = false;
let _replayInterval = null;

const _mbPendingNotify = new Set();

function _replayStop() {
    if (_replayInterval) { clearTimeout(_replayInterval); _replayInterval = null; }
    _replayActive = false;
    _unpinMarker();
    const btn = document.getElementById('replay-btn');
    if (btn) { btn.textContent = '▶ replay'; }
}

function _replayStart(dets) {
    if (!dets || !dets.length) { return; }
    _replayStop();

    const sorted = [...dets]
        .filter(function (d) { return !d.teleseismic && (d.epicenter || (d.usgs && d.usgs.lat != null)); })
        .sort(function (a, b) { return a.unix_ts - b.unix_ts; });
    if (!sorted.length) { return; }

    _replayActive = true;
    let idx = 0;

    const btn = document.getElementById('replay-btn');
    if (btn) { btn.textContent = '⏹ stop'; }

    const scrub = document.getElementById('replay-scrub');
    if (scrub) { scrub.max = Math.max(0, sorted.length - 1); }

    function step() {
        if (!_replayActive) { return; }
        if (idx >= sorted.length) { _replayStop(); return; }

        const det = sorted[idx++];
        const la  = det.usgs && det.usgs.lat != null ? det.usgs.lat  : det.epicenter[0];
        const lo  = det.usgs && det.usgs.lon != null ? det.usgs.lon  : det.epicenter[1];

        if (scrub) { scrub.value = idx - 1; scrub.title = det.ts; }
        flyToEpi(la, lo, det.ts);

        map.once('moveend', function () {
            if (!_replayActive) { return; }
            const entry = detMarkers.find(function (x) { return x.ts === det.ts; });
            if (entry) { _pinMarker(entry.m); }
            _replayInterval = setTimeout(function () {
                _unpinMarker();
                step();
            }, REPLAY_DWELL_MS);
        });
    }

    step();
}

function _initReplayControls() {
    const replayBtn = document.getElementById('replay-btn');
    if (replayBtn) {
        replayBtn.addEventListener('click', function () {
            if (_replayActive) { _replayStop(); } else { _replayStart(_currentFilteredDets); }
        });
    }

    const scrub = document.getElementById('replay-scrub');
    if (scrub) {
        scrub.addEventListener('input', function () {
            const sorted = [..._currentFilteredDets].sort(function (a, b) { return a.unix_ts - b.unix_ts; });
            const det = sorted[parseInt(scrub.value, 10)];
            if (!det) { return; }
            selectedDetTs = det.ts;
            applyRowSelection();
            const row = document.querySelector('.det[data-ts="' + CSS.escape(det.ts) + '"]');
            if (row) { row.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
            if (det.epicenter || (det.usgs && det.usgs.lat != null)) {
                const la = det.usgs && det.usgs.lat != null ? det.usgs.lat : det.epicenter[0];
                const lo = det.usgs && det.usgs.lon != null ? det.usgs.lon : det.epicenter[1];
                map.flyTo([la, lo], FLY_ZOOM, { duration: 0.6, easeLinearity: 0.5 });
                map.once('moveend', applyMarkerSelection);
            }
        });
    }
}


// ── Replay tick marks ─────────────────────────────────────────────────────────

function _updateReplayTicks(dets) {
    const el = document.getElementById('replay-ticks');
    if (!el) { return; }

    const sorted = [...dets].sort(function (a, b) { return a.unix_ts - b.unix_ts; });
    const n = sorted.length;
    if (n < 2) { el.innerHTML = ''; return; }

    const minT = sorted[0].unix_ts;
    const maxT = sorted[n - 1].unix_ts;
    const span = maxT - minT || 1;

    el.innerHTML = sorted.map(function (det) {
        const pct = ((det.unix_ts - minT) / span * 100).toFixed(2);
        const color = det.usgs ? '#a371f7' : det.epicenter ? '#586069' : '#3a3f47';
        return '<div style="position:absolute;left:' + pct + '%;top:0;width:1px;height:6px;background:' + color + ';transform:translateX(-50%)"></div>';
    }).join('');
}


// ── BTC VM ledger ─────────────────────────────────────────────────────────────

let _btcvmByUnix = {};
let _btcvmBatchByHash = {};

function _pollBtcvm() {
    fetch('/api/btcvm').then(function (r) {
        return r.json();
    }).then(function (d) {
        const byUnix = {};
        const byHash = {};
        (d.entries || []).forEach(function (e) {
            if (e.label === 'batch' && e.tx_hash && Array.isArray(e.det_hashes)) {
                e.det_hashes.forEach(function (h) { byHash[h] = e; });
            } else if (e.det_unix != null) {
                const k = e.det_unix.toFixed(3);
                if (!byUnix[k] || e.label === 'confirmed') { byUnix[k] = e; }
            }
        });
        _btcvmByUnix = byUnix;
        _btcvmBatchByHash = byHash;
    }).catch(function () {});
}

_pollBtcvm();
setInterval(_pollBtcvm, 15000);


// ── HTML builders ─────────────────────────────────────────────────────────────

function showMoreDets() { detDisplayLimit += 50; }

function escAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

function _mbChipClass(mb) {
    return mb >= 5 ? 'chip-mb-high' : mb >= 4 ? 'chip-mb-mid' : 'chip-mb-low';
}

function _buildMbChip(det) {
    if (det.mb != null) {
        if (det.mb_local) {
            return '<span class="chip chip-mb-approx" title="Amplitude ratio between stations suggests a local/regional source; IASPEI mb unreliable">local</span>';
        }
        const lbl = (det.mb_approx ? 'mb~' : 'mb=') + det.mb.toFixed(1);
        const cls = _mbChipClass(det.mb) + (det.mb_approx ? ' chip-mb-approx' : '');
        const title = det.mb_approx ? 'approx, assumed distance 45 deg' : 'IASPEI body-wave';
        return '<span class="chip ' + cls + '" title="' + title + '">' + lbl + '</span>';
    }
    return '<span class="chip" style="color:#6e7681;background:#161b22">mb…</span>';
}

function _buildEpiChip(det) {
    if (!det.epicenter) { return ''; }
    if (det.teleseismic) {
        return '<span class="chip chip-epi" title="Localization unreliable (high residual) — likely distant teleseismic source" style="opacity:.7">&#x1F310; teleseismic</span>';
    }
    const la = det.epicenter[0], lo = det.epicenter[1];
    const ns = la >= 0 ? 'N' : 'S', ew = lo >= 0 ? 'E' : 'W';
    return '<button class="chip chip-epi" onclick="event.stopPropagation();flyToEpi(' + la + ',' + lo + ',\'' + det.ts + '\')" '
        + 'title="' + Math.abs(la).toFixed(2) + '°' + ns + ' ' + Math.abs(lo).toFixed(2) + '°' + ew + '" '
        + 'style="cursor:pointer;border:none;font-family:inherit">&#x1F4CD;</button>';
}

function _buildUsgsIcon(det) {
    if (det.usgs) {
        const place    = det.usgs.place || '';
        const mt       = det.usgs.magType || '';
        const src      = det.usgs.source || 'usgs';
        const srcLabel = src === 'emsc' ? 'EMSC' : 'USGS';
        const iconColor = src === 'emsc' ? '#39c5cf' : '#a371f7';
        const title = srcLabel + ': M' + det.usgs.mag + mt + ' — ' + place;
        const eid   = det.usgs.event_id || '';
        const href  = eid
            ? (src === 'emsc'
                ? 'https://www.seismicportal.eu/eventdetails.html?unid=' + encodeURIComponent(eid)
                : 'https://earthquake.usgs.gov/earthquakes/eventpage/' + encodeURIComponent(eid) + '/executive')
            : '';
        const inner = '<span style="color:' + iconColor + '">&#10003;</span>';
        return href
            ? '<a class="det-usgs-icon" href="' + href + '" target="_blank" rel="noopener" title="' + escAttr(title) + '" style="text-decoration:none" onclick="event.stopPropagation()">' + inner + '</a>'
            : '<span class="det-usgs-icon" title="' + escAttr(title) + '">' + inner + '</span>';
    }
    if (det.usgs_checked) {
        const title = 'No match in USGS (M' + CONFIG.usgsMag + '+) or EMSC (M' + CONFIG.emscMag + '+) for this window';
        return '<span class="det-usgs-icon" style="color:#30363d" title="' + escAttr(title) + '">&#10007;</span>';
    }
    return '<span class="det-usgs-icon" style="color:#6e7681" title="Catalog lookup pending">&#8943;</span>';
}

function _buildBtcIcon(det) {
    const key   = det.unix_ts != null ? det.unix_ts.toFixed(3) : null;
    const ledger = key ? _btcvmByUnix[key] : null;
    const batch  = ledger ? _btcvmBatchByHash[ledger.det_hash] : null;

    if (batch) {
        const tip = 'On-chain (daily batch) · block ' + batch.block_height + '\nmerkle root: ' + (batch.merkle_root || '') + '\ntx: ' + batch.tx_hash + '\n' + batch.n + ' detections in batch';
        return '<a class="det-usgs-icon" href="https://blockstream.info/tx/' + encodeURIComponent(batch.tx_hash) + '" target="_blank" rel="noopener" title="' + escAttr(tip) + '" style="text-decoration:none;color:#f7931a" onclick="event.stopPropagation()">₿</a>';
    }
    if (ledger && ledger.tx_hash) {
        const tip = 'On-chain (v1 individual) · block ' + ledger.block_height + '\ntx: ' + ledger.tx_hash;
        return '<a class="det-usgs-icon" href="https://blockstream.info/tx/' + encodeURIComponent(ledger.tx_hash) + '" target="_blank" rel="noopener" title="' + escAttr(tip) + '" style="text-decoration:none;color:#f7931a;opacity:.65" onclick="event.stopPropagation()">₿</a>';
    }
    if (ledger) {
        const isConf = ledger.label === 'confirmed';
        const tip = 'Ledger anchor (batch pending) · block ' + ledger.block_height + '\ncommitment: ' + (ledger.commitment || '') + '\n' + (isConf ? 'Catalog confirmed' : 'Raw detection');
        const blockUrl = ledger.block_hash ? 'https://blockstream.info/block/' + encodeURIComponent(ledger.block_hash) : '';
        return blockUrl
            ? '<a class="det-usgs-icon" href="' + blockUrl + '" target="_blank" rel="noopener" title="' + escAttr(tip) + '" style="text-decoration:none;color:#a07830" onclick="event.stopPropagation()">₿</a>'
            : '<span class="det-usgs-icon" style="color:#a07830" title="' + escAttr(tip) + '">₿</span>';
    }
    return '';
}

function _buildDetRow(det, serverStart, deployLabel, sepInserted) {
    let sep = '';
    if (!sepInserted.done && det.unix_ts < serverStart) {
        sepInserted.done = true;
        sep = '<div class="det-deploy-sep" title="Process restarted / new version deployed at '
            + fmtLocal(new Date(serverStart * 1000).toISOString()) + '">deployed ' + deployLabel + '</div>';
    }

    const mbNote = det.mb != null
        ? (det.mb_local ? 'local source (amp ratio > 5x)' : det.mb_approx ? 'mb~' + det.mb.toFixed(1) + ' IASPEI Δ≈45°' : 'mb=' + det.mb.toFixed(1) + ' IASPEI')
        : 'mb pending';
    const catStr = det.usgs
        ? '\n' + (det.usgs.source || 'usgs').toUpperCase() + ': M' + det.usgs.mag + (det.usgs.magType || '') + ' — ' + (det.usgs.place || '')
        : det.usgs_checked
            ? '\nNo catalog match (USGS M' + CONFIG.usgsMag + '+ / EMSC M' + CONFIG.emscMag + '+)'
            : '\nCatalog lookup pending';
    const detTitle = det.ts + '\n' + det.stations.join(', ')
        + '\nconf: ' + det.conf.toFixed(4) + '  gap: ' + (det.logit_gap || 0).toFixed(1)
        + (det.epicenter ? '\nepi: ' + det.epicenter[0].toFixed(2) + 'N ' + det.epicenter[1].toFixed(2) + 'E' : '')
        + '\n' + mbNote + catStr;

    const pinLat = det.usgs && det.usgs.lat != null ? det.usgs.lat  : det.epicenter ? det.epicenter[0] : null;
    const pinLon = det.usgs && det.usgs.lon != null ? det.usgs.lon  : det.epicenter ? det.epicenter[1] : null;
    const canClick = !det.teleseismic && pinLat != null;

    const selCls   = det.ts === selectedDetTs ? ' det-selected' : '';
    const mutedCls = canClick ? '' : ' det-muted';
    const verifCls = (det.usgs && canClick) ? ' det-verified' : '';
    const rowClick = canClick
        ? 'onclick="flyToEpi(' + pinLat + ',' + pinLon + ',\'' + det.ts + '\')" style="cursor:pointer"'
        : '';

    return sep
        + '<div class="det' + selCls + mutedCls + verifCls + '" data-ts="' + det.ts + '" data-unix-ts="' + det.unix_ts + '" ' + rowClick + ' title="' + escAttr(detTitle) + '">'
        + '<div class="det-row1"><span class="det-time">' + fmtLocal(det.ts) + '</span><span class="det-age">' + fmtAge(det.unix_ts) + '</span></div>'
        + '<div class="det-row2"><span class="det-stas">' + det.stations.join(' · ') + '</span>'
        + '<span class="det-chips-inline">' + _buildMbChip(det) + _buildEpiChip(det) + _buildUsgsIcon(det) + _buildBtcIcon(det) + '</span></div>'
        + '</div>';
}


// ── Render: stations ──────────────────────────────────────────────────────────

function _renderStations(d) {
    const sDiv = document.getElementById('stations');
    const sorted = Object.entries(d.stations).sort(function (a, b) {
        return staSortMode === 'name' ? a[0].localeCompare(b[0]) : b[1].conf - a[1].conf;
    });

    let html = '';
    sorted.forEach(function (entry) {
        const k = entry[0], s = entry[1];
        const pct  = Math.round(s.conf * 100);
        const col  = confColor(s.conf);
        const coord = sCoords[k]
            ? sCoords[k][0].toFixed(2) + '°N ' + sCoords[k][1].toFixed(2) + '°E'
            : 'coords unknown';
        const flatline = !!s.flatline;
        const cardTitle = k + '\n' + coord + '\nconf: ' + s.conf.toFixed(4) + '\nlast sample: ' + fmtLocal(new Date(s.last_ts * 1000).toISOString());
        const barTitle  = 'threshold: ' + CONFIG.threshold + ' | current: ' + s.conf.toFixed(3);

        let sparkSvg = '';
        const hist = s.conf_history || [];
        if (hist.length > 1) {
            const W = 200, H = 24, thr = CONFIG.threshold;
            const pts = hist.slice(-120);
            const mx  = Math.max(1, ...pts);
            const xs  = function (i) { return ((i / (pts.length - 1)) * W).toFixed(1); };
            const ys  = function (v) { return (H - 1 - v / mx * (H - 1)).toFixed(1); };
            const polyline = pts.map(function (v, i) { return xs(i) + ',' + ys(v); }).join(' ');
            sparkSvg = '<div class="spark-wrap">'
                + '<svg width="100%" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">'
                + '<line x1="0" y1="' + ys(thr) + '" x2="' + W + '" y2="' + ys(thr) + '" stroke="#d29922" stroke-width="0.7" stroke-dasharray="3,2"/>'
                + '<polyline points="' + polyline + '" fill="none" stroke="' + col + '" stroke-width="1.2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
                + '</svg><div class="spark-cursor"></div></div>';
        }

        html += '<div class="station" title="' + cardTitle + (flatline ? '\n⚠ Flatline — zero variance, feed may be dead' : '') + '"'
            + (flatline ? ' style="opacity:.55;border-left:2px solid #f85149"' : '')
            + ' onmouseenter="_staHoverIn(\'' + k + '\')" onmouseleave="_staHoverOut(\'' + k + '\')">'
            + '<div class="sta-row"><span class="sta-name">' + k + '</span>'
            + (flatline ? '<span style="color:#f85149;font-size:9px;letter-spacing:.5px">FLAT</span>' : '')
            + '<span class="sta-conf" style="color:' + (flatline ? '#f85149' : col) + '">' + s.conf.toFixed(3) + '</span></div>'
            + '<div class="conf-bar" title="' + barTitle + '"><div class="conf-fill" style="width:' + pct + '%;background:' + (flatline ? '#f85149' : col) + '"></div></div>'
            + sparkSvg
            + '<div style="color:#6e7681;font-size:10px">' + coord + ' &mdash; ' + fmtAge(s.last_ts) + ' ago</div>'
            + '</div>';

        if (sCoords[k] && !staMarkers[k]) {
            const latlon = sCoords[k];
            staMarkers[k] = L.circleMarker([latlon[0], latlon[1]], {
                radius: 4, color: '#3a6fa8', weight: 1, fillColor: '#58a6ff', fillOpacity: 0.9
            }).bindTooltip(
                '<div class="sta-tip"><span class="tip-key">' + k + '</span><div class="tip-conf">' + coord + '</div></div>',
                { permanent: false, direction: 'top', className: 'sta-tip' }
            ).addTo(map);
        }

        if (staMarkers[k]) {
            const mc = confColor(s.conf);
            if (staMarkers[k].options.fillColor !== mc) {
                staMarkers[k].setStyle({ color: mc, fillColor: mc, fillOpacity: 0.9 });
            }
            const tip = '<div class="sta-tip"><span class="tip-key">' + k + '</span>'
                + '<div class="tip-conf">' + coord + '</div>'
                + '<div style="margin-top:3px;color:' + mc + ';font-size:10px">conf ' + pct + '%'
                + (s.last_ts ? ' &middot; ' + fmtAge(s.last_ts) + ' ago' : '') + '</div></div>';
            if (staMarkers[k]._tooltip && staMarkers[k]._tooltip._content !== tip) {
                staMarkers[k].setTooltipContent(tip);
            }
        }
    });

    if (sDiv.innerHTML !== html) { sDiv.innerHTML = html; }

    const mobSta = document.getElementById('mobile-sta-panel');
    if (mobSta && mobSta.innerHTML !== html) { mobSta.innerHTML = html; }
}


// ── Render: detection list ────────────────────────────────────────────────────

function _renderDetections(dets, filteredDets, serverStart) {
    const dDiv = document.getElementById('detections');
    if (!dets.length) {
        dDiv.innerHTML = '<div class="no-data">No detections yet</div>';
        return;
    }

    const deployLabel = (function () {
        const dt = new Date(serverStart * 1000);
        return dt.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: _activeTz() }) + ' ' + _tzAbbr();
    }());

    const moreEl = document.getElementById('det-more');
    if (moreEl) { moreEl.style.display = filteredDets.length > detDisplayLimit ? 'block' : 'none'; }

    const sepInserted = { done: false };
    const newHtml = filteredDets.slice(0, detDisplayLimit).map(function (det) {
        return _buildDetRow(det, serverStart, deployLabel, sepInserted);
    }).join('');

    if (dDiv.innerHTML !== newHtml) { dDiv.innerHTML = newHtml; }
}


// ── Render: epicenter markers ─────────────────────────────────────────────────

function _renderEpiMarkers(filteredDets) {
    const epiDets = filteredDets.filter(function (det) {
        return !det.teleseismic && (det.epicenter || (det.usgs && det.usgs.lat != null));
    });
    const epiTsSet = new Set(epiDets.map(function (det) { return det.ts; }));
    const keptTs = new Set();

    detMarkers.forEach(function (entry) {
        const det = epiDets.find(function (d) { return d.ts === entry.ts; });
        const usgsC = det && det.usgs && det.usgs.lat != null;
        const newLat = usgsC ? det.usgs.lat : (det && det.epicenter ? det.epicenter[0] : null);
        const newLon = usgsC ? det.usgs.lon : (det && det.epicenter ? det.epicenter[1] : null);
        const moved  = newLat != null && (Math.abs(newLat - entry.lat) > 0.5 || Math.abs(newLon - entry.lon) > 0.5);
        if (!epiTsSet.has(entry.ts) || moved) {
            map.removeLayer(entry.m);
        } else {
            keptTs.add(entry.ts);
        }
    });

    const kept = detMarkers.filter(function (entry) { return keptTs.has(entry.ts); });
    let markersChanged = kept.length !== detMarkers.length;

    epiDets.forEach(function (det) {
        if (keptTs.has(det.ts)) { return; }
        markersChanged = true;

        const usgsCoords = det.usgs && det.usgs.lat != null;
        const la = usgsCoords ? det.usgs.lat : det.epicenter[0];
        const lo = usgsCoords ? det.usgs.lon : det.epicenter[1];
        const mb = det.mb || 4;
        const r  = Math.max(4, Math.min(14, (mb - 2) * 3 + 4));

        const mbLabel  = det.mb ? (det.mb_local ? 'local' : det.mb_approx ? 'mb~' + det.mb.toFixed(1) : 'mb=' + det.mb.toFixed(1)) : 'mb pending';
        const mbClass  = mb >= 5 ? 'high' : mb >= 4 ? 'mid' : 'low';
        const locSrc   = usgsCoords ? 'USGS' : 'sensor';
        const locStr   = locSrc + ': ' + Math.abs(la).toFixed(2) + '°' + (la >= 0 ? 'N' : 'S') + ' ' + Math.abs(lo).toFixed(2) + '°' + (lo >= 0 ? 'E' : 'W');
        const hasOrig  = usgsCoords && det.epicenter && det.epicenter[0] != null;
        const origLa   = hasOrig ? det.epicenter[0] : null;
        const origLo   = hasOrig ? det.epicenter[1] : null;
        const origStr  = hasOrig ? Math.abs(origLa).toFixed(2) + '°' + (origLa >= 0 ? 'N' : 'S') + ' ' + Math.abs(origLo).toFixed(2) + '°' + (origLo >= 0 ? 'E' : 'W') : '';
        const origLink = hasOrig
            ? '<div class="tip-loc-orig"><a href="#" onclick="event.preventDefault();event.stopPropagation();map.flyTo([' + origLa + ',' + origLo + '],3,{duration:1.0})">sensor: ' + origStr + '</a></div>'
            : '';

        const tipHtml = '<div class="det-tip">'
            + '<div class="tip-time">' + fmtLocal(det.ts) + ' <span style="color:#6e7681">(' + fmtAge(det.unix_ts) + ' ago)</span></div>'
            + '<div class="tip-mb ' + mbClass + '">' + mbLabel + '</div>'
            + '<div class="tip-stas">' + det.stations.join(' · ') + '</div>'
            + '<div class="tip-loc">' + locStr + '</div>'
            + origLink + '</div>';

        const m = L.circleMarker([la, lo], Object.assign({ radius: zoomR(r) }, _markerBaseStyle(usgsCoords, false)))
            .bindTooltip(tipHtml, { sticky: false, direction: 'top', className: 'det-tip' })
            .addTo(map);

        m.on('click', function (e) {
            L.DomEvent.stopPropagation(e);
            if (_pinnedMarker === m) { _unpinMarker(); } else { _pinMarker(m); }
            const entry = detMarkers.find(function (x) { return x.m === m; });
            if (entry) {
                selectedDetTs = entry.ts;
                applyRowSelection();
                const row = document.querySelector('.det[data-ts="' + CSS.escape(entry.ts) + '"]');
                if (row) { row.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
            }
        });

        m.on('mouseout', function () { if (_pinnedMarker === m) { m.openTooltip(); } });

        kept.push({ m, ts: det.ts, r, lat: la, lon: lo, usgs: usgsCoords });
    });

    detMarkers.length = 0;
    kept.forEach(function (x) { detMarkers.push(x); });
    if (markersChanged) { applyMarkerSelection(); }
}


// ── Main update ───────────────────────────────────────────────────────────────

function update() {
    fetch('/api/state').then(function (r) {
        if (!r.ok) { throw new Error('HTTP ' + r.status); }
        return r.json();
    }).then(function (d) {
        document.getElementById('status-dot').style.background = '';
        try { _updateBody(d); } catch (e) { console.error('[seismic] update error:', e); }
    }).catch(function () {
        document.getElementById('status-dot').style.background = '#f85149';
    });
}

function _updateBody(d) {
    if (d.detections) { _lastDets = [...d.detections]; }

    const now       = new Date();
    const localStr  = now.toLocaleTimeString('en', { timeZone: _activeTz(), hour: '2-digit', minute: '2-digit', second: '2-digit' }) + ' ' + _tzAbbr();
    const utcStr    = now.toLocaleTimeString('en', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) + ' UTC';
    document.getElementById('last-update').textContent = 'updated ' + localStr + ' (' + utcStr + ')';

    if (d.now) {
        _serverClockOffset = d.now - Date.now() / 1000;
        const skewEl = document.getElementById('clock-skew-banner');
        if (skewEl) {
            if (Math.abs(_serverClockOffset) > 120) {
                skewEl.style.display = 'block';
                skewEl.textContent = '⚠ Clock skew: server is ' + (_serverClockOffset > 0 ? '+' : '') + Math.round(_serverClockOffset) + 's vs browser — check your local clock';
            } else {
                skewEl.style.display = 'none';
            }
        }
    }

    if (d.station_coords) { Object.assign(sCoords, d.station_coords); }

    _renderStations(d);

    const dets = [...d.detections].reverse();
    let filteredDets = dets;
    if (filterConfirmed) { filteredDets = filteredDets.filter(function (det) { return !!det.usgs; }); }
    if (filterLocal)     { filteredDets = filteredDets.filter(function (det) { return det.epicenter && !det.teleseismic; }); }
    if (filterMinMb > 0) { filteredDets = filteredDets.filter(function (det) { return det.mb != null && det.mb >= filterMinMb; }); }
    _currentFilteredDets = filteredDets;

    const cntEl = document.getElementById('det-count');
    const activeFilters = filterConfirmed || filterLocal || filterMinMb > 0;
    if (cntEl) {
        cntEl.textContent = activeFilters
            ? filteredDets.length + ' / ' + d.detections.length
            : (d.detections.length ? d.detections.length + ' total' : '');
    }

    if (dets.length) {
        const newest = dets[0];
        if (lastDetTs !== null && newest.ts !== lastDetTs) {
            playDetectionAlert(newest.mb);
            if (newest.mb != null) {
                showDesktopNotification(newest);
            } else {
                _mbPendingNotify.add(newest.ts);
            }
        }
        lastDetTs = newest.ts;

        dets.forEach(function (det) {
            if (_mbPendingNotify.has(det.ts) && det.mb != null) {
                _mbPendingNotify.delete(det.ts);
                showDesktopNotification(det);
            }
        });
    }

    const sumEl = document.getElementById('last-event-summary');
    if (sumEl && dets.length) {
        const ld    = dets[0];
        const mbStr = ld.mb != null
            ? (ld.mb_local ? 'local' : ld.mb_approx ? 'mb~' + ld.mb.toFixed(1) : 'mb=' + ld.mb.toFixed(1))
            : 'mb…';
        const age = fmtAge(ld.unix_ts);
        sumEl.textContent = 'Last: ' + mbStr + ' · ' + (age === '—' ? 'future?' : age) + ' ago';
    }

    const scrub = document.getElementById('replay-scrub');
    if (scrub) {
        scrub.max   = Math.max(0, filteredDets.length - 1);
        scrub.value = scrub.max;
    }
    _updateReplayTicks(filteredDets);
    _renderDetections(dets, filteredDets, d.server_start || 0);
    _renderEpiMarkers(filteredDets);

    const newestEpi = dets.find(function (det) {
        return !det.teleseismic && (det.epicenter || (det.usgs && det.usgs.lat != null));
    });
    if (!_deepLinkTs && newestEpi) {
        const usgsC = newestEpi.usgs && newestEpi.usgs.lat != null;
        const la    = usgsC ? newestEpi.usgs.lat : newestEpi.epicenter[0];
        const lo    = usgsC ? newestEpi.usgs.lon : newestEpi.epicenter[1];
        const isNew = newestEpi.ts !== lastFlyTs;
        const moved = lastFlyLat != null && (Math.abs(la - lastFlyLat) > 1 || Math.abs(lo - lastFlyLon) > 1);
        if (isNew || moved) {
            lastFlyTs = newestEpi.ts;
            lastFlyLat = la;
            lastFlyLon = lo;
            selectedDetTs = newestEpi.ts;
            applyRowSelection();
            map.flyTo([la, lo], FLY_ZOOM, { duration: FLY_DURATION, easeLinearity: 0.5 });
            map.once('moveend', applyMarkerSelection);
            _drawEpiViz(newestEpi, la, lo);
        }
    }

    const fsoSta = document.getElementById('fso-stations');
    if (fsoSta) {
        fsoSta.innerHTML = Object.entries(d.stations)
            .sort(function (a, b) {
                return staSortMode === 'name' ? a[0].localeCompare(b[0]) : b[1].conf - a[1].conf;
            })
            .map(function (entry) {
                const k = entry[0], s = entry[1];
                const col = confColor(s.conf);
                const pct = Math.round(s.conf * 100);
                return '<div class="fso-sta"><span style="color:#58a6ff">' + k + '</span><span style="color:' + col + '">' + s.conf.toFixed(3) + '</span></div>'
                    + '<div class="fso-bar"><div class="fso-bar-fill" style="width:' + pct + '%;background:' + col + '"></div></div>';
            }).join('');
    }

    const fsoDet = document.getElementById('fso-det');
    if (fsoDet && dets.length) {
        const ld    = dets[0];
        const mbStr = ld.mb != null
            ? (ld.mb_local ? 'local' : ld.mb_approx ? 'mb~' + ld.mb.toFixed(1) : 'mb=' + ld.mb.toFixed(1))
            : 'mb…';
        const src     = (ld.usgs && (ld.usgs.source || 'usgs').toUpperCase()) || '';
        const usgsStr = ld.usgs
            ? src + ': M' + ld.usgs.mag + ' ' + (ld.usgs.place || '').split(',')[0]
            : ld.usgs_checked ? 'no catalog match' : 'catalog pending';
        fsoDet.innerHTML = '<div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Latest Detection</div>'
            + '<div style="color:#e6edf3">' + fmtLocal(ld.ts) + '</div>'
            + '<div style="color:#58a6ff;margin:2px 0">' + ld.stations.join(' · ') + '</div>'
            + '<div style="color:#d29922">' + mbStr + '</div>'
            + (ld.epicenter ? '<div style="color:#d29922">' + ld.epicenter[0].toFixed(2) + 'N ' + ld.epicenter[1].toFixed(2) + 'E</div>' : '')
            + '<div style="color:#a371f7;margin-top:2px">' + usgsStr + '</div>';
    }
}


// ── Mobile tab switcher ───────────────────────────────────────────────────────

function _mobTab(which, btn) {
    document.querySelectorAll('.mob-tab').forEach(function (b) { b.classList.remove('active'); });
    btn.classList.add('active');
    const dw = document.getElementById('detections-wrap');
    const sp = document.getElementById('mobile-sta-panel');
    if (which === 'dets') {
        if (dw) { dw.classList.add('active'); }
        if (sp) { sp.classList.remove('active'); }
    } else {
        if (sp) { sp.classList.add('active'); }
        if (dw) { dw.classList.remove('active'); }
    }
}


// ── Station panel resize handle ───────────────────────────────────────────────

(function () {
    const handle = document.getElementById('sta-resize-handle');
    const grid   = document.querySelector('.grid');
    if (!handle || !grid) { return; }

    const MIN_W = 140, MAX_W = 480;
    const saved = parseInt(localStorage.getItem('staW'), 10) || 220;
    grid.style.setProperty('--sta-w', saved + 'px');

    let dragging = false, startX = 0, startW = 0;

    handle.addEventListener('mousedown', function (e) {
        dragging = true;
        startX = e.clientX;
        startW = parseInt(getComputedStyle(grid).getPropertyValue('--sta-w'), 10) || 220;
        handle.classList.add('dragging');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
    });

    document.addEventListener('mousemove', function (e) {
        if (!dragging) { return; }
        const w = Math.max(MIN_W, Math.min(MAX_W, startW + (e.clientX - startX)));
        grid.style.setProperty('--sta-w', w + 'px');
        map.invalidateSize();
    });

    document.addEventListener('mouseup', function () {
        if (!dragging) { return; }
        dragging = false;
        handle.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        const w = parseInt(getComputedStyle(grid).getPropertyValue('--sta-w'), 10) || 220;
        localStorage.setItem('staW', w);
    });
}());


// ── Sparkline crosshair ───────────────────────────────────────────────────────

(function () {
    const staPanel = document.getElementById('stations');
    if (!staPanel) { return; }

    staPanel.addEventListener('mousemove', function (e) {
        document.querySelectorAll('.spark-wrap').forEach(function (wrap) {
            const rect = wrap.getBoundingClientRect();
            if (!rect.width) { return; }
            const x = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
            const cursor = wrap.querySelector('.spark-cursor');
            if (cursor) { cursor.style.left = x + 'px'; cursor.style.display = 'block'; }
        });
    });

    staPanel.addEventListener('mouseleave', function () {
        document.querySelectorAll('.spark-cursor').forEach(function (c) { c.style.display = 'none'; });
    });
}());


// ── Startup ───────────────────────────────────────────────────────────────────

_initReplayControls();
update();
setInterval(update, 3000);

// Deep link: ?det=<unix_ts> → highlight row and fly map
(function () {
    if (!_deepLinkTs) { return; }

    function resetFilterButtons() {
        const confBtn  = document.getElementById('filter-btn');
        const localBtn = document.getElementById('filter-local-btn');
        const mbSel    = document.getElementById('mb-filter-sel');
        if (confBtn)  { confBtn.style.color = '#6e7681'; confBtn.style.borderColor = '#30363d'; }
        if (localBtn) { localBtn.style.color = '#6e7681'; localBtn.style.borderColor = '#30363d'; }
        if (mbSel)    { mbSel.selectedIndex = 0; mbSel.style.color = '#8b949e'; mbSel.style.borderColor = '#30363d'; }
    }

    setTimeout(resetFilterButtons, 100);

    let attempts = 0;

    function tryHighlight() {
        const rows = document.querySelectorAll('.det[data-unix-ts]');
        for (const row of rows) {
            if (Math.abs((parseFloat(row.dataset.unixTs) || 0) - _deepLinkTs) < 2) {
                row.scrollIntoView({ behavior: 'smooth', block: 'center' });
                row.style.transition = 'outline .2s,box-shadow .2s';
                row.style.outline = '2px solid #58a6ff';
                row.style.boxShadow = '0 0 10px #58a6ff99';
                setTimeout(function () { row.style.outline = ''; row.style.boxShadow = ''; }, 4000);
                const ts = row.dataset.ts;
                if (ts) {
                    const entry = detMarkers.find(function (x) { return x.ts === ts; });
                    if (entry) {
                        const ll = entry.m.getLatLng();
                        map.flyTo([ll.lat, ll.lng], FLY_ZOOM, { duration: 1.2, easeLinearity: 0.5 });
                    }
                }
                return;
            }
        }
        if (++attempts < 12) { setTimeout(tryHighlight, 600); }
    }

    setTimeout(tryHighlight, 800);
}());
