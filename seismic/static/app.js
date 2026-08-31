// ── Module constants ──────────────────────────────────────────────────────────

const CONFIG = window.SEISMIC_CONFIG;
const SETTINGS_KEY = 'seismic_settings';
// FAULT_GEOJSON_URL replaced with USGS WMS tile service (see fault toggle handler)
const REPLAY_DWELL_MS = 4000;
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

// Cluster bubbles are L.Marker (divIcon) → markerPane (z=600 by default),
// while individual epicenter dots are L.circleMarker (SVG path) →
// overlayPane (z=400) — different panes, so a plain bringToFront() can't
// lift a selected dot above a cluster bubble; it only reorders within its
// own pane. pinnedPane sits above markerPane (but below tooltipPane, so
// tooltips of any marker still read over it) for exactly the selected pin.
map.createPane('pinnedPane');
map.getPane('pinnedPane').style.zIndex = 625;

const _mbToken = CONFIG.mapboxToken;

// CARTO's free dark_all tiles now require an API key we don't have and
// return a tiled "API KEY REQUIRED" placeholder image instead of a 4xx —
// Mapbox's own dark style, already paid for via the same token used below
// for satellite view, replaces it when a token is configured.
const _darkLayer = (_mbToken
    ? L.tileLayer(
        'https://api.mapbox.com/styles/v1/mapbox/dark-v11/tiles/256/{z}/{x}/{y}@2x?access_token=' + _mbToken,
        { attribution: '&copy; <a href="https://www.mapbox.com/">Mapbox</a> &copy; OpenStreetMap', maxZoom: 20, tileSize: 256 }
    )
    : L.tileLayer(
        'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
        { attribution: '&copy; OSM &copy; CARTO', subdomains: 'abcd', maxZoom: 19 }
    )
).addTo(map);

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

// Epicenter markers cluster at low zoom so a large detection history doesn't
// pile into an unreadable stack of overlapping circles. Cluster color reuses
// the same magnitude ramp as the individual markers — a cluster containing a
// big event should read as "big" before you even expand it.
//
// Clustering can be toggled off (see _setClustering below). markercluster's
// grid radius is baked in at construction (_generateInitialClusters runs
// once, off the initial options) and isn't a documented runtime-mutable
// setting, so toggling swaps the whole group for a fresh one built with
// maxClusterRadius near zero rather than trying to mutate it in place.
function _makeEpiCluster(radius) {
    return L.markerClusterGroup({
        maxClusterRadius: radius,
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
        iconCreateFunction: function (cluster) {
            const markers = cluster.getAllChildMarkers();
            const maxMb = markers.reduce(function (m, mk) {
                return Math.max(m, mk._detMb || 0);
            }, 0);
            // Same validated sequential hue as individual markers, just varying
            // lightness/opacity across the disc instead of a flat fill — still
            // one hue (dataviz: sequential = one hue, light→dark), not a second
            // palette. Off-center highlight reads as a glossy sphere; the inset
            // ring is the mark-spec's "ring on overlapping marks" applied to a
            // cluster, which *is* an aggregate of overlapping marks.
            const rgb = _magColorRgb(maxMb).join(',');
            const n = markers.length;
            const size = Math.round(Math.min(56, 22 + Math.sqrt(n) * 8));
            return L.divIcon({
                html: '<div style="width:100%;height:100%;border-radius:50%;'
                    + 'background:radial-gradient(circle at 35% 30%, rgba(' + rgb + ',0.98) 0%, '
                    + 'rgba(' + rgb + ',0.88) 55%, rgba(' + rgb + ',0.55) 100%);'
                    + 'box-shadow:0 0 8px rgba(' + rgb + ',0.55), inset 0 0 0 2px rgba(255,255,255,0.18);'
                    + 'display:flex;align-items:center;justify-content:center;'
                    + 'color:#fff;font:600 ' + Math.round(size / 2.6) + 'px system-ui,sans-serif">'
                    + n + '</div>',
                className: 'epi-cluster-icon',
                iconSize: [size, size],
            });
        },
    }).addTo(map);
}

let clusteringEnabled = loadSettings().clusteringEnabled !== false;  // default on
let epiCluster = _makeEpiCluster(clusteringEnabled ? 50 : 1);

// Swaps epiCluster for a freshly-built group at the new radius, carrying
// over every marker currently clustered. Markers popped directly onto the
// map (pinned/selected — see _pinMarker) aren't layers of epiCluster and are
// untouched by the swap, same as any other epiCluster.addLayer/removeLayer.
function _setClustering(enabled) {
    clusteringEnabled = enabled;
    saveSettings({ clusteringEnabled });
    const layers = epiCluster.getLayers();
    map.removeLayer(epiCluster);
    epiCluster = _makeEpiCluster(enabled ? 50 : 1);
    if (layers.length) { epiCluster.addLayers(layers); }
}
let sCoords = CONFIG.sCoords;
let lastFlyTs = null, lastFlyLat = null, lastFlyLon = null;
let selectedDetTs = null;
let _pulseIv = null, _pulsePhase = 0, _pulseTs = null;
let _lastDets = [];
let _pinnedMarker = null;

// True when the currently pinned marker was pulled out of epiCluster to
// force it visible — a marker still inside a collapsed cluster has no _map
// and openTooltip() silently no-ops on it. Popping it onto the map directly
// (instead of zooming to un-cluster it) shows marker + tooltip without
// changing the current view.
let _pinnedPopped = false;

function _pinMarker(m) {
    // Idempotent: flyToEpi and replay's step() both pin on the same
    // 'moveend' event, so a repeat call must not re-derive _pinnedPopped
    // from the now-already-popped state (it would read map.hasLayer(m) as
    // true and wrongly conclude nothing needs restoring to epiCluster).
    if (_pinnedMarker === m) { m.openTooltip(); return; }
    if (_pinnedMarker) { _unpinMarker(); }
    _pinnedMarker = m;
    // Capture origin before touching anything, then unconditionally land in
    // pinnedPane (map.removeLayer first, since addTo() is a no-op — pane
    // included — on a layer the map already considers added).
    _pinnedPopped = epiCluster.hasLayer(m);
    if (_pinnedPopped) { epiCluster.removeLayer(m); }
    if (map.hasLayer(m)) { map.removeLayer(m); }
    m.options.pane = 'pinnedPane';
    m.addTo(map);
    m.openTooltip();
}

