#!/usr/bin/env python3
"""Wake System - personal alarm for one Windows laptop.

Backend owns reliability: scheduling, escalating sound (winsound), TTS voice,
dismissal tasks, missed-alarm detection and Windows integration calls.
The browser UI is a client only; audio never depends on it.
"""
import ctypes
import json
import math
import os
import queue
import random
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')
HISTORY_PATH = os.path.join(APP_DIR, 'history.json')
PORT_PATH = os.path.join(APP_DIR, 'port.txt')
LOG_PATH = os.path.join(APP_DIR, 'server.log')
PATHS_PATH = os.path.join(APP_DIR, '_paths.json')
SOUND_DIR = os.path.join(APP_DIR, 'sounds')
WIN_SCRIPT = os.path.join(APP_DIR, 'win_integration.ps1')
WAKE_TASK = os.path.join(APP_DIR, 'wake_task.py')
SLEEP_PENDING = os.path.join(APP_DIR, 'sleep_test_pending.json')
SLEEP_RESULT = os.path.join(APP_DIR, 'sleep_test_result.json')
VOLUME_BACKUP = os.path.join(APP_DIR, 'volume_backup.json')
PORT = 8520
MAX_LOG = 1_000_000
MAX_HISTORY = 400

IS_WIN = sys.platform.startswith('win')
if IS_WIN:
    import winsound

# The server runs under pythonw (no console), so any console child process
# would otherwise get a fresh console window. Keep shell-outs invisible.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if IS_WIN else 0

# ------------------------------------------------------------------ logging
_log_lock = threading.Lock()


def log(msg):
    line = '%s %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with _log_lock:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG:
                os.replace(LOG_PATH, LOG_PATH + '.old')
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass
    print(line, flush=True)


# ------------------------------------------------------------- sound design
SR = 22050


def _render(path, dur, notes, amp):
    """notes: list of (start, length, freq, amp_mult, [(harmonic, weight), ...])"""
    total = int(dur * SR)
    buf = [0.0] * total
    for (st, ln, fr, am, harm) in notes:
        n0 = int(st * SR)
        n1 = min(total, int((st + ln) * SR))
        for i in range(max(0, n0), n1):
            t = (i - n0) / SR
            atk = min(1.0, t / 0.006)
            dec = math.exp(-3.2 * t / ln)
            rel = min(1.0, (ln - t) / 0.03)
            env = atk * dec * rel
            s = 0.0
            for (m, w) in harm:
                s += w * math.sin(2 * math.pi * fr * m * t)
            buf[i] += s * env * am * amp
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = bytearray()
        for v in buf:
            if v > 0.98:
                v = 0.98
            elif v < -0.98:
                v = -0.98
            frames += struct.pack('<h', int(v * 32000))
        w.writeframes(bytes(frames))


def ensure_sounds():
    os.makedirs(SOUND_DIR, exist_ok=True)
    plain = [(1, 1.0)]
    warm = [(1, 1.0), (2, 0.25)]
    rich = [(1, 1.0), (2, 0.35), (3, 0.12)]
    specs = {
        # Stage 1: soft two-note chime (gentle, noticeable)
        'stage1.wav': (2.0, [
            (0.00, 0.9, 784.0, 0.55, warm),
            (1.00, 0.9, 1046.5, 0.45, warm),
        ], 0.22),
        # Stage 2: alternating two-tone melody (prevents habituation)
        'stage2.wav': (2.0, [
            (0.00, 0.22, 659.3, 0.9, rich), (0.30, 0.22, 880.0, 0.9, rich),
            (0.60, 0.22, 659.3, 0.9, rich), (0.90, 0.22, 987.8, 1.0, rich),
            (1.20, 0.22, 880.0, 0.9, rich), (1.50, 0.35, 1046.5, 1.0, rich),
        ], 0.40),
        # Stage 3: urgent triple beeps
        'stage3.wav': (2.0, [
            (0.00, 0.14, 987.8, 1.0, rich), (0.22, 0.14, 987.8, 1.0, rich),
            (0.44, 0.14, 987.8, 1.0, rich),
            (1.00, 0.14, 987.8, 1.0, rich), (1.22, 0.14, 987.8, 1.0, rich),
            (1.44, 0.14, 987.8, 1.0, rich),
        ], 0.75),
        # Stage 4: same urgency + low undertone (distinctive, louder)
        'stage4.wav': (2.0, [
            (0.00, 0.5, 246.9, 0.5, plain), (1.00, 0.5, 246.9, 0.5, plain),
            (0.00, 0.14, 1046.5, 1.0, rich), (0.22, 0.14, 1046.5, 1.0, rich),
            (0.44, 0.14, 1046.5, 1.0, rich),
            (1.00, 0.14, 1046.5, 1.0, rich), (1.22, 0.14, 1046.5, 1.0, rich),
            (1.44, 0.14, 1046.5, 1.0, rich),
        ], 0.85),
    }
    for name, (dur, notes, amp) in specs.items():
        path = os.path.join(SOUND_DIR, name)
        need = not os.path.exists(path) or os.path.getsize(path) < 1000
        if need:
            try:
                _render(path, dur, notes, amp)
                log('generated %s' % name)
            except Exception as e:
                log('sound gen failed for %s: %s' % (name, e))


