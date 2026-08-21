// Minimal, dependency-free i18n — no build step, matches the rest of this
// app (plain <script> tags, no bundler). A library like i18next expects
// component/template-driven rendering; most of this app's user-facing text
// is built directly in JS template literals (tooltips, detection rows), so
// a small custom t(key) that works the same whether called from a Jinja2
// template attribute or a JS string builder is a better fit than fighting
// a framework-shaped library for a non-framework app.
//
// Coverage: the header, panel titles, legend, primary controls, and
// detection-tooltip row labels — the highest-visibility UI chrome. Deeper
// strings (individual tooltip descriptions, timezone names, etc.) aren't
// covered yet; add more keys to TRANSLATIONS following the same pattern.
//
// Translation quality note: these are AI-produced, not reviewed by native
// speakers. Good enough for a v1, but worth a native-speaker pass before
// treating any of these as a "supported language" for real users — this
// app uses specific seismology terms (teleseismic, epicenter, consensus)
// that deserve accuracy beyond casual translation.

(function () {
    'use strict';

    const TRANSLATIONS = {
        en: {
            app_title: 'Seismic Sensor',
            boot_connecting: 'connecting to sensor…',
            status_connecting: 'connecting...',
            status_restarting: 'server restarting — waiting for boot…',
            nav_stations: 'Stations',
            nav_detections: 'Detections',
            nav_score: 'Score',
            btn_sat: 'sat',
            btn_sat_title: 'Toggle satellite imagery (ESRI World Imagery)',
            btn_faults: 'faults',
            btn_faults_title: 'Toggle active fault overlay (GEM Global Active Faults)',
            btn_faults_loading: 'loading...',
            notif_blocked: 'Browser blocked notifications',
            btn_mute_on: 'on',
            btn_mute_off: 'off',
            btn_mute_title: 'Toggle audio alerts',
            btn_notif: 'notif',
            btn_notif_title: 'Toggle desktop notifications',
            btn_replay: 'replay',
            btn_replay_stop: 'stop',
            btn_replay_title: 'Replay detections on map in chronological order',
            btn_local: 'local',
            btn_local_title: 'Show only localized (non-teleseismic) detections',
            btn_conf: 'conf',
            btn_conf_title: 'Show confirmed catalog matches only',
            btn_showolder: 'show older',
            btn_fullscreen_title: 'Toggle fullscreen map',
            btn_fullscreen_exit_title: 'Exit fullscreen',
            sel_mb_title: 'Minimum magnitude filter',
            sel_mb_any: 'any mb',
            sel_notif_mb_title: 'Minimum magnitude to trigger alerts',
            sel_notif_mb_all: 'all mb',
            sel_tz_title: 'Display timezone',
            sta_resize_title: 'Drag to resize stations panel',
            sta_sort_title: 'Toggle station sort order',
            legend_catalog: 'Catalog verified (USGS/EMSC)',
            legend_sensor: 'Sensor estimate only',
            legend_selected: 'Selected',
            legend_station: 'Station',
            det_none: 'No detections yet',
            det_none_filtered: 'No detections match the current filters',
            det_teleseismic: 'teleseismic',
            tip_catalog: 'catalog',
            tip_stations: 'stations',
            tip_swavelead: 'S-wave lead',
            tip_sensor: 'sensor',
            fs_latest: 'Latest Detection',
            fs_no_match: 'no catalog match',
            fs_pending: 'catalog pending',
            score_unavailable: 'unavailable',
            cfg_text: 'threshold {threshold} | {n}/{total} consensus | {window}s window',
            last_event: 'Last: {mag} · {age} ago',
            last_event_future: 'Last: {mag} · future?',
            updated_at: 'updated {local} ({utc})',
            score_precision: 'precision',
            score_recall: 'recall',
            score_confirmed_frac: '{n}/{d} detections confirmed',
            score_recall_frac: '{n}/{d} USGS events caught',
            score_raw: 'raw',
            score_since_restart: 'restart',
            det_deployed: 'deployed {label}',
            lang_label: 'Language',
        },
        es: {
            app_title: 'Sensor Sísmico',
            boot_connecting: 'conectando al sensor…',
            status_connecting: 'conectando...',
            status_restarting: 'reiniciando servidor — esperando arranque…',
            nav_stations: 'Estaciones',
            nav_detections: 'Detecciones',
            nav_score: 'Puntuación',
            btn_sat: 'satélite',
            btn_sat_title: 'Activar imágenes satelitales (ESRI World Imagery)',
            btn_faults: 'fallas',
            btn_faults_title: 'Activar capa de fallas activas (GEM Global Active Faults)',
            btn_faults_loading: 'cargando...',
            notif_blocked: 'El navegador bloqueó las notificaciones',
            btn_mute_on: 'activo',
            btn_mute_off: 'silenciado',
            btn_mute_title: 'Activar/desactivar alertas de audio',
            btn_notif: 'notif',
            btn_notif_title: 'Activar/desactivar notificaciones de escritorio',
            btn_replay: 'repetir',
            btn_replay_stop: 'detener',
            btn_replay_title: 'Repetir detecciones en el mapa en orden cronológico',
            btn_local: 'local',
            btn_local_title: 'Mostrar solo detecciones localizadas (no telesísmicas)',
            btn_conf: 'confirmado',
            btn_conf_title: 'Mostrar solo coincidencias confirmadas por catálogo',
            btn_showolder: 'mostrar anteriores',
            btn_fullscreen_title: 'Alternar mapa a pantalla completa',
            btn_fullscreen_exit_title: 'Salir de pantalla completa',
            sel_mb_title: 'Filtro de magnitud mínima',
            sel_mb_any: 'cualquier mb',
            sel_notif_mb_title: 'Magnitud mínima para activar alertas',
            sel_notif_mb_all: 'todas mb',
            sel_tz_title: 'Zona horaria mostrada',
            sta_resize_title: 'Arrastrar para redimensionar el panel de estaciones',
            sta_sort_title: 'Alternar orden de estaciones',
            legend_catalog: 'Verificado por catálogo (USGS/EMSC)',
            legend_sensor: 'Solo estimación del sensor',
            legend_selected: 'Seleccionado',
            legend_station: 'Estación',
            det_none: 'Aún no hay detecciones',
            det_none_filtered: 'Ninguna detección coincide con los filtros actuales',
            det_teleseismic: 'telesísmico',
            tip_catalog: 'catálogo',
            tip_stations: 'estaciones',
            tip_swavelead: 'Adelanto onda S',
            tip_sensor: 'sensor',
            fs_latest: 'Última Detección',
            fs_no_match: 'sin coincidencia en catálogo',
            fs_pending: 'catálogo pendiente',
            score_unavailable: 'no disponible',
            cfg_text: 'umbral {threshold} | {n}/{total} consenso | ventana {window}s',
            last_event: 'Último: {mag} · hace {age}',
            last_event_future: 'Último: {mag} · ¿futuro?',
            updated_at: 'actualizado {local} ({utc})',
            score_precision: 'precisión',
            score_recall: 'sensibilidad',
            score_confirmed_frac: '{n}/{d} detecciones confirmadas',
            score_recall_frac: '{n}/{d} eventos USGS detectados',
            score_raw: 'crudo',
            score_since_restart: 'reinicio',
            det_deployed: 'desplegado {label}',
            lang_label: 'Idioma',
        },
        fr: {
            app_title: 'Capteur Sismique',
            boot_connecting: 'connexion au capteur…',
            status_connecting: 'connexion...',
            status_restarting: 'redémarrage du serveur — en attente…',
            nav_stations: 'Stations',
            nav_detections: 'Détections',
            nav_score: 'Score',
            btn_sat: 'satellite',
            btn_sat_title: 'Activer l\'imagerie satellite (ESRI World Imagery)',
            btn_faults: 'failles',
            btn_faults_title: 'Activer la couche de failles actives (GEM Global Active Faults)',
            btn_faults_loading: 'chargement...',
            notif_blocked: 'Le navigateur a bloqué les notifications',
            btn_mute_on: 'actif',
            btn_mute_off: 'muet',
            btn_mute_title: 'Activer/désactiver les alertes audio',
            btn_notif: 'notif',
            btn_notif_title: 'Activer/désactiver les notifications du bureau',
            btn_replay: 'relecture',
            btn_replay_stop: 'arrêter',
            btn_replay_title: 'Relire les détections sur la carte par ordre chronologique',
            btn_local: 'local',
            btn_local_title: 'Afficher uniquement les détections localisées (non télésismiques)',
            btn_conf: 'confirmé',
            btn_conf_title: 'Afficher uniquement les correspondances confirmées par catalogue',
            btn_showolder: 'afficher plus ancien',
            btn_fullscreen_title: 'Basculer la carte en plein écran',
            btn_fullscreen_exit_title: 'Quitter le plein écran',
            sel_mb_title: 'Filtre de magnitude minimale',
            sel_mb_any: 'mb quelconque',
            sel_notif_mb_title: 'Magnitude minimale pour déclencher les alertes',
            sel_notif_mb_all: 'toutes mb',
            sel_tz_title: 'Fuseau horaire affiché',
            sta_resize_title: 'Glisser pour redimensionner le panneau des stations',
            sta_sort_title: 'Basculer l\'ordre de tri des stations',
            legend_catalog: 'Vérifié par catalogue (USGS/EMSC)',
            legend_sensor: 'Estimation du capteur uniquement',
            legend_selected: 'Sélectionné',
            legend_station: 'Station',
            det_none: 'Aucune détection pour l\'instant',
            det_none_filtered: 'Aucune détection ne correspond aux filtres actuels',
            det_teleseismic: 'télésismique',
            tip_catalog: 'catalogue',
            tip_stations: 'stations',
            tip_swavelead: 'Avance onde S',
            tip_sensor: 'capteur',
            fs_latest: 'Dernière Détection',
            fs_no_match: 'aucune correspondance au catalogue',
            fs_pending: 'catalogue en attente',
            score_unavailable: 'indisponible',
            cfg_text: 'seuil {threshold} | {n}/{total} consensus | fenêtre {window}s',
            last_event: 'Dernier : {mag} · il y a {age}',
            last_event_future: 'Dernier : {mag} · futur ?',
            updated_at: 'mis à jour {local} ({utc})',
            score_precision: 'précision',
            score_recall: 'rappel',
            score_confirmed_frac: '{n}/{d} détections confirmées',
            score_recall_frac: '{n}/{d} événements USGS détectés',
            score_raw: 'brut',
            score_since_restart: 'redémarrage',
            det_deployed: 'déployé {label}',
            lang_label: 'Langue',
        },
        de: {
            app_title: 'Seismischer Sensor',
            boot_connecting: 'Verbindung zum Sensor…',
            status_connecting: 'verbinde...',
            status_restarting: 'Server startet neu — warte auf Start…',
            nav_stations: 'Stationen',
            nav_detections: 'Erkennungen',
            nav_score: 'Bewertung',
            btn_sat: 'Satellit',
            btn_sat_title: 'Satellitenbilder umschalten (ESRI World Imagery)',
            btn_faults: 'Verwerfungen',
            btn_faults_title: 'Aktive Verwerfungslinien umschalten (GEM Global Active Faults)',
            btn_faults_loading: 'lädt...',
            notif_blocked: 'Browser hat Benachrichtigungen blockiert',
            btn_mute_on: 'an',
            btn_mute_off: 'aus',
            btn_mute_title: 'Audioalarme umschalten',
            btn_notif: 'Hinweis',
            btn_notif_title: 'Desktop-Benachrichtigungen umschalten',
            btn_replay: 'Wiedergabe',
            btn_replay_stop: 'stopp',
            btn_replay_title: 'Erkennungen auf der Karte chronologisch abspielen',
            btn_local: 'lokal',
            btn_local_title: 'Nur lokalisierte (nicht-teleseismische) Erkennungen anzeigen',
            btn_conf: 'bestätigt',
            btn_conf_title: 'Nur bestätigte Katalogtreffer anzeigen',
            btn_showolder: 'ältere anzeigen',
            btn_fullscreen_title: 'Kartenvollbild umschalten',
            btn_fullscreen_exit_title: 'Vollbild beenden',
            sel_mb_title: 'Mindestmagnituden-Filter',
            sel_mb_any: 'jede mb',
            sel_notif_mb_title: 'Mindestmagnitude für Alarme',
            sel_notif_mb_all: 'alle mb',
            sel_tz_title: 'Angezeigte Zeitzone',
            sta_resize_title: 'Ziehen, um das Stationsfeld zu skalieren',
            sta_sort_title: 'Sortierreihenfolge der Stationen umschalten',
            legend_catalog: 'Katalog verifiziert (USGS/EMSC)',
            legend_sensor: 'Nur Sensorschätzung',
            legend_selected: 'Ausgewählt',
            legend_station: 'Station',
            det_none: 'Noch keine Erkennungen',
            det_none_filtered: 'Keine Erkennungen entsprechen den aktuellen Filtern',
            det_teleseismic: 'teleseismisch',
            tip_catalog: 'Katalog',
            tip_stations: 'Stationen',
            tip_swavelead: 'S-Wellen-Vorsprung',
            tip_sensor: 'Sensor',
            fs_latest: 'Letzte Erkennung',
            fs_no_match: 'kein Katalogtreffer',
            fs_pending: 'Katalog ausstehend',
            score_unavailable: 'nicht verfügbar',
            cfg_text: 'Schwelle {threshold} | {n}/{total} Konsens | {window}s Fenster',
            last_event: 'Letztes: {mag} · vor {age}',
            last_event_future: 'Letztes: {mag} · Zukunft?',
            updated_at: 'aktualisiert {local} ({utc})',
            score_precision: 'Präzision',
            score_recall: 'Trefferquote',
            score_confirmed_frac: '{n}/{d} Erkennungen bestätigt',
            score_recall_frac: '{n}/{d} USGS-Ereignisse erfasst',
            score_raw: 'roh',
            score_since_restart: 'Neustart',
            det_deployed: 'bereitgestellt {label}',
            lang_label: 'Sprache',
        },
        ja: {
            app_title: '地震センサー',
            boot_connecting: 'センサーに接続中…',
            status_connecting: '接続中...',
            status_restarting: 'サーバー再起動中 — 起動を待っています…',
            nav_stations: '観測点',
            nav_detections: '検知',
            nav_score: 'スコア',
            btn_sat: '衛星',
            btn_sat_title: '衛星画像の切替 (ESRI World Imagery)',
            btn_faults: '断層',
            btn_faults_title: '活断層レイヤーの切替 (GEM Global Active Faults)',
            btn_faults_loading: '読み込み中...',
            notif_blocked: 'ブラウザが通知をブロックしました',
            btn_mute_on: 'オン',
            btn_mute_off: 'オフ',
            btn_mute_title: '音声アラートの切替',
            btn_notif: '通知',
            btn_notif_title: 'デスクトップ通知の切替',
            btn_replay: 'リプレイ',
            btn_replay_stop: '停止',
            btn_replay_title: '検知を地図上で時系列に再生',
            btn_local: 'ローカル',
            btn_local_title: '局地(遠地地震以外)の検知のみ表示',
            btn_conf: '確認済',
            btn_conf_title: 'カタログで確認済みの検知のみ表示',
            btn_showolder: '過去の検知を表示',
            btn_fullscreen_title: '地図を全画面表示',
            btn_fullscreen_exit_title: '全画面表示を終了',
            sel_mb_title: '最小マグニチュードフィルター',
            sel_mb_any: 'すべて',
            sel_notif_mb_title: 'アラート発生の最小マグニチュード',
            sel_notif_mb_all: 'すべて',
            sel_tz_title: '表示タイムゾーン',
            sta_resize_title: 'ドラッグして観測点パネルのサイズを変更',
            sta_sort_title: '観測点の並び替え順を切替',
            legend_catalog: 'カタログ確認済み (USGS/EMSC)',
            legend_sensor: 'センサー推定のみ',
            legend_selected: '選択中',
            legend_station: '観測点',
            det_none: '検知はまだありません',
            det_none_filtered: '現在のフィルターに一致する検知はありません',
            det_teleseismic: '遠地地震',
            tip_catalog: 'カタログ',
            tip_stations: '観測点',
            tip_swavelead: 'S波先行時間',
            tip_sensor: 'センサー',
            fs_latest: '最新の検知',
            fs_no_match: 'カタログに一致なし',
            fs_pending: 'カタログ確認待ち',
            score_unavailable: '利用不可',
            cfg_text: '閾値 {threshold} | {n}/{total} 合意 | {window}秒ウィンドウ',
            last_event: '最新: {mag} · {age}前',
            last_event_future: '最新: {mag} · 未来?',
            updated_at: '更新 {local} ({utc})',
            score_precision: '適合率',
            score_recall: '再現率',
            score_confirmed_frac: '{n}/{d} 件の検知を確認',
            score_recall_frac: '{n}/{d} 件のUSGSイベントを検出',
            score_raw: '生データ',
            score_since_restart: '再起動',
            det_deployed: 'デプロイ {label}',
            lang_label: '言語',
        },
    };

    function _detectLang() {
        try {
            const saved = localStorage.getItem('seismic_lang');
            if (saved && TRANSLATIONS[saved]) { return saved; }
        } catch (e) { /* localStorage unavailable — ignore, fall through */ }
        const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
        return TRANSLATIONS[nav] ? nav : 'en';
    }

    let _lang = _detectLang();

    function t(key, vars) {
        let str = (TRANSLATIONS[_lang] && TRANSLATIONS[_lang][key]) || TRANSLATIONS.en[key] || key;
        if (vars) {
            Object.keys(vars).forEach(function (k) {
                str = str.replace('{' + k + '}', vars[k]);
            });
        }
        return str;
    }

    function currentLang() { return _lang; }

    function availableLangs() { return Object.keys(TRANSLATIONS); }

    function applyStaticI18n() {
        document.querySelectorAll('[data-i18n]').forEach(function (el) {
            el.textContent = t(el.getAttribute('data-i18n'));
        });
        document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
            el.title = t(el.getAttribute('data-i18n-title'));
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
            el.placeholder = t(el.getAttribute('data-i18n-placeholder'));
        });
    }

    function setLang(lang) {
        if (!TRANSLATIONS[lang]) { return; }
        _lang = lang;
        try { localStorage.setItem('seismic_lang', lang); } catch (e) { /* ignore */ }
        applyStaticI18n();
        if (typeof window._onLangChange === 'function') { window._onLangChange(lang); }
    }

    window.t = t;
    window.setLang = setLang;
    window.currentLang = currentLang;
    window.availableLangs = availableLangs;
    window.applyStaticI18n = applyStaticI18n;

    document.addEventListener('DOMContentLoaded', function () {
        applyStaticI18n();
        document.documentElement.classList.add('i18n-ready');
    });
})();