function _unpinMarker() {
    if (_pinnedMarker) {
        _pinnedMarker.closeTooltip();
        map.removeLayer(_pinnedMarker);
        _pinnedMarker.options.pane = 'overlayPane';  // L.Path/circleMarker default
        if (_pinnedPopped) {
            epiCluster.addLayer(_pinnedMarker);
        } else {
            _pinnedMarker.addTo(map);
        }
        _pinnedMarker = null;
        _pinnedPopped = false;
    }
}

function _deselectDetection() {
    // selectedDetTs gets set whenever a new detection auto-flies in, or a
    // marker/row gets clicked, but nothing ever cleared it back to null —
    // applyMarkerSelection() dims every OTHER marker whenever anything is
    // selected (dimmed = !!selectedDetTs), so that fade became permanent
    // for the rest of the session once anything had ever been selected,
    // regardless of whether the user had since zoomed/panned away to look
    // at something else entirely. Clicking empty map space should reset
    // this the same way it already resets the pinned tooltip.
    if (selectedDetTs === null) { return; }
    selectedDetTs = null;
    applyRowSelection();
    applyMarkerSelection();
}

map.on('click', function () {
    _unpinMarker();
    _deselectDetection();
});


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
    _faultsBtn.textContent = '⚡ ' + t('btn_faults');
    _faultsBtn.style.opacity = _faultLoading ? '0.5' : '1';
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

    try {
        _faultsBtn.textContent = '⏳ ' + t('btn_faults_loading');
        const r = await fetch(CONFIG.faultGeojsonUrl || '/static/gem_active_faults.geojson');
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
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
        console.warn('Failed to load fault data:', e.message);
    }
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

    const clusterBtn = document.getElementById('cluster-toggle-btn');
    if (clusterBtn) {
        clusterBtn.addEventListener('click', function () {
            _setClustering(!clusteringEnabled);
            clusterBtn.style.color = clusteringEnabled ? '#3fb950' : '#6e7681';
            clusterBtn.style.borderColor = clusteringEnabled ? '#3fb950' : '#30363d';
        });
        clusterBtn.style.color = clusteringEnabled ? '#3fb950' : '#6e7681';
        clusterBtn.style.borderColor = clusteringEnabled ? '#3fb950' : '#30363d';
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
        detMarkers.forEach(function (entry) { epiCluster.removeLayer(entry.m); });
        detMarkers.length = 0;
        update();
    });
}());


// ── Language selector ─────────────────────────────────────────────────────────

function _renderCfgLine() {
    const cfgEl = document.getElementById('cfg');
    if (!cfgEl) { return; }
    cfgEl.textContent = t('cfg_text', {
        threshold: CONFIG.threshold,
        n: CONFIG.nConsensus,
        total: CONFIG.allStations,
        window: CONFIG.consensusWindow,
    });
}

window._onLangChange = function () {
    detMarkers.forEach(function (entry) { epiCluster.removeLayer(entry.m); });
    detMarkers.length = 0;
    _renderCfgLine();
    update();
};

_renderCfgLine();

(function () {
    const sel = document.getElementById('lang-sel');
    if (!sel) { return; }
    sel.value = currentLang();
    sel.addEventListener('change', function () {
        setLang(sel.value);
    });
}());


// ── Fullscreen ────────────────────────────────────────────────────────────────

const _fsBtn = document.getElementById('fs-btn');

