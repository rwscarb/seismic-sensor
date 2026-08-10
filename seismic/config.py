import os
import time
import warnings

warnings.filterwarnings('ignore')

SERVER_START_TIME = time.time()  # recorded once at process start; used as deploy boundary in UI

# ── Config from env ────────────────────────────────────────────────────────────
CHECKPOINT_DIR       = os.environ.get('CHECKPOINT_DIR', './checkpoints')
SEEDLINK_SERVER      = os.environ.get('SEEDLINK_SERVER', 'geofon.gfz-potsdam.de:18000')
STATIONS_RAW         = os.environ.get('STATIONS', 'GE.APE,GE.MORC,GE.BORG,GE.KBS')
IRIS_SERVER          = os.environ.get('IRIS_SERVER', 'rtserve.iris.washington.edu:18000')
IRIS_STATIONS_RAW    = os.environ.get('IRIS_STATIONS', '')  # e.g. "IU.COR,CN.PGC,IU.KDAK"
CHANNELS             = os.environ.get('CHANNELS', 'HHZ,HHN,HHE').split(',')
THRESHOLD            = float(os.environ.get('THRESHOLD', '0.835'))
N_SEEDS              = int(os.environ.get('N_SEEDS', '3'))
ALERT_COOLDOWN       = float(os.environ.get('ALERT_COOLDOWN', '60.0'))
N_CONSENSUS          = int(os.environ.get('N_CONSENSUS', '2'))
CONSENSUS_WINDOW     = float(os.environ.get('CONSENSUS_WINDOW', '120.0'))
P_VEL_KM_S          = float(os.environ.get('P_VEL_KM_S', '8.0'))   # teleseismic P-wave speed
LOC_MIN_STA          = int(os.environ.get('LOC_MIN_STA', '3'))       # stations needed for location
P_LEAD_S             = float(os.environ.get('P_LEAD_S', '0.4'))      # model's pre-P horizon
WEB_PORT             = int(os.environ.get('WEB_PORT', '8080'))
TUI_MODE             = os.environ.get('TUI', '').lower() in ('1', 'true', 'yes')
TEMP_SCALE           = float(os.environ.get('TEMP_SCALE', '1.0'))   # temperature for classifier calibration
USGS_MIN_MAG         = float(os.environ.get('USGS_MIN_MAG', '4.0'))  # min magnitude for USGS catalog lookup
EMSC_MIN_MAG         = float(os.environ.get('EMSC_MIN_MAG', '2.0'))  # min magnitude for EMSC fallback lookup
SLACK_WEBHOOK_URL    = os.environ.get('SLACK_WEBHOOK_URL', '')        # optional: post detection alerts to Slack
UMAMI_SITE_ID        = os.environ.get('UMAMI_SITE_ID', '')            # optional: Umami analytics website ID
DETECTIONS_PATH      = os.environ.get('DETECTIONS_PATH', '/data/detections.json')
USGS_POLL_INTERVAL   = float(os.environ.get('USGS_POLL_INTERVAL', '600'))   # seconds between significant-event polls
USGS_SIG_MIN_MAG     = float(os.environ.get('USGS_SIG_MIN_MAG', '5.5'))    # min mag to flag as significant global event
# ±s around expected P-arrival to claim a match (5 min covers model error at teleseismic distances)
TELE_MATCH_WINDOW    = float(os.environ.get('TELE_MATCH_WINDOW', '300.0'))
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')           # Slack app signing secret


# Parse stations: "GE.APE,GE.MORC" → [('GE','APE'), ('GE','MORC')]
def _parse_stations(raw):
    result = []
    for s in raw.split(','):
        s = s.strip()
        if not s:
            continue
        if '.' in s:
            net, sta = s.split('.', 1)
            result.append((net.strip(), sta.strip()))
        else:
            result.append(('GE', s))
    return result


STATIONS      = _parse_stations(STATIONS_RAW)
IRIS_STATIONS = _parse_stations(IRIS_STATIONS_RAW)
ALL_STATIONS  = STATIONS + IRIS_STATIONS   # full combined list for ring init / coord fetch

DEVICE           = 'cpu'
K                = 128
CYCLES           = 1
WIN_SAMPLES      = 100
STRIDE           = 10
TARGET_SRATE     = 100.0
BUF_DECAY        = 0.876
BUF_STRENGTH     = 1.429
MAG_MAX_CREDIBLE = 7.5   # regression head saturates above this; suppress display
MB_DELAY_S       = 15.0  # wait after detection before computing mb (P coda fill)
MB_WIN_S         = 10.0  # P-wave measurement window length for mb


def fmt_mag(mag_est):
    if mag_est > MAG_MAX_CREDIBLE:
        return "---"
    return f"M{max(-2.0, mag_est):.1f}"