class SoundPlayer:
    """Loops a WAV via winsound; stop() purges immediately when possible."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._current = None

    def play(self, path):
        with self._lock:
            self._stop.set()
            old = self._thread
            self._stop.clear()
            self._current = path

            def run():
                while not self._stop.is_set():
                    try:
                        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
                    except Exception as e:
                        log('play error: %s' % e)
                        time.sleep(1.0)
                    if self._stop.is_set():
                        break

            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
            if old and old.is_alive() and old is not threading.current_thread():
                old.join(timeout=3)
            log('sound: playing %s' % os.path.basename(path))

    def stop(self):
        with self._lock:
            self._stop.set()
            t0 = time.time()
            try:
                if IS_WIN:
                    winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            t = self._thread
            if t and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=3)
            if self._current:
                log('sound: stopped after %.2fs' % (time.time() - t0))
                self._current = None

    def playing(self):
        t = self._thread
        return bool(t and t.is_alive() and not self._stop.is_set())


# ------------------------------------------------------------------- voices
_voice_lock = threading.Lock()
_voice_procs = []


def speak(text):
    text = ' '.join(str(text).split())[:400]
    if not text:
        return False
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.Speak([string]'" + text.replace("'", "''") + "')")
    try:
        p = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        with _voice_lock:
            _voice_procs.append(p)
        log('voice: speaking (%d chars)' % len(text))
        return True
    except Exception as e:
        log('voice failed: %s' % e)
        return False


def stop_voices():
    with _voice_lock:
        for p in _voice_procs:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        _voice_procs.clear()


# ---------------------------------------------------------- volume control
# The alarm forces the master volume to 100% while it rings (pycaw) and
# restores the previous level when it stops. The previous level is kept in
# VOLUME_BACKUP so even a crash/restart of the server cannot leave the
# laptop stuck at max volume. Without pycaw installed this is a no-op.
#
# All COM work happens on one dedicated thread: COM must be initialized per
# thread (CoInitialize), and the server otherwise calls this from HTTP
# handler threads and one-shot ring threads.
_vol_mod = {'loaded': False, 'au': None}
_vol_cache = {'at': 0.0, 'data': None}
_vol_jobs = queue.Queue()
_vol_start = {'ok': False}
_vol_start_lock = threading.Lock()


def _vol_api():
    if not _vol_mod['loaded']:
        _vol_mod['loaded'] = True
        try:
            from pycaw.pycaw import AudioUtilities
            _vol_mod['au'] = AudioUtilities
        except Exception as e:
            log('volume control unavailable (pycaw missing): %s' % e)
    return _vol_mod['au']


def _vol_read_raw():
    au = _vol_api()
    if not au:
        return None
    ev = au.GetSpeakers().EndpointVolume
    return {'percent': round(ev.GetMasterVolumeLevelScalar() * 100.0, 1),
            'mute': bool(ev.GetMute())}


def _vol_max_raw():
    au = _vol_api()
    if not au:
        return False
    prev = None
    if not os.path.exists(VOLUME_BACKUP):
        prev = _vol_read_raw()      # only the first max keeps a backup
    ev = au.GetSpeakers().EndpointVolume
    ev.SetMasterVolumeLevelScalar(1.0, None)
    ev.SetMute(False, None)
    if prev:
        with open(VOLUME_BACKUP, 'w', encoding='utf-8') as f:
            json.dump(prev, f)
        log('volume: %.0f%% -> 100%%' % prev['percent'])
    return True


def _vol_restore_raw():
    if not os.path.exists(VOLUME_BACKUP):
        return False
    with open(VOLUME_BACKUP, encoding='utf-8') as f:
        prev = json.load(f)
    os.remove(VOLUME_BACKUP)
    au = _vol_api()
    if not au:
        return False
    ev = au.GetSpeakers().EndpointVolume
    # If the level still looks like ours (100 %, unmuted) put the old level
    # back. Otherwise the user changed the volume during the ring (e.g. it was
    # too loud) - keep their choice instead of stomping it.
    if ev.GetMasterVolumeLevelScalar() * 100.0 < 99.5 or ev.GetMute():
        log('volume: user changed it during the ring - keeping current level')
        return False
    ev.SetMasterVolumeLevelScalar(max(0.0, min(1.0, float(prev.get('percent', 100)) / 100.0)), None)
    ev.SetMute(bool(prev.get('mute')), None)
    log('volume: restored to %.0f%%' % float(prev.get('percent', 100)))
    return True


def _vol_worker():
    try:
        rc = ctypes.windll.ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        if rc < 0:
            log('COM init failed (0x%08X) - volume control off' % (rc & 0xFFFFFFFF))
    except Exception as e:
        log('COM init failed: %s' % e)
    while True:
        job = _vol_jobs.get()
        if job is None:
            return
        fn, out, ev = job
        try:
            out['v'] = fn()
        except Exception as e:
            log('volume op failed: %s' % e)
            out['v'] = None
        ev.set()


def _vol_call(fn, timeout=6.0):
    """Run a volume operation on the dedicated COM thread and wait for it."""
    with _vol_start_lock:
        if not _vol_start['ok']:
            threading.Thread(target=_vol_worker, daemon=True, name='volume').start()
            _vol_start['ok'] = True
    out, ev = {}, threading.Event()
    _vol_jobs.put((fn, out, ev))
    if not ev.wait(timeout):
        log('volume op timed out')
        return None
    return out.get('v')


def volume_read():
    """Current master volume {'percent': 0-100, 'mute': bool} or None."""
    v = _vol_call(_vol_read_raw)
    if not v:
        return None
    _vol_cache['at'] = time.time()
    _vol_cache['data'] = v
    return v


def volume_state():
    """Cached volume for the state poll (refresh at most every 3 s)."""
    now = time.time()
    if now - _vol_cache['at'] > 3:
        _vol_cache['at'] = now
        _vol_cache['data'] = _vol_call(_vol_read_raw)
    return _vol_cache['data']


def volume_max():
    """Jump to 100% + unmute for a ringing alarm; remember the old level."""
    res = _vol_call(_vol_max_raw)
    _vol_cache['at'] = 0.0
    return bool(res)


def volume_restore():
    """Put the pre-ring level back (no-op when we never changed it)."""
    try:
        if not os.path.exists(VOLUME_BACKUP):
            return False
    except Exception:
        return False
    res = _vol_call(_vol_restore_raw)
    _vol_cache['at'] = 0.0
    return bool(res)


# ------------------------------------------------------------ Windows calls
def _run_ps(args, timeout=30):
    try:
        r = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass'] + args,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout, cwd=APP_DIR,
            creationflags=NO_WINDOW,
        )
        return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()
    except Exception as e:
        return -1, '', str(e)


def ps_battery():
    rc, out, err = _run_ps(['-File', WIN_SCRIPT, '-Action', 'battery'], timeout=20)
    if rc != 0 or not out or out == 'none':
        return None
    try:
        pct_s, status_s = out.split('|')
        pct = int(pct_s)
        status = int(status_s)
        charging = status in (2, 3, 6, 7, 8, 9, 11)
        return {'pct': pct, 'status': status, 'charging': charging}
    except Exception:
        return None


def ps_boot():
    rc, out, err = _run_ps(['-File', WIN_SCRIPT, '-Action', 'boot'], timeout=20)
    if rc != 0 or not out:
        return None
    try:
        return datetime.fromisoformat(out.replace('Z', '+00:00')).astimezone().replace(tzinfo=None)
    except Exception:
        return None


def ps_status():
    rc, out, err = _run_ps(['-File', WIN_SCRIPT, '-Action', 'status'], timeout=25)
    if rc != 0 or not out:
        return {'error': err or out or 'status failed'}
    try:
        return json.loads(out)
    except Exception:
        return {'error': 'bad status json: ' + out[:200]}


def find_pythonw():
    exe = sys.executable
    if os.path.basename(exe).lower().startswith('pythonw'):
        return exe
    cand = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    return cand if os.path.exists(cand) else exe


def read_paths():
    try:
        with open(PATHS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def write_paths():
    data = {
        'pythonw': find_pythonw(),
        'server': os.path.join(APP_DIR, 'server.py'),
        'wake_task': WAKE_TASK,
        'script': WIN_SCRIPT,
        'app_dir': APP_DIR,
    }
    try:
        with open(PATHS_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log('write _paths.json failed: %s' % e)
    return data


def register_task():
    rc, out, err = _run_ps(['-File', WIN_SCRIPT, '-Action', 'register-task'], timeout=60)
    try:
        j = json.loads(out) if out else None
    except Exception:
        j = None
    if rc == 0 and j and j.get('ok'):
        return True, 'ok'
    msg = (j or {}).get('error') or err or out or 'failed'
    return False, msg


def sleep_test_state():
    """Stage of the user-initiated sleep/wake proof test, if any."""
    try:
        if os.path.exists(SLEEP_RESULT):
            with open(SLEEP_RESULT, encoding='utf-8') as f:
                r = json.load(f)
            return {'stage': 'done', 'wake_at': r.get('wake_at'),
                    'woken_at': r.get('woken_at'), 'started_at': r.get('started_at')}
        if os.path.exists(SLEEP_PENDING):
            with open(SLEEP_PENDING, encoding='utf-8') as f:
                p = json.load(f)
            wake_at = datetime.fromisoformat(p['wake_at'])
            if datetime.now() > wake_at + timedelta(minutes=30):
                return {'stage': 'failed', 'wake_at': p.get('wake_at'),
                        'started_at': p.get('started_at')}
            return {'stage': 'armed', 'wake_at': p.get('wake_at'),
                    'started_at': p.get('started_at')}
    except Exception:
        pass
    return None


def start_sleep_test():
    """Prove the wake path: arm a one-shot WakeToRun task 2 minutes ahead,
    then suspend this PC. wake_task.py records the wake in
    sleep_test_result.json so the UI can show the outcome."""
    for p in (SLEEP_PENDING, SLEEP_RESULT):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    paths = read_paths()
    if not paths:
        return {'ok': False, 'error': '_paths.json missing - restart the app first'}
    wake_at = datetime.now() + timedelta(minutes=2)
    at = wake_at.strftime('%Y-%m-%d %H:%M:%S')
    cmd = (
        "$a = New-ScheduledTaskAction -Execute '{py}' -Argument '\"{wt}\"'; "
        "$t = New-ScheduledTaskTrigger -Once -At '{at}'; "
        "$s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew; "
        "Register-ScheduledTask -TaskName 'WakeAlarmSleepTest' -Action $a -Trigger $t "
        "-Settings $s -Force | Out-Null; 'ok'"
    ).format(py=paths['pythonw'], wt=WAKE_TASK, at=at)
    rc, out, err = _run_ps(['-Command', cmd], timeout=60)
    if rc != 0 or out.strip() != 'ok':
        return {'ok': False, 'error': (err or out or 'task registration failed').strip()[:300]}
    try:
        with open(SLEEP_PENDING, 'w', encoding='utf-8') as f:
            json.dump({'started_at': datetime.now().isoformat(timespec='seconds'),
                       'wake_at': wake_at.isoformat(timespec='seconds')}, f)
    except OSError as e:
        return {'ok': False, 'error': 'cannot write pending marker: %s' % e}

    def _suspend():
        time.sleep(3.0)  # let the HTTP response flush first
        log('sleep test: suspending now (wake expected %s)' % wake_at.strftime('%H:%M:%S'))
        try:
            subprocess.Popen(
                ['rundll32.exe', 'powrprof.dll,SetSuspendState', '0', '1', '0'],
                creationflags=NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log('sleep test: suspend failed: %s' % e)

    threading.Thread(target=_suspend, daemon=True).start()
    return {'ok': True, 'wake_at': wake_at.isoformat(timespec='seconds')}


def run_setup_elevated():
    out_path = os.path.join(APP_DIR, '_setup_out.txt')
    err_path = os.path.join(APP_DIR, '_setup_err.txt')
    for p in (out_path, err_path):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    # -Verb RunAs cannot be combined with -RedirectStandardOutput, so the
    # script writes its own output file via -OutFile.
    cmd = (
        "Start-Process -Wait -FilePath powershell.exe -Verb RunAs "
        "-ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{script}\" -Action setup -OutFile \"{out}\"'"
    ).format(script=WIN_SCRIPT, out=out_path)
    rc, so, se = _run_ps(['-Command', cmd], timeout=180)
    if rc != 0 and not os.path.exists(out_path):
        return {'ok': False, 'output': [se or so or 'UAC cancelled or start failed']}
    text = ''
    try:
        with open(out_path, encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
    except Exception:
        pass
    try:
        return json.loads(text.strip().splitlines()[-1])
    except Exception:
        err_text = ''
        try:
            with open(err_path, encoding='utf-8', errors='replace') as f:
                err_text = f.read()
        except Exception:
            pass
        return {'ok': False, 'output': [text or err_text or 'setup produced no output']}


# ------------------------------------------------------------- data storage
DEFAULT_CONFIG = {
    'installed_at': None,
    'alarm': {
        'enabled': True,
        'time': '06:00',
        'days': [0, 1, 2, 3, 4, 5, 6],
        'commitment': '',
        'letter': '',
    },
    'settings': {
        'stage_delays': [45, 45, 60],
        'snooze_minutes': 5,
        'snooze_max': 2,
        'dismiss_task': 'random',
        'math_difficulty': 'easy',
        'voice_every': 20,
        'voice_enabled': True,
        'battery_warn_pct': 30,
        'grace_minutes': 45,
        'auto_open': True,
        'max_volume': True,
    },
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _normalize_alarms(raw):
    """Validate a list of alarm entries -> [{'id','time','days','enabled'}].
    Returns None when any entry is unusable (so save can reject the batch)."""
    out = []
    for x in (raw or []):
        if not isinstance(x, dict):
            return None
        try:
            h, m = _hm(str(x.get('time', '')).strip())
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            t = '%02d:%02d' % (h, m)
        except Exception:
            return None
        try:
            days = sorted({int(d) for d in (x.get('days') or []) if 0 <= int(d) <= 6})
        except Exception:
            return None
        try:
            aid = int(x.get('id'))
        except Exception:
            aid = 0
        out.append({'id': aid or (max([r['id'] for r in out] or [0]) + 1),
                    'time': t, 'days': days,
                    'enabled': bool(x.get('enabled', True))})
    return out


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.config = dict(DEFAULT_CONFIG)
        self.history = []
        try:
            with open(CONFIG_PATH, encoding='utf-8') as f:
                self.config = _deep_merge(DEFAULT_CONFIG, json.load(f))
        except Exception:
            pass
        try:
            with open(HISTORY_PATH, encoding='utf-8') as f:
                self.history = json.load(f)
        except Exception:
            self.history = []
        self._migrate_alarms()
        if not self.config.get('installed_at'):
            self.config['installed_at'] = datetime.now().isoformat(timespec='seconds')
            self.save_config()

    def _migrate_alarms(self):
        """config['alarms'] is the list of all alarms; older configs only had
        the single 'alarm' object - promote it (idempotent)."""
        cfg = self.config
        rows = _normalize_alarms(cfg.get('alarms'))
        if not rows:
            a = cfg.get('alarm') or {}
            rows = _normalize_alarms([{'time': a.get('time') or '06:00',
                                       'days': a.get('days') or [],
                                       'enabled': a.get('enabled', True), 'id': 1}])
        if not rows:  # last resort: sane default so the app always has one
            rows = _normalize_alarms([{'time': '06:00', 'days': list(range(7)),
                                       'enabled': True, 'id': 1}])
        cfg['alarms'] = rows
        self._mirror_primary()

    def _mirror_primary(self):
        """Keep the legacy single-alarm keys (and old API consumers) in sync
        with the first alarm entry; commitment/letter stay global."""
        rows = (self.config.get('alarms') or [])
        if rows:
            a = self.config['alarm']
            a['time'] = rows[0]['time']
            a['days'] = list(rows[0]['days'])
            a['enabled'] = rows[0]['enabled']

    def save_config(self):
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)

    def save_history(self):
        tmp = HISTORY_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.history[-MAX_HISTORY:], f, indent=2, ensure_ascii=False)
        os.replace(tmp, HISTORY_PATH)

    def find_rec(self, occ, kind=None):
        key = occ.isoformat(timespec='seconds')
        for r in reversed(self.history):
            if r.get('scheduled') == key and (kind is None or r.get('kind') == kind):
                return r
        return None

    def upsert(self, rec):
        key = rec.get('scheduled')
        for i, r in enumerate(self.history):
            if r.get('scheduled') == key and r.get('kind') == rec.get('kind'):
                self.history[i] = rec
                self.save_history()
                return
        self.history.append(rec)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        self.save_history()


# ------------------------------------------------------- occurrence helpers
def _hm(s):
    h, m = s.split(':')
    return int(h), int(m)


def most_recent_occurrence(now, tstr, days, lookback=7):
    h, m = _hm(tstr)
    for i in range(lookback + 1):
        d = (now - timedelta(days=i)).date()
        if d.weekday() in days:
            occ = datetime(d.year, d.month, d.day, h, m)
            if occ <= now:
                return occ
    return None


def next_occurrence(now, tstr, days):
    h, m = _hm(tstr)
    for i in range(8):
        d = (now + timedelta(days=i)).date()
        if d.weekday() in days:
            occ = datetime(d.year, d.month, d.day, h, m)
            if occ > now:
                return occ
    return None


# ---------------------------------------------------------- dismissal tasks
SENTENCES = [
    'The early bird catches the worm.',
    'I am awake because I chose to be.',
    'Small steps every day beat big steps someday.',
    'Discipline gets me out of bed, not motivation.',
    'Today I keep the promise I made last night.',
]
DECOYS = [
    'Sleep in and skip the morning entirely.',
    'Wait until lunch to decide what to do.',
    'Check social media for an hour first.',
    'Postpone the study session until tonight.',
    'Ignore the plan I wrote yesterday.',
    'Let the day happen to me.',
]
COLORS = ['red', 'blue', 'green', 'yellow']
DEFAULT_REASON = 'Get up at my planned time and keep my wake-up consistent.'


def normalize_sentence(s):
    return ' '.join(str(s).lower().split())


def make_task(settings, commitment):
    kind = settings.get('dismiss_task', 'math')
    if kind == 'random':
        kind = random.choice(['math', 'sentence', 'quiz', 'sequence'])
    if kind == 'math':
        diff = settings.get('math_difficulty', 'easy')
        if diff == 'medium':
            a, b = random.randint(12, 29), random.randint(4, 9)
            prompt, ans = 'What is %d x %d?' % (a, b), a * b
        elif diff == 'hard':
            a, b, c = random.randint(40, 90), random.randint(15, 40), random.randint(5, 19)
            prompt, ans = 'What is %d + %d - %d?' % (a, b, c), a + b - c
        else:
            a, b = random.randint(11, 49), random.randint(11, 49)
            prompt, ans = 'What is %d + %d?' % (a, b), a + b
        return {'type': 'math', 'prompt': prompt, 'answer': str(ans)}
    if kind == 'sentence':
        target = random.choice(SENTENCES)
        return {
            'type': 'sentence',
            'prompt': 'Type this sentence exactly:',
            'sentence': target,
            'answer': normalize_sentence(target),
        }
    if kind == 'quiz':
        correct = (commitment or '').strip() or DEFAULT_REASON
        opts = [correct] + random.sample(DECOYS, 2)
        random.shuffle(opts)
        return {
            'type': 'quiz',
            'prompt': 'Which of these is your wake-up commitment?',
            'options': opts,
            'correct_index': opts.index(correct),
        }
    seq = [random.choice(COLORS)]
    while len(seq) < 4:
        c = random.choice(COLORS)
        if c != seq[-1]:
            seq.append(c)
    return {
        'type': 'sequence',
        'prompt': 'Click the colours in this order:',
        'target': seq,
    }


def public_task(task):
    if not task:
        return None
    t = {'type': task['type'], 'prompt': task['prompt']}
    if task['type'] == 'sentence':
        t['sentence'] = task['sentence']
    elif task['type'] == 'quiz':
        t['options'] = task['options']
    elif task['type'] == 'sequence':
        t['target'] = task['target']
    return t


def check_answer(task, answer):
    t = task['type']
    if t == 'math':
        return str(answer).strip() == task['answer']
    if t == 'sentence':
        return normalize_sentence(answer) == task['answer']
    if t == 'quiz':
        try:
            return int(answer) == task['correct_index']
        except Exception:
            return False
    if t == 'sequence':
        try:
            return list(answer) == task['target']
        except Exception:
            return False
    return False


# ----------------------------------------------------------------- browser
EDGE_PATHS = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
]
CHROME_PATHS = [
    os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
]


def open_fullscreen(url):
    flags = 0
    if sys.platform == 'win32':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
    for exe in EDGE_PATHS + CHROME_PATHS:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, '--start-fullscreen', url], cwd=APP_DIR,
                                 creationflags=flags,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log('opened fullscreen window via %s' % exe)
                return True
            except Exception as e:
                log('fullscreen open failed (%s): %s' % (exe, e))
    try:
        os.startfile(url)
        return True
    except Exception as e:
        log('default browser open failed: %s' % e)
        return False


# ----------------------------------------------------------------- engine
STAGE_FILES = ['stage1.wav', 'stage2.wav', 'stage3.wav', 'stage4.wav']
STAGE_LABELS = ['gentle chime', 'alternating pattern', 'urgent beeps', 'voice reminder']


class Engine:
    def __init__(self, store):
        self.store = store
        self.player = SoundPlayer()
        self.lock = threading.Lock()
        self.ringing = None          # dict while alarm is sounding
        self.last_client = 0.0
        self.last_voice = 0.0
        self.stop_evt = threading.Event()
        self.battery_cache = {'at': 0.0, 'data': None}
        self.status_cache = {'at': 0.0, 'data': None}
        self.boot_cache = {'at': 0.0, 'data': None}

    # ---- background loops
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._introspect, daemon=True).start()

    def _loop(self):
        while not self.stop_evt.is_set():
            try:
                self.tick()
            except Exception:
                log('tick error:\n' + traceback.format_exc())
            self.stop_evt.wait(1.0)

    def _introspect(self):
        while not self.stop_evt.is_set():
            try:
                now = time.time()
                if now - self.battery_cache['at'] > 60:
                    self.battery_cache = {'at': now, 'data': ps_battery()}
                if now - self.status_cache['at'] > 30:
                    self.status_cache = {'at': now, 'data': ps_status()}
                if now - self.boot_cache['at'] > 300:
                    self.boot_cache = {'at': now, 'data': ps_boot()}
            except Exception:
                log('introspect error:\n' + traceback.format_exc())
            self.stop_evt.wait(20.0)

    def battery(self):
        return self.battery_cache['data']

    def windows_status(self):
        return self.status_cache['data']

    def boot_time(self):
        return self.boot_cache['data']

    # ---- core scheduling
    def due_candidates(self, now):
        """(occ, alarm) for every armed alarm whose most recent occurrence is
        due - deduplicated (two alarms at the same time ring once), earliest
        occurrence first."""
        out, seen = [], set()
        for a in (self.store.config.get('alarms') or []):
            if not a.get('enabled'):
                continue
            try:
                days = [int(d) for d in a.get('days', [])]
            except Exception:
                days = []
            if not days:
                continue
            try:
                occ = most_recent_occurrence(now, a['time'], days)
            except Exception:
                continue
            if not occ or occ in seen:
                continue
            seen.add(occ)
            out.append((occ, a))
        out.sort(key=lambda x: x[0])
        return out

    def tick(self):
        now = datetime.now()
        cfg = self.store.config
        grace = timedelta(minutes=int(cfg['settings'].get('grace_minutes', 45)))
        installed = None
        if cfg.get('installed_at'):
            try:
                installed = datetime.fromisoformat(cfg['installed_at'])
            except Exception:
                installed = None
        cands = self.due_candidates(now)
        if not cands:
            with self.lock:
                if self.ringing:
                    self._progress(now)
            return
        with self.lock:
            if self.ringing:
                self._progress(now)
                return
            for occ, a in cands:
                if installed and occ <= installed:
                    continue  # before the app existed - nothing to report
                rec = self.store.find_rec(occ)
                if rec and rec.get('dismissed_at'):
                    continue
                if rec and rec.get('missed'):
                    continue
                if rec and rec.get('snoozed_until'):
                    try:
                        if now < datetime.fromisoformat(rec['snoozed_until']):
                            continue
                    except Exception:
                        pass
                elapsed = now - occ
                if elapsed <= grace:
                    self._ring(occ, (rec or {}).get('kind') or 'wake', rec)
                    return  # one ring at a time; remaining cands ring after this one ends
                if not rec:
                    self._mark_missed(occ, 'wake', None, now)
                elif not rec.get('missed') and not rec.get('dismissed_at'):
                    reason = 'never_dismissed' if rec.get('ring_at') else 'app_not_running'
                    self._mark_missed(occ, rec.get('kind') or 'wake', rec, now, reason)

    def _missed_reason(self, occ, now):
        boot = self.boot_time()
        if boot and boot > occ:
            return 'pc_was_off_or_asleep_until_%s' % boot.strftime('%H:%M')
        return 'app_not_running'

    def _mark_missed(self, occ, kind, rec, now, reason=None):
        if kind == 'test':
            return
        rec = rec or {'scheduled': occ.isoformat(timespec='seconds'), 'kind': kind}
        rec['missed'] = True
        rec['missed_reason'] = reason or self._missed_reason(occ, now)
        rec['missed_at'] = now.isoformat(timespec='seconds')
        rec.setdefault('boot_time', self.boot_time().isoformat(timespec='seconds') if self.boot_time() else None)
        self.store.upsert(rec)
        log('MISSED %s (%s)' % (occ, rec['missed_reason']))

    def _ring(self, occ, kind, rec=None):
        now = datetime.now()
        cfg = self.store.config
        rec = rec or {'scheduled': occ.isoformat(timespec='seconds'), 'kind': kind}
        rec['kind'] = kind
        rec['ring_at'] = now.isoformat(timespec='seconds')
        rec['missed'] = False
        rec['missed_reason'] = None
        rec['late_by_s'] = int((now - occ).total_seconds())
        batt = self.battery()
        rec['battery'] = batt['pct'] if batt else None
        rec['charging'] = batt['charging'] if batt else None
        boot = self.boot_time()
        rec['boot_time'] = boot.isoformat(timespec='seconds') if boot else None
        task = make_task(cfg['settings'], cfg['alarm'].get('commitment', ''))
        rec['task'] = task['type']
        self.store.upsert(rec)
        self.ringing = {
            'occ': occ, 'kind': kind, 'started_at': now, 'task': task,
            'stage': 1, 'silent': False,
        }
        self.last_voice = 0.0
        self.player.play(os.path.join(SOUND_DIR, STAGE_FILES[0]))
        if cfg['settings'].get('max_volume', True):
            # background thread: the first call may import pycaw (~0.2 s) and
            # the chime should not wait for it
            threading.Thread(target=volume_max, daemon=True).start()
        log('RING %s kind=%s late=%ss' % (occ, kind, rec['late_by_s']))
        if cfg['settings'].get('auto_open', True) and (time.time() - self.last_client) > 15:
            open_fullscreen('http://127.0.0.1:%d/' % PORT)

    def _progress(self, now):
        r = self.ringing
        if not r:
            return
        delays = self.store.config['settings'].get('stage_delays', [45, 45, 60])
        try:
            delays = [int(x) for x in delays]
        except Exception:
            delays = [45, 45, 60]
        el = (now - r['started_at']).total_seconds()
        b1, b2 = delays[0], delays[0] + delays[1]
        stage = 1 if el < b1 else 2 if el < b2 else 3 if el < b2 + delays[2] else 4
        if stage != r['stage']:
            r['stage'] = stage
            log('stage -> %d (%s)' % (stage, STAGE_LABELS[stage - 1]))
            if not r['silent']:
                self.player.play(os.path.join(SOUND_DIR, STAGE_FILES[stage - 1]))
            if stage == 4:
                self.last_voice = 0.0
        s = self.store.config['settings']
        if stage == 4 and s.get('voice_enabled', True) and not r['silent']:
            interval = max(5, int(s.get('voice_every', 20)))
            if now.timestamp() - self.last_voice >= interval:
                self.last_voice = now.timestamp()
                speak(self._voice_text())

    def _voice_text(self):
        a = self.store.config['alarm']
        parts = ['Good morning.']
        c = (a.get('commitment') or '').strip()
        if c:
            parts.append('You said you would wake up to: ' + c)
        else:
            parts.append('You said you would keep your wake-up time.')
        letter = (a.get('letter') or '').strip()
        if letter:
            parts.append(letter)
        return ' '.join(parts)

    # ---- commands
    def dismiss(self, answer):
        with self.lock:
            r = self.ringing
            if not r:
                return {'ok': False, 'message': 'Nothing is ringing.'}
            if not check_answer(r['task'], answer):
                log('dismiss rejected (wrong answer)')
                return {'ok': False, 'message': 'Not quite - try again.'}
            now = datetime.now()
            self.player.stop()
            stop_voices()
            volume_restore()
            rec = self.store.find_rec(r['occ'], r['kind']) or {}
            rec['scheduled'] = r['occ'].isoformat(timespec='seconds')
            rec['kind'] = r['kind']
            rec['dismissed_at'] = now.isoformat(timespec='seconds')
            rec['dismiss_seconds'] = int((now - r['started_at']).total_seconds())
            rec['ring_seconds'] = int((now - r['started_at']).total_seconds())
            rec['snoozed_until'] = None  # stale snooze must not keep showing
            self.store.upsert(rec)
            self.ringing = None
            log('DISMISS %s after %ss' % (r['occ'], rec['dismiss_seconds']))
            return {'ok': True, 'dismissed_at': rec['dismissed_at'],
                    'ring_seconds': rec['dismiss_seconds'],
                    'letter': self.store.config['alarm'].get('letter', '')}

    def snooze(self):
        with self.lock:
            r = self.ringing
            if not r:
                return {'ok': False, 'message': 'Nothing is ringing.'}
            s = self.store.config['settings']
            rec = self.store.find_rec(r['occ'], r['kind']) or {}
            used = int(rec.get('snoozes', 0))
            maxn = int(s.get('snooze_max', 2))
            if used >= maxn:
                return {'ok': False, 'message': 'No snoozes left - dismiss the alarm.'}
            now = datetime.now()
            self.player.stop()
            stop_voices()
            volume_restore()
            until = now + timedelta(minutes=int(s.get('snooze_minutes', 5)))
            rec['scheduled'] = r['occ'].isoformat(timespec='seconds')
            rec['kind'] = r['kind']
            rec['snoozes'] = used + 1
            rec['snoozed_until'] = until.isoformat(timespec='seconds')
            self.store.upsert(rec)
            self.ringing = None
            log('SNOOZE %s until %s (%d left)' % (r['occ'], until.strftime('%H:%M'), maxn - used - 1))
            return {'ok': True, 'snoozed_until': rec['snoozed_until'],
                    'remaining': maxn - used - 1}

    def test_ring(self, silent=False):
        with self.lock:
            if self.ringing:
                old = self.ringing
                self.player.stop()
                stop_voices()
                rec = self.store.find_rec(old['occ'], old['kind'])
                if rec and not rec.get('dismissed_at'):
                    rec['dismissed_at'] = datetime.now().isoformat(timespec='seconds')
                    rec['note'] = 'replaced by new test'
                    self.store.upsert(rec)
                self.ringing = None
            occ = datetime.now().replace(microsecond=0)
            self._ring(occ, 'test')
            if silent:
                self.ringing['silent'] = True
                self.player.stop()
                stop_voices()
            return {'ok': True, 'silent': bool(silent)}

    def sound_test(self, stage, seconds):
        if self.ringing:
            return {'ok': False, 'message': 'Alarm is ringing.'}
        stage = max(1, min(4, int(stage)))
        path = os.path.join(SOUND_DIR, STAGE_FILES[stage - 1])
        self.player.play(path)
        threading.Timer(max(1.0, float(seconds)), self.player.stop).start()
        return {'ok': True, 'stage': stage}

    def voice_test(self):
        ok = speak('Good morning. This is how your wake up voice will sound.')
        return {'ok': ok}

    def ack_missed(self):
        # Clear every unacked missed alarm: acking just one would leave the
        # banner showing the next-oldest record straight away.
        with self.store.lock:
            n = 0
            for r in self.store.history:
                if r.get('missed') and not r.get('acked'):
                    r['acked'] = True
                    n += 1
            if n:
                self.store.save_history()
            return {'ok': bool(n), 'acked': n}

    def clear_history(self):
        with self.store.lock:
            self.store.history = []
            self.store.save_history()
        return {'ok': True}


# ------------------------------------------------------------------ state
def build_state(engine, store):
    now = datetime.now()
    cfg = store.config
    alarm = cfg['alarm']
    rows = cfg.get('alarms') or []
    out = {
        'now': now.isoformat(timespec='seconds'),
        'alarm': alarm,
        'alarms': rows,
        'settings': cfg['settings'],
        'installed_at': cfg.get('installed_at'),
        'next': None,
        'next_in_s': None,
        'next_primary': None,
        'ringing': None,
        'snoozed_until': None,
        'missed': None,
        'battery': engine.battery(),
        'windows': engine.windows_status(),
        'volume': volume_state(),
        'client_seen': bool(engine.last_client and (time.time() - engine.last_client) < 15),
        'stage_labels': STAGE_LABELS,
        'sleep_test': sleep_test_state(),
    }
    with engine.lock:
        r = engine.ringing
        if r:
            rec = store.find_rec(r['occ'], r['kind'])
            out['ringing'] = {
                'active': True,
                'occ': r['occ'].isoformat(timespec='seconds'),
                'kind': r['kind'],
                'started_at': r['started_at'].isoformat(timespec='seconds'),
                'stage': r['stage'],
                'silent': r['silent'],
                'task': public_task(r['task']),
                'elapsed_s': int((now - r['started_at']).total_seconds()),
                'snoozes_used': int(rec.get('snoozes', 0)) if rec else 0,
                'snooze_max': int(cfg['settings'].get('snooze_max', 2)),
            }
    if not out['ringing']:
        best = None
        primary = None
        for a in rows:
            if not a.get('enabled'):
                continue
            adays = [int(d) for d in a.get('days', [])]
            if not adays:
                continue
            try:
                n = next_occurrence(now, a['time'], adays)
            except Exception:
                continue
            if n and (best is None or n < best):
                best = n
            if primary is None:
                primary = n  # first armed alarm owns the 'WakeAlarm' task
        if best:
            out['next'] = best.isoformat(timespec='seconds')
            out['next_in_s'] = int((best - now).total_seconds())
            out['next_primary'] = primary.isoformat(timespec='seconds') if primary else None
    for rec in reversed(store.history):
        if rec.get('snoozed_until'):
            try:
                if datetime.fromisoformat(rec['snoozed_until']) > now:
                    out['snoozed_until'] = rec['snoozed_until']
                    break
            except Exception:
                pass
    for rec in reversed(store.history):
        if rec.get('missed') and not rec.get('acked'):
            out['missed'] = {
                'scheduled': rec.get('scheduled'),
                'reason': rec.get('missed_reason'),
                'kind': rec.get('kind'),
            }
            break
    return out


def compute_stats(store):
    wake = [r for r in store.history if r.get('kind') == 'wake']
    tests = [r for r in store.history if r.get('kind') == 'test']
    dismissed = [r for r in wake if r.get('dismissed_at')]
    missed = [r for r in wake if r.get('missed')]
    durations = [r['dismiss_seconds'] for r in dismissed if isinstance(r.get('dismiss_seconds'), int)]
    on_time = sum(1 for d in durations if d <= 300)
    return {
        'wake_total': len(wake),
        'wake_dismissed': len(dismissed),
        'wake_missed': len(missed),
        'tests': len(tests),
        'snoozes': sum(int(r.get('snoozes', 0)) for r in store.history),
        'avg_dismiss_s': int(sum(durations) / len(durations)) if durations else None,
        'on_time_pct': int(100 * on_time / len(durations)) if durations else None,
    }


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    engine = None
    store = None
    server_version = 'WakeSystem/1.0'
    _LOCAL_HOSTS = ('127.0.0.1', 'localhost')

    def log_message(self, *a):
        pass

    def _host_ok(self):
        """Reject DNS-rebinding: Host must be this loopback address/port."""
        host = (self.headers.get('Host') or '').strip().lower()
        name = host.split(':')[0]
        port_part = host.rsplit(':', 1)[1] if ':' in host else ''
        if name not in self._LOCAL_HOSTS:
            return False
        return port_part in ('', str(PORT))

    def _origin_ok(self):
        """Reject cross-site POSTs: any Origin/Referer must be our own origin."""
        for header in ('Origin', 'Referer'):
            raw = self.headers.get(header)
            if not raw:
                continue
            p = urllib.parse.urlparse(raw)
            if p.hostname not in self._LOCAL_HOSTS:
                return False
            if p.port not in (None, PORT):
                return False
        return True

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b'{}'
        try:
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            return {}

    def _file(self, path, ctype):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except Exception:
            self._json({'error': 'not found'}, 404)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------------- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = dict(urllib.parse.parse_qsl(parsed.query))
        eng, store = self.engine, self.store
        try:
            if not self._host_ok():
                self._json({'error': 'forbidden'}, 403)
            elif path == '/':
                self._file(os.path.join(APP_DIR, 'index.html'), 'text/html; charset=utf-8')
            elif path == '/favicon.ico':
                self.send_response(204)
                self.end_headers()
            elif path == '/api/state':
                self._json(build_state(eng, store))
            elif path == '/api/history':
                recs = list(reversed(store.history))[:60]
                self._json({'records': recs, 'stats': compute_stats(store)})
            elif path == '/api/preview-next':
                t = q.get('time', '06:00')
                days = [int(d) for d in q.get('days', '0,1,2,3,4,5,6').split(',') if d != '']
                try:
                    nxt = next_occurrence(datetime.now(), t, days)
                    self._json({'ok': True, 'next': nxt.isoformat(timespec='seconds') if nxt else None})
                except Exception as e:
                    self._json({'ok': False, 'error': str(e)}, 400)
            else:
                self._json({'error': 'not found'}, 404)
        except Exception:
            log('GET %s error:\n%s' % (path, traceback.format_exc()))
            self._json({'error': 'server error'}, 500)

    # ---------------- POST
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok() or not self._origin_ok():
            self._json({'error': 'forbidden origin'}, 403)
            return
        ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if ctype != 'application/json':
            self._json({'error': 'Content-Type must be application/json'}, 415)
            return
        body = self._body()
        eng, store = self.engine, self.store
        try:
            if path == '/nudge':
                eng.tick()
                self._json({'ok': True, 'wake': True})
            elif path == '/api/heartbeat':
                eng.last_client = time.time()
                self._json({'ok': True})
            elif path == '/api/alarm':
                res = save_alarm(store, eng, body)
                self._json(res, 200 if res.get('ok') else 400)
            elif path == '/api/settings':
                res = save_settings(store, body)
                self._json(res, 200 if res.get('ok') else 400)
            elif path == '/api/test-ring':
                self._json(eng.test_ring(silent=bool(body.get('silent'))))
            elif path == '/api/dismiss':
                res = eng.dismiss(body.get('answer'))
                self._json(res, 200 if res.get('ok') else 400)
            elif path == '/api/snooze':
                res = eng.snooze()
                self._json(res, 200 if res.get('ok') else 400)
            elif path == '/api/sound-test':
                res = eng.sound_test(body.get('stage', 1), body.get('seconds', 2.5))
                self._json(res, 200 if res.get('ok') else 400)
            elif path == '/api/voice-test':
                self._json(eng.voice_test())
            elif path == '/api/ack-missed':
                res = eng.ack_missed()
                self._json(res, 200 if res.get('ok') else 400)
            elif path == '/api/history/clear':
                self._json(eng.clear_history())
            elif path == '/api/windows/setup':
                log('running Windows setup (UAC prompt may appear)...')
                res = run_setup_elevated()
                eng.status_cache = {'at': time.time(), 'data': ps_status()}
                self._json(res)
            elif path == '/api/sleep-test':
                self._json(start_sleep_test())
            else:
                self._json({'error': 'not found'}, 404)
        except Exception:
            log('POST %s error:\n%s' % (path, traceback.format_exc()))
            self._json({'error': 'server error'}, 500)


def save_alarm(store, eng, body):
    a = body.get('alarm') or {}
    rows_in = body.get('alarms')
    if rows_in is None:
        # Legacy single-alarm payload: {'alarm': {time, days, enabled, ...}}
        t = str(a.get('time', '')).strip()
        try:
            h, m = _hm(t)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            t = '%02d:%02d' % (h, m)
        except Exception:
            return {'ok': False, 'error': 'Invalid time.'}
        try:
            days = sorted({int(d) for d in (a.get('days') or []) if 0 <= int(d) <= 6})
        except Exception:
            return {'ok': False, 'error': 'Invalid days.'}
        rows_in = [{'id': 1, 'time': t, 'days': days,
                    'enabled': bool(a.get('enabled', True))}]
    if not isinstance(rows_in, list) or not rows_in:
        return {'ok': False, 'error': 'Add at least one alarm.'}
    if len(rows_in) > 8:
        return {'ok': False, 'error': 'A maximum of 8 alarms can be armed.'}
    rows = _normalize_alarms(rows_in)
    if rows is None or len(rows) != len(rows_in):
        return {'ok': False, 'error': 'Invalid time in one of the alarms.'}
    enabled_any = False
    for i, r in enumerate(rows):
        if r['enabled'] and not r['days']:
            return {'ok': False,
                    'error': 'Select at least one day - an armed alarm with no days never rings.'}
        if r['enabled']:
            enabled_any = True
    with store.lock:
        store.config['alarms'] = rows
        if body.get('alarm') is not None:
            alarm = store.config['alarm']
            alarm['commitment'] = str(a.get('commitment', ''))[:600]
            alarm['letter'] = str(a.get('letter', ''))[:2000]
        store._mirror_primary()
        store.save_config()
    task_msg = 'not_needed'
    if enabled_any:
        ok, msg = register_task()
        task_msg = 'ok' if ok else 'failed: ' + msg
        log('register-task -> %s' % task_msg)
        # refresh immediately so the status card shows the new task list now
        eng.status_cache = {'at': time.time(), 'data': ps_status()}
    return {'ok': True, 'task_update': task_msg,
            'alarm': store.config['alarm'], 'alarms': store.config['alarms']}


def save_settings(store, body):
    s = body.get('settings') or {}
    try:
        with store.lock:
            allowed = store.config['settings']
            if 'stage_delays' in s:
                vals = [max(5, min(600, int(x))) for x in s['stage_delays']]
                if len(vals) == 3:
                    allowed['stage_delays'] = vals
            for key, lo, hi in (('snooze_minutes', 1, 30), ('snooze_max', 0, 5),
                                ('voice_every', 5, 120), ('battery_warn_pct', 5, 90),
                                ('grace_minutes', 5, 240)):
                if key in s:
                    allowed[key] = max(lo, min(hi, int(s[key])))
            if s.get('dismiss_task') in ('math', 'sentence', 'quiz', 'sequence', 'random'):
                allowed['dismiss_task'] = s['dismiss_task']
            if s.get('math_difficulty') in ('easy', 'medium', 'hard'):
                allowed['math_difficulty'] = s['math_difficulty']
            for key in ('voice_enabled', 'auto_open', 'max_volume'):
                if key in s:
                    allowed[key] = bool(s[key])
            store.save_config()
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    return {'ok': True, 'settings': allowed}


# -------------------------------------------------------- single instance
_instance_lock_handle = None


def _port_serving(port):
    """True when something already answers on the loopback port."""
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1.5):
            return True
    except OSError:
        return False


def acquire_instance_lock():
    """Hold server.lock exclusively for the process lifetime.

    Windows SO_REUSEADDR lets two sockets bind the same port, so the port
    probe alone is not enough; the OS releases this lock when we exit.
    """
    global _instance_lock_handle
    path = os.path.join(APP_DIR, 'server.lock')
    f = None
    try:
        f = open(path, 'a+')
        if IS_WIN:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        return False
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
    except OSError:
        pass
    _instance_lock_handle = f
    return True


# ------------------------------------------------------------------ main
def main():
    # Refuse to start a second copy: the Run key and the WakeAlarm task can
    # both launch server.py (e.g. at logon + a wake), and two engines would
    # double-ring and race on config/history writes.
    if _port_serving(PORT):
        log('another instance already serves port %d - exiting' % PORT)
        return 0
    if not acquire_instance_lock():
        log('another instance holds server.lock - exiting')
        return 0
    ensure_sounds()
    store = Store()
    if os.path.exists(VOLUME_BACKUP):
        volume_restore()  # a crash mid-ring must not leave the laptop stuck loud
    write_paths()
    engine = Engine(store)
    Handler.engine = engine
    Handler.store = store
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError as e:
        log('port %d busy (%s) - another instance is probably running' % (PORT, e))
        return 0
    httpd.daemon_threads = True
    try:
        with open(PORT_PATH, 'w') as f:
            f.write(str(PORT))
    except Exception:
        pass
    # prime caches once so the first UI load has data
    engine.battery_cache = {'at': time.time(), 'data': ps_battery()}
    engine.status_cache = {'at': time.time(), 'data': ps_status()}
    engine.boot_cache = {'at': time.time(), 'data': ps_boot()}
    engine.start()
    log('Wake System listening on http://127.0.0.1:%d/' % PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop_evt.set()
        engine.player.stop()
        stop_voices()
        volume_restore()
        try:
            os.remove(PORT_PATH)
        except Exception:
            pass
        log('shutdown')
    return 0


if __name__ == '__main__':
    sys.exit(main())