function _applyFsMode(on) {
    document.body.classList.toggle('fs-mode', on);
    _fsBtn.textContent = on ? '✕' : '⛶';
    _fsBtn.title = on ? t('btn_fullscreen_exit_title') : t('btn_fullscreen_title');
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


// ── S-wave lead time ─────────────────────────────────────────────────────────
// P-waves outrun S-waves, so a P-wave-triggered detection gives real advance
// notice before the (usually more damaging) S-wave arrives. Vp/Vs ~= 1.73 is
// the standard Poisson-solid approximation — the app itself only models
// P-wave travel time (P_VEL_KM_S), so S-wave velocity is derived here, not
// read from a config value.
const VP_VS_RATIO = 1.73;

function _haversineKm(lat1, lon1, lat2, lon2) {
    const R = 6371.0;
    const toRad = function (d) { return d * Math.PI / 180; };
    const dphi = toRad(lat2 - lat1);
    const dlmb = toRad(lon2 - lon1);
    const a = Math.sin(dphi / 2) ** 2
        + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dlmb / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
}

function _sWaveLeadSeconds(det, epicLat, epicLon) {
    const pVel = CONFIG.pVelKmS || 8.0;
    const sVel = pVel / VP_VS_RATIO;
    let minDist = null;
    (det.stations || []).forEach(function (key) {
        const coord = sCoords[key];
        if (!coord) { return; }
        const d = _haversineKm(epicLat, epicLon, coord[0], coord[1]);
        if (minDist == null || d < minDist) { minDist = d; }
    });
    if (minDist == null) { return null; }
    return minDist * (1 / sVel - 1 / pVel);
}

function _fmtLead(seconds) {
    if (seconds < 60) { return seconds.toFixed(0) + 's'; }
    return (seconds / 60).toFixed(1) + ' min';
}


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

    // ── Pass 1: draw lines and collect label candidates ─────────────────────
    const _labelCandidates = [];
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

        const p1 = map.latLngToContainerPoint([coord[0], coord[1]]);
        const p2 = map.latLngToContainerPoint([epicLat, epicLon]);
        let ang = Math.atan2(p2.y - p1.y, p2.x - p1.x) * 180 / Math.PI;
        if (ang > 90)  { ang -= 180; }
        if (ang < -90) { ang += 180; }

        _labelCandidates.push({ key: key, coord: coord, label: label, ang: ang, p1: p1, p2: p2 });
    });

    // ── Pass 2: place labels with overlap resolution ───────────────────────────
    // Strategy: for each label try (t along line) × (perpendicular flip) until
    // the bounding box doesn't collide with already-placed labels.
    // t=0.2 means 20% of the way from epicenter toward the station.
    var _T_STEPS  = [0.20, 0.30, 0.40, 0.50];
    var _PERP_PX  = 15;   // pixels perpendicular to line for flip
    var _PERP_SEQ = [0, 1, -1];   // 0=on line, +1=left side, -1=right side
    var _LBL_W    = 92;   // approx label half-width in px (generous)
    var _LBL_H    = 8;    // approx label half-height in px

    function _lbbox(cx, cy, angDeg) {
        // Axis-aligned bounding box of a rotated label — conservative AABB
        var r = angDeg * Math.PI / 180;
        var hw = Math.abs(_LBL_W * Math.cos(r)) + Math.abs(_LBL_H * Math.sin(r));
        var hh = Math.abs(_LBL_W * Math.sin(r)) + Math.abs(_LBL_H * Math.cos(r));
        return { cx: cx, cy: cy, hw: hw, hh: hh };
    }

    function _bboxHit(a, b) {
        var PAD = 5;
        return Math.abs(a.cx - b.cx) < a.hw + b.hw + PAD
            && Math.abs(a.cy - b.cy) < a.hh + b.hh + PAD;
    }

    var _placedBoxes = [];

    _labelCandidates.forEach(function (c) {
        var chosen = null;
        var dx = c.p2.x - c.p1.x;
        var dy = c.p2.y - c.p1.y;
        var lineLen = Math.sqrt(dx * dx + dy * dy) || 1;
        var nx = -dy / lineLen;   // unit perpendicular (left)
        var ny =  dx / lineLen;

        outer:
        for (var ti = 0; ti < _T_STEPS.length; ti++) {
            var t = _T_STEPS[ti];
            for (var pi = 0; pi < _PERP_SEQ.length; pi++) {
                var ps = _PERP_SEQ[pi];

                // Base lat/lng along the line at parameter t
                var baseLat = epicLat + t * (c.coord[0] - epicLat);
                var baseLon = epicLon + t * (c.coord[1] - epicLon);
                var baseP   = map.latLngToContainerPoint([baseLat, baseLon]);

                // Apply perpendicular pixel offset
                var sx = baseP.x + ps * _PERP_PX * nx;
                var sy = baseP.y + ps * _PERP_PX * ny;

                var bbox = _lbbox(sx, sy, c.ang);
                var ok = true;
                for (var j = 0; j < _placedBoxes.length; j++) {
                    if (_bboxHit(bbox, _placedBoxes[j])) { ok = false; break; }
                }

                if (ok) {
                    var ll = map.containerPointToLatLng([sx, sy]);
                    chosen = { lat: ll.lat, lon: ll.lng, bbox: bbox, ang: c.ang };
                    break outer;
                }
            }
        }

        if (!chosen) {
            // No clean position found — fall back to t=0.2 on-line
            var fbLat = epicLat + 0.2 * (c.coord[0] - epicLat);
            var fbLon = epicLon + 0.2 * (c.coord[1] - epicLon);
            var fbP   = map.latLngToContainerPoint([fbLat, fbLon]);
            chosen = { lat: fbLat, lon: fbLon, bbox: _lbbox(fbP.x, fbP.y, c.ang), ang: c.ang };
        }

        _placedBoxes.push(chosen.bbox);

        var lm = L.marker([chosen.lat, chosen.lon], {
            interactive: false,
            icon: L.divIcon({
                className: '',
                iconSize: [400, 24],
                iconAnchor: [200, 12],
                html: '<div style="width:400px;height:24px;display:flex;align-items:center;justify-content:center;'
                    + 'transform:rotate(' + chosen.ang.toFixed(1) + 'deg);color:#c9d1d9;font-size:11px;font-weight:500;'
                    + 'letter-spacing:.3px;text-shadow:0 0 4px #0d1117,0 0 4px #0d1117,0 0 6px #0d1117;'
                    + 'white-space:nowrap;pointer-events:none">' + c.label + '</div>'
            })
        }).addTo(map);
        _epiLines.push(lm);
    });

    const initR = Math.max(0, (_serverNow() - det.unix_ts) * pVelMs);
    if (initR < 20100000) {
        const initFade = Math.max(0, 0.75 - (initR / 5000000) * 0.65);
        _pWaveCircle = L.circle([epicLat, epicLon], {
            radius: initR, color: '#f85149', weight: 1.5, fillOpacity: 0, opacity: initFade, interactive: false
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
}

function _scheduleEpiRedraw() {
    if (!_epiVizDet || _epiVizLat == null) { return; }
    clearTimeout(_epiRedrawTimer);
    _epiRedrawTimer = setTimeout(function () {
        _drawEpiViz(_epiVizDet, _epiVizLat, _epiVizLon);
    }, 80);
}

let _mapFlying = false;
map.on('zoomend', function () { if (!_mapFlying) { applyMarkerSelection(); } });
map.on('moveend',  function () { _scheduleEpiRedraw(); });


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

// Sequential single-hue ramp (magnitude = "how much" → one hue, light to dark),
// steps 100-700 from the dataviz skill's documented palette — not eyeballed.
const _MAG_RAMP = ['#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7',
                    '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b'];

function _hexToRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function _magColorRgb(mb) {
    const mbVal = (mb != null) ? mb : 4;  // 0 is a real (if unlikely) magnitude, not "missing"
    const t = Math.max(0, Math.min(1, (mbVal - 2) / 5));  // M2→0, M7→1
    const idx = t * (_MAG_RAMP.length - 1);
    const lo = Math.floor(idx), hi = Math.min(_MAG_RAMP.length - 1, lo + 1);
    const frac = idx - lo;
    const a = _hexToRgb(_MAG_RAMP[lo]), b = _hexToRgb(_MAG_RAMP[hi]);
    return [
        Math.round(a[0] + (b[0] - a[0]) * frac),
        Math.round(a[1] + (b[1] - a[1]) * frac),
        Math.round(a[2] + (b[2] - a[2]) * frac),
    ];
}

function _magColor(mb) {
    return 'rgb(' + _magColorRgb(mb).join(',') + ')';
}

// Fill = magnitude (sequential, redundant with radius so small size deltas
// still read at a glance). Border = confirmation status (solid vs dashed) —
// kept on its own channel instead of color so the two encodings don't collide.
function _markerBaseStyle(usgs, dimmed, mb) {
    const fill = _magColor(mb);
    if (usgs) {
        return { color: '#0d1117', weight: 1.5, fillColor: fill, fillOpacity: dimmed ? 0.35 : 0.9, dashArray: null };
    }
    return { color: '#8b949e', weight: 1, fillColor: fill, fillOpacity: dimmed ? 0.2 : 0.55, dashArray: '5,3' };
}

function applyMarkerSelection(skipPulse) {
    detMarkers.forEach(function (entry) {
        const { m, ts, r, usgs, mb } = entry;
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
            m.setStyle(_markerBaseStyle(usgs, !!selectedDetTs, mb));
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

function flyToEpi(lat, lon, ts, targetZoom) {
    _unpinMarker();
    selectedDetTs = ts || null;

    // Clear epi viz immediately; store new coords for post-land redraw
    _clearEpiViz();
    if (ts) {
        const det = _lastDets.find(function (d) { return d.ts === ts; });
        _epiVizDet  = det;
        _epiVizLat  = lat;
        _epiVizLon  = lon;
    } else {
        _epiVizDet = null;
        _epiVizLat = null;
        _epiVizLon = null;
    }

    applyRowSelection();

    if (ts) {
        const row = document.querySelector('.det[data-ts="' + CSS.escape(ts) + '"]');
        if (row) { row.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
    }

    // Defer marker re-coloring until after the animation lands so the new
    // selection doesn't visibly fly across the screen during the pan.
    if (_pulseIv) { clearInterval(_pulseIv); _pulseIv = null; _pulseTs = null; }
    _mapFlying = true;
    map.flyTo([lat, lon], targetZoom != null ? targetZoom : map.getZoom(), { duration: FLY_DURATION, easeLinearity: 0.5 });
    map.once('moveend', function () {
        _mapFlying = false;
        applyMarkerSelection();
        if (ts) {
            const entry = detMarkers.find(function (x) { return x.ts === ts; });
            if (entry) { _pinMarker(entry.m); }
        }
        if (_epiVizDet) {
            clearTimeout(_epiRedrawTimer);
            _drawEpiViz(_epiVizDet, _epiVizLat, _epiVizLon);
        }
    });

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
muteBtn.textContent = audioEnabled ? '🔔 ' + t('btn_mute_on') : '🔕 ' + t('btn_mute_off');
muteBtn.style.color = audioEnabled ? '#8b949e' : '#6e7681';

muteBtn.addEventListener('click', function () {
    audioEnabled = !audioEnabled;
    muteBtn.textContent = audioEnabled ? '🔔 ' + t('btn_mute_on') : '🔕 ' + t('btn_mute_off');
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
        if (perm !== 'granted') { notifBtn.title = t('notif_blocked'); return; }
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

// P-wave: sharp knock (swept tone 180→55Hz + high-freq click transient)
function _playPWave(ac) {
    const t = ac.currentTime;
    const osc = ac.createOscillator();
    const env = ac.createGain();
    osc.connect(env); env.connect(ac.destination);
    osc.frequency.setValueAtTime(180, t);
    osc.frequency.exponentialRampToValueAtTime(55, t + 0.3);
    env.gain.setValueAtTime(0.45, t);
    env.gain.exponentialRampToValueAtTime(0.001, t + 0.4);
    osc.start(t); osc.stop(t + 0.45);
    const click = ac.createOscillator();
    const clickEnv = ac.createGain();
    click.connect(clickEnv); clickEnv.connect(ac.destination);
    click.frequency.value = 700;
    clickEnv.gain.setValueAtTime(0.18, t);
    clickEnv.gain.exponentialRampToValueAtTime(0.001, t + 0.06);
    click.start(t); click.stop(t + 0.08);
}

// S-wave: deep sustained rumble (brown noise LPF + sawtooth body) — mb≥5
function _playSWave(ac) {
    const t = ac.currentTime;
    const sr = ac.sampleRate;
    const dur = 8.0;
    const frames = Math.ceil(sr * dur);
    const buf = ac.createBuffer(1, frames, sr);
    const d = buf.getChannelData(0);
    let prev = 0;
    for (let i = 0; i < frames; i++) {
        const white = Math.random() * 2 - 1;
        prev = (prev + 0.02 * white) / 1.02;
        d[i] = prev * 3.5;
    }
    const src = ac.createBufferSource();
    src.buffer = buf;
    const lpf = ac.createBiquadFilter();
    lpf.type = 'lowpass'; lpf.frequency.value = 90;
    const rumbleGain = ac.createGain();
    rumbleGain.gain.setValueAtTime(0.0, t);
    rumbleGain.gain.linearRampToValueAtTime(0.55, t + 1.2);
    rumbleGain.gain.setValueAtTime(0.50, t + 3.5);
    rumbleGain.gain.exponentialRampToValueAtTime(0.001, t + dur);
    src.connect(lpf); lpf.connect(rumbleGain); rumbleGain.connect(ac.destination);
    src.start(t); src.stop(t + dur);
    const body = ac.createOscillator();
    const bodyGain = ac.createGain();
    body.type = 'sawtooth';
    body.frequency.setValueAtTime(40, t);
    body.frequency.linearRampToValueAtTime(20, t + 5);
    bodyGain.gain.setValueAtTime(0.0, t);
    bodyGain.gain.linearRampToValueAtTime(0.22, t + 0.9);
    bodyGain.gain.exponentialRampToValueAtTime(0.001, t + 6);
    body.connect(bodyGain); bodyGain.connect(ac.destination);
    body.start(t); body.stop(t + 6.2);
}

// Consensus alert: double beep (1100Hz + 880Hz)
function _playDetectBeep(ac) {
    const t = ac.currentTime;
    [[0, 1100, 0.22], [0.12, 880, 0.16]].forEach(function (tone) {
        const osc = ac.createOscillator();
        const env = ac.createGain();
        osc.connect(env); env.connect(ac.destination);
        osc.type = 'sine'; osc.frequency.value = tone[1];
        env.gain.setValueAtTime(tone[2], t + tone[0]);
        env.gain.exponentialRampToValueAtTime(0.001, t + tone[0] + 0.1);
        osc.start(t + tone[0]); osc.stop(t + tone[0] + 0.14);
    });
}

function playDetectionAlert(mb) {
    if (!audioEnabled || !_notifMbOk(mb)) { return; }
    try {
        if (!audioCtx) { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
        if (audioCtx.state === 'suspended') { audioCtx.resume(); }
        _playDetectBeep(audioCtx);
        _playPWave(audioCtx);
        if (mb != null && mb >= 5.0) {
            setTimeout(function () {
                if (audioCtx.state === 'suspended') audioCtx.resume();
                _playSWave(audioCtx);
            }, 600);
        }
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

// Close-in view used to auto-zoom on each detection during replay — unless
// the user manually zooms mid-replay, in which case we back off and stop
// fighting them for the rest of that run (reset each time replay restarts).
const REPLAY_ZOOM = 7;
let _replayAutoZoomOverridden = false;
let _replayZoomAnimating = false;

map.on('zoomstart', function () {
    if (_replayActive && !_replayZoomAnimating) { _replayAutoZoomOverridden = true; }
});

const _mbPendingNotify = new Set();

function _replayStop() {
    if (_replayInterval) { clearTimeout(_replayInterval); _replayInterval = null; }
    _replayActive = false;
    _unpinMarker();
    const btn = document.getElementById('replay-btn');
    if (btn) { btn.textContent = '▶ ' + t('btn_replay'); }
    const cursor = document.getElementById('replay-cursor');
    if (cursor) { cursor.style.display = 'none'; }
}

function _positionReplayCursor(idx) {
    const cursor = document.getElementById('replay-cursor');
    if (!cursor) { return; }
    const max = Math.max(1, _replayRange.max);
    cursor.style.left = (idx / max * 100) + '%';
    cursor.style.display = 'block';
}

// Range state lives here, not on native <input> elements — the two-
// overlaid-range-inputs CSS trick this used to use is fiddly across
// browsers and was silently rendering with no visible thumbs at all.
// Plain divs positioned by percentage + pointer-event dragging instead.
const _replayRange = { start: 0, end: 0, max: 0 };
// Once the user drags either handle, stop auto-expanding the range to
// "everything" on each data refresh — respect their chosen window instead.
let _replayRangeTouched = false;

function _sortedReplayDets(dets) {
    return [...dets]
        .filter(function (d) { return !d.teleseismic && (d.epicenter || (d.usgs && d.usgs.lat != null)); })
        .sort(function (a, b) { return a.unix_ts - b.unix_ts; });
}

function _positionReplayThumbs() {
    const startThumb = document.getElementById('replay-start-thumb');
    const endThumb   = document.getElementById('replay-end-thumb');
    const fill       = document.getElementById('replay-fill');
    if (!startThumb || !endThumb) { return; }
    const max = Math.max(1, _replayRange.max);
    const pStart = (_replayRange.start / max) * 100;
    const pEnd   = (_replayRange.end / max) * 100;
    startThumb.style.left = pStart + '%';
    endThumb.style.left   = pEnd + '%';
    if (fill) {
        fill.style.left  = Math.min(pStart, pEnd) + '%';
        fill.style.width = Math.abs(pEnd - pStart) + '%';
    }
}

function _replayStart(dets) {
    if (!dets || !dets.length) { return; }
    _replayStop();

    const sorted = _sortedReplayDets(dets);
    if (!sorted.length) { return; }

    const lo = Math.min(_replayRange.start, _replayRange.end, sorted.length - 1);
    const hi = Math.min(Math.max(_replayRange.start, _replayRange.end), sorted.length - 1);

    _replayActive = true;
    _replayAutoZoomOverridden = false;
    let idx = lo;

    const btn = document.getElementById('replay-btn');
    if (btn) { btn.textContent = '⏹ ' + t('btn_replay_stop'); }

    function step() {
        if (!_replayActive) { return; }
        if (idx > hi) { _replayStop(); return; }

        _positionReplayCursor(idx);
        const det = sorted[idx++];
        const la  = det.usgs && det.usgs.lat != null ? det.usgs.lat  : det.epicenter[0];
        const lo2 = det.usgs && det.usgs.lon != null ? det.usgs.lon  : det.epicenter[1];

        _replayZoomAnimating = !_replayAutoZoomOverridden;
        flyToEpi(la, lo2, det.ts, _replayAutoZoomOverridden ? undefined : REPLAY_ZOOM);

        map.once('moveend', function () {
            _replayZoomAnimating = false;
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

function _previewReplayAt(idx) {
    const sorted = _sortedReplayDets(_currentFilteredDets);
    const det = sorted[idx];
    if (!det) { return; }
    selectedDetTs = det.ts;
    applyRowSelection();
    const row = document.querySelector('.det[data-ts="' + CSS.escape(det.ts) + '"]');
    if (row) { row.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    if (det.epicenter || (det.usgs && det.usgs.lat != null)) {
        const la = det.usgs && det.usgs.lat != null ? det.usgs.lat : det.epicenter[0];
        const lo = det.usgs && det.usgs.lon != null ? det.usgs.lon : det.epicenter[1];
        _mapFlying = true;
        map.flyTo([la, lo], map.getZoom(), { duration: 0.6, easeLinearity: 0.5 });
        map.once('moveend', function () { _mapFlying = false; applyMarkerSelection(); });
    }
}

function _initReplayControls() {
    const replayBtn = document.getElementById('replay-btn');
    if (replayBtn) {
        replayBtn.addEventListener('click', function () {
            if (_replayActive) { _replayStop(); } else { _replayStart(_currentFilteredDets); }
        });
    }

    const track = document.getElementById('replay-range');
    const startThumb = document.getElementById('replay-start-thumb');
    const endThumb   = document.getElementById('replay-end-thumb');
    if (!track || !startThumb || !endThumb) { return; }

    function idxFromClientX(clientX) {
        const rect = track.getBoundingClientRect();
        const frac = rect.width > 0 ? (clientX - rect.left) / rect.width : 0;
        return Math.round(Math.max(0, Math.min(1, frac)) * _replayRange.max);
    }

    function bindThumbDrag(thumb, key) {
        thumb.addEventListener('pointerdown', function (e) {
            e.preventDefault();
            thumb.setPointerCapture(e.pointerId);
            _replayRangeTouched = true;

            function onMove(ev) {
                const idx = idxFromClientX(ev.clientX);
                _replayRange[key] = idx;
                if (_replayRange.start > _replayRange.end) {
                    // Clamp against the other handle instead of letting them cross.
                    _replayRange[key === 'start' ? 'end' : 'start'] = idx;
                }
                _positionReplayThumbs();
                _previewReplayAt(idx);
            }
            function onUp(ev) {
                thumb.releasePointerCapture(e.pointerId);
                thumb.removeEventListener('pointermove', onMove);
                thumb.removeEventListener('pointerup', onUp);
            }
            thumb.addEventListener('pointermove', onMove);
            thumb.addEventListener('pointerup', onUp);
        });
    }

    bindThumbDrag(startThumb, 'start');
    bindThumbDrag(endThumb, 'end');
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
        return '<span class="chip chip-epi" title="Localization unreliable (high residual) — likely distant teleseismic source" style="opacity:.7">&#x1F310; ' + t('det_teleseismic') + '</span>';
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
            + fmtLocal(new Date(serverStart * 1000).toISOString()) + '">' + t('det_deployed', { label: deployLabel }) + '</div>';
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
        dDiv.innerHTML = '<div class="no-data">' + t('det_none') + '</div>';
        return;
    }
    if (!filteredDets.length) {
        dDiv.innerHTML = '<div class="no-data">' + t('det_none_filtered') + '</div>';
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
            epiCluster.removeLayer(entry.m);
            // _pinMarker can pop a marker directly onto the map (out of
            // epiCluster) so its tooltip shows without moving the view —
            // epiCluster.removeLayer() is a silent no-op for a marker in
            // that state, so without this it becomes a permanent orphan on
            // the map when its detection ages out or its epicenter moves.
            if (map.hasLayer(entry.m)) { map.removeLayer(entry.m); }
            if (_pinnedMarker === entry.m) { _pinnedMarker = null; _pinnedPopped = false; }
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
        const r  = Math.max(5, Math.min(20, (mb - 2) * 4 + 5));

        const mbLabel  = det.mb ? (det.mb_local ? 'local' : det.mb_approx ? 'mb~' + det.mb.toFixed(1) : 'mb=' + det.mb.toFixed(1)) : 'mb pending';
        const mbClass  = mb >= 5 ? 'high' : mb >= 4 ? 'mid' : 'low';
        const mbPx     = Math.max(13, Math.min(44, Math.round(13 + (mb - 2) * 5)));
        const locSrc   = usgsCoords ? (det.usgs.source === 'emsc' ? 'EMSC' : 'USGS') : 'sensor';
        const locStr   = locSrc + ': ' + Math.abs(la).toFixed(2) + '°' + (la >= 0 ? 'N' : 'S') + ' ' + Math.abs(lo).toFixed(2) + '°' + (lo >= 0 ? 'E' : 'W');
        const hasOrig  = usgsCoords && det.epicenter && det.epicenter[0] != null;
        const origLa   = hasOrig ? det.epicenter[0] : null;
        const origLo   = hasOrig ? det.epicenter[1] : null;
        const origStr  = hasOrig ? Math.abs(origLa).toFixed(2) + '°' + (origLa >= 0 ? 'N' : 'S') + ' ' + Math.abs(origLo).toFixed(2) + '°' + (origLo >= 0 ? 'E' : 'W') : '';
        const origLink = hasOrig
            ? '<div class="tip-loc-orig"><a href="#" onclick="event.preventDefault();event.stopPropagation();map.flyTo([' + origLa + ',' + origLo + '],map.getZoom(),{duration:1.0})">' + t('tip_sensor') + ': ' + origStr + '</a></div>'
            : '';

        const teleBadge = det.teleseismic ? '<span class="tip-badge tele">' + t('det_teleseismic') + '</span>' : '';
        const confVal   = det.conf != null ? det.conf.toFixed(3) : null;
        const confBadge = confVal ? '<span class="tip-badge conf-ok" title="PhaseNet consensus confidence">conf ' + confVal + '</span>' : '';
        const badges    = (teleBadge || confBadge) ? '<div class="tip-badges">' + teleBadge + confBadge + '</div>' : '';
        const placeRow  = usgsCoords && det.usgs.place
            ? '<div class="tip-place">' + det.usgs.place + '</div>'
            : '';

        const catMag  = usgsCoords && det.usgs.mag != null
            ? 'M' + det.usgs.mag.toFixed(1) + (det.usgs.magType && det.usgs.magType !== '?' ? det.usgs.magType : '')
            : null;
        const depthM  = usgsCoords && det.usgs.depth != null ? Math.abs(det.usgs.depth) : null;
        const depthStr = depthM != null ? depthM.toFixed(0) + ' km depth' : null;
        const catRow  = catMag
            ? '<div class="tip-row"><span class="tip-row-label">' + t('tip_catalog') + '</span><span class="tip-cat-val">' + catMag + (depthStr ? ' <span class="tip-depth">· ' + depthStr + '</span>' : '') + '</span></div>'
            : '';
        const stasRow = '<div class="tip-row"><span class="tip-row-label">' + t('tip_stations') + '</span><span class="tip-stas-val">' + det.stations.join(' · ') + '</span></div>';
        const locLabel = locSrc.toLowerCase() === 'sensor' ? t('tip_sensor') : locSrc.toLowerCase();
        const locRow  = '<div class="tip-row"><span class="tip-row-label">' + locLabel + '</span><span class="tip-loc-val">' + Math.abs(la).toFixed(2) + '°' + (la >= 0 ? 'N' : 'S') + ' ' + Math.abs(lo).toFixed(2) + '°' + (lo >= 0 ? 'E' : 'W') + '</span></div>';
        const sLeadS  = _sWaveLeadSeconds(det, la, lo);
        const leadRow = sLeadS != null
            ? '<div class="tip-row" title="Estimated time between the P-wave detection and S-wave arrival at the nearest firing station — Vp/Vs~1.73 approximation, not measured"><span class="tip-row-label">' + t('tip_swavelead') + '</span><span class="tip-loc-val">~' + _fmtLead(sLeadS) + '</span></div>'
            : '';
        const origRow = hasOrig
            ? '<div class="tip-row tip-loc-orig"><span class="tip-row-label">' + t('tip_sensor') + '</span><a href="#" onclick="event.preventDefault();event.stopPropagation();map.flyTo([' + origLa + ',' + origLo + '],map.getZoom(),{duration:1.0})">' + Math.abs(origLa).toFixed(2) + '°' + (origLa >= 0 ? 'N' : 'S') + ' ' + Math.abs(origLo).toFixed(2) + '°' + (origLo >= 0 ? 'E' : 'W') + '</a></div>'
            : '';

        const eid      = usgsCoords && det.usgs.event_id;
        const src      = usgsCoords ? (det.usgs.source === 'emsc' ? 'emsc' : 'usgs') : null;
        const eventUrl = eid && src === 'usgs'
            ? 'https://earthquake.usgs.gov/earthquakes/eventpage/' + eid
            : eid && src === 'emsc' ? 'https://www.seismicportal.eu/eventdetail.html?unid=' + eid : null;
        const footHtml = eventUrl
            ? '<div class="tip-foot"><div class="tip-link"><a href="' + eventUrl + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">↗ ' + src.toUpperCase() + ' event page</a></div></div>'
            : '';

        const tipHtml = '<div class="det-tip">'
            + '<div class="tip-head">'
            +   '<div class="tip-time">' + fmtLocal(det.ts) + ' · ' + fmtAge(det.unix_ts) + ' ago</div>'
            +   '<div class="tip-mag-row"><span class="tip-mb ' + mbClass + '" style="font-size:' + mbPx + 'px">' + mbLabel + '</span>' + badges + '</div>'
            +   placeRow
            + '</div>'
            + '<div class="tip-body">'
            +   catRow
            +   stasRow
            +   locRow
            +   leadRow
            +   origRow
            + '</div>'
            + footHtml
            + '</div>';

        const m = L.circleMarker([la, lo], Object.assign({ radius: zoomR(r) }, _markerBaseStyle(usgsCoords, false, mb)))
            .bindTooltip(tipHtml, { sticky: false, direction: 'top', className: 'det-tip' });
        // Plain property, not a Leaflet .options key — options can get
        // cloned/rebuilt by markercluster internals, which was silently
        // dropping a custom _mb option and making every cluster compute
        // the exact same fallback color regardless of what was inside it.
        m._detMb = mb;
        epiCluster.addLayer(m);

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

        kept.push({ m, ts: det.ts, r, lat: la, lon: lo, usgs: usgsCoords, mb });
    });

    detMarkers.length = 0;
    kept.forEach(function (x) { detMarkers.push(x); });
    if (markersChanged) { applyMarkerSelection(); }
}


// ── Main update ───────────────────────────────────────────────────────────────

let _failStreak = 0;
const _BOOT_OVERLAY_THRESHOLD = 2;

function _setBootOverlay(show) {
    const el = document.getElementById('boot-overlay');
    if (!el) { return; }
    if (show) { el.classList.add('visible'); } else { el.classList.remove('visible'); }
}

function _updateBootMsg(streak) {
    const msg = document.getElementById('boot-msg');
    if (!msg) { return; }
    msg.textContent = streak < 6 ? t('boot_connecting') : t('status_restarting');
}

// The detections list only grows on a real detection (at most one per
// ALERT_COOLDOWN, i.e. minutes apart) — re-fetching and re-rendering the
// whole (currently ~2MB) list every 3s tick was pure waste. Routine ticks
// ask for station liveness only (?full=0); a full fetch (with detections)
// happens immediately when detections_count changes, and otherwise every
// _FULL_POLL_EVERY ticks anyway to pick up in-place field updates on
// already-shown detections (mb refine, USGS confirmation) that don't
// change the count.
let _pollTick = 0;
const _FULL_POLL_EVERY = 10;
let _lastKnownDetCount = (window.SEISMIC_INITIAL_STATE && window.SEISMIC_INITIAL_STATE.detections)
    ? window.SEISMIC_INITIAL_STATE.detections.length : null;

function update() {
    _pollTick++;
    const needFull = _lastKnownDetCount === null || (_pollTick % _FULL_POLL_EVERY === 0);
    fetch(needFull ? '/api/state' : '/api/state?full=0').then(function (r) {
        if (!r.ok) { throw new Error('HTTP ' + r.status); }
        return r.json();
    }).then(function (d) {
        _failStreak = 0;
        _setBootOverlay(false);
        document.getElementById('status-dot').style.background = '';
        const newDetection = d.detections_count !== undefined && d.detections_count !== _lastKnownDetCount;
        if (newDetection) { _lastKnownDetCount = d.detections_count; }
        if (newDetection && !d.detections) {
            // A detection landed but this was a lightweight poll — go get it now
            // rather than waiting up to _FULL_POLL_EVERY ticks to show it.
            fetch('/api/state').then(function (r2) { return r2.json(); }).then(function (d2) {
                try { _updateBody(d2); } catch (e) { console.error('[seismic] update error:', e); }
            });
            return;
        }
        try { _updateBody(d); } catch (e) { console.error('[seismic] update error:', e); }
    }).catch(function () {
        _failStreak++;
        document.getElementById('status-dot').style.background = '#f85149';
        if (_failStreak >= _BOOT_OVERLAY_THRESHOLD) {
            _updateBootMsg(_failStreak);
            _setBootOverlay(true);
        }
    });
}

function _updateBody(d) {
    if (d.detections) { _lastDets = [...d.detections]; }

    const now       = new Date();
    const localStr  = now.toLocaleTimeString('en', { timeZone: _activeTz(), hour: '2-digit', minute: '2-digit', second: '2-digit' }) + ' ' + _tzAbbr();
    const utcStr    = now.toLocaleTimeString('en', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) + ' UTC';
    document.getElementById('last-update').textContent = t('updated_at', { local: localStr, utc: utcStr });

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

    const srcDets = d.detections || _lastDets;  // lightweight polls carry no detections payload
    const dets = [...srcDets].reverse();
    let filteredDets = dets;
    if (filterConfirmed) { filteredDets = filteredDets.filter(function (det) { return !!det.usgs; }); }
    if (filterLocal)     { filteredDets = filteredDets.filter(function (det) { return det.epicenter && !det.teleseismic; }); }
    if (filterMinMb > 0) { filteredDets = filteredDets.filter(function (det) { return det.mb != null && det.mb >= filterMinMb; }); }
    _currentFilteredDets = filteredDets;

    const cntEl = document.getElementById('det-count');
    const activeFilters = filterConfirmed || filterLocal || filterMinMb > 0;
    if (cntEl) {
        cntEl.textContent = activeFilters
            ? filteredDets.length + ' / ' + srcDets.length
            : (srcDets.length ? srcDets.length + ' total' : '');
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
        sumEl.textContent = age === '—'
            ? t('last_event_future', { mag: mbStr })
            : t('last_event', { mag: mbStr, age: age });
    }

    // Index space here must match what _replayStart/_previewReplayAt actually
    // index into — _sortedReplayDets(filteredDets), not filteredDets itself.
    // That helper drops teleseismic/no-location dets and re-sorts by time,
    // so using filteredDets.length as "max" let the slider pick indices past
    // the end of the real playback array, silently clamping start==end and
    // replaying just one detection. (Bug hit 2026-08-19: narrowing the range
    // made replay stop after the first fly-to.)
    _replayRange.max = Math.max(0, _sortedReplayDets(filteredDets).length - 1);
    if (!_replayActive) {
        if (!_replayRangeTouched) {
            // Default / still untouched: track the full available range.
            _replayRange.start = 0;
            _replayRange.end = _replayRange.max;
        } else {
            // User picked a sub-range — keep it, just clamp to the
            // (possibly still growing) available range.
            _replayRange.start = Math.min(_replayRange.start, _replayRange.max);
            _replayRange.end = Math.min(_replayRange.end, _replayRange.max);
        }
    }
    _positionReplayThumbs();
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
            _mapFlying = true;
            map.flyTo([la, lo], map.getZoom(), { duration: FLY_DURATION, easeLinearity: 0.5 });
            map.once('moveend', function () { _mapFlying = false; applyMarkerSelection(); });
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
            : ld.usgs_checked ? t('fs_no_match') : t('fs_pending');
        fsoDet.innerHTML = '<div style="color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">' + t('fs_latest') + '</div>'
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


// ── Scoreboard ───────────────────────────────────────────────────────────────

(function () {
    const scoreEl   = document.getElementById('scoreboard-content');
    const winBtns   = document.querySelectorAll('.score-win-btn');
    if (!scoreEl) { return; }

    let activeDays = 1;
    let activeSinceRestart = false;

    function pct(n, d) { return d > 0 ? Math.round(n / d * 100) : '—'; }
    function col(v)     { return v >= 80 ? '#3fb950' : v >= 60 ? '#d29922' : '#f85149'; }
    function bar(v)     {
        const c = col(v);
        return '<div style="height:3px;background:#21262d;border-radius:2px;margin:3px 0 4px">'
            + '<div style="width:' + Math.min(100, v) + '%;height:100%;background:' + c + ';border-radius:2px"></div></div>';
    }
    function metricRow(label, v, vc, frac) {
        return '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:2px">'
            + '<span style="color:#8b949e">' + label + '</span>'
            + '<span style="color:' + vc + ';font-weight:600">' + (typeof v === 'number' ? v + '%' : v) + '</span>'
            + '</div>'
            + (typeof v === 'number' ? bar(v) : '')
            + '<div style="color:#6e7681;font-size:10px;margin-bottom:6px">' + frac + '</div>';
    }

    function loadScoreboard() {
        scoreEl.style.opacity = '0.45';
        var qs = '?days=' + activeDays + (activeSinceRestart ? '&since_restart=1' : '');
        Promise.all([
            fetch('/api/scoreboard' + qs).then(function (r) { return r.json(); }),
            fetch('/api/recall' + qs).then(function (r) { return r.json(); })
        ]).then(function (results) {
            const sb = results[0], rc = results[1];
            const precV = pct(sb.confirmed || 0, sb.checked || 0);
            const recV  = pct(rc.true_positives || 0, rc.usgs_events || 0);
            const precC = typeof precV === 'number' ? col(precV) : '#6e7681';
            const recC  = typeof recV  === 'number' ? col(recV)  : '#6e7681';
            scoreEl.innerHTML =
                metricRow(t('score_precision'), precV, precC,
                    t('score_confirmed_frac', { n: sb.confirmed || 0, d: sb.checked || 0 }))
                + metricRow(t('score_recall'), recV, recC,
                    t('score_recall_frac', { n: rc.true_positives || 0, d: rc.usgs_events || 0 }))
                + '<a href="/api/scoreboard" target="_blank" style="color:#484f58;font-size:9px;text-decoration:none">'
                + t('score_raw') + ' ↗</a>';
            scoreEl.style.opacity = '1';
        }).catch(function () {
            scoreEl.innerHTML = '<span style="color:#484f58;font-size:10px">' + t('score_unavailable') + '</span>';
            scoreEl.style.opacity = '1';
        });
    }

    winBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
            activeDays = parseInt(btn.dataset.days, 10);
            activeSinceRestart = btn.dataset.sinceRestart === '1';
            winBtns.forEach(function (b) {
                const active = b === btn;
                b.style.color       = active ? '#58a6ff' : '#6e7681';
                b.style.borderColor = active ? '#58a6ff' : '#30363d';
            });
            loadScoreboard();
        });
    });

    loadScoreboard();
    setInterval(loadScoreboard, 60000);

    const _prevOnLangChange = window._onLangChange;
    window._onLangChange = function (lang) {
        if (_prevOnLangChange) { _prevOnLangChange(lang); }
        loadScoreboard();
    };
}());


// ── Startup ───────────────────────────────────────────────────────────────────

_initReplayControls();
if (window.SEISMIC_INITIAL_STATE) {
    try { _updateBody(window.SEISMIC_INITIAL_STATE); } catch (e) { console.error('[seismic] inline state error:', e); }
}
update();
setInterval(update, 3000);

// Deep link: ?det=<unix_ts> → highlight row and fly map
(function () {
    if (!_deepLinkTs) { return; }

    function syncFilterButtons() {
        const confBtn  = document.getElementById('filter-btn');
        const localBtn = document.getElementById('filter-local-btn');
        const mbSel    = document.getElementById('mb-filter-sel');
        if (confBtn)  { confBtn.style.color = filterConfirmed ? '#3fb950' : '#6e7681'; confBtn.style.borderColor = filterConfirmed ? '#3fb950' : '#30363d'; }
        if (localBtn) { localBtn.style.color = filterLocal ? '#d29922' : '#6e7681'; localBtn.style.borderColor = filterLocal ? '#d29922' : '#30363d'; }
        if (mbSel) {
            if (filterMinMb > 0) { mbSel.value = filterMinMb.toFixed(1); } else { mbSel.selectedIndex = 0; }
            mbSel.style.color = filterMinMb > 0 ? '#58a6ff' : '#8b949e';
            mbSel.style.borderColor = filterMinMb > 0 ? '#58a6ff' : '#30363d';
        }
    }

    setTimeout(syncFilterButtons, 100);

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
                        map.flyTo([ll.lat, ll.lng], map.getZoom(), { duration: 1.2, easeLinearity: 0.5 });
                    }
                }
                return;
            }
        }
        if (++attempts < 12) { setTimeout(tryHighlight, 600); }
    }

    setTimeout(tryHighlight, 800);
}());
