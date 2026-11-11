#!/usr/bin/env python3
"""Scheduled-task action: nudge the running server, or start it if it is down."""
import datetime
import json
import os
import subprocess
import sys
import urllib.request
from datetime import timedelta

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(APP_DIR, 'wake_task.log')


def log(msg):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def mark_sleep_test():
    """If a sleep test was pending, record that this wake proves it worked."""
    pend = os.path.join(APP_DIR, 'sleep_test_pending.json')
    if not os.path.exists(pend):
        return
    try:
        with open(pend, encoding='utf-8') as f:
            p = json.load(f)
        wake_at = datetime.datetime.fromisoformat(p['wake_at'])
        now = datetime.datetime.now()
        if now < wake_at - timedelta(minutes=10):
            return          # not due yet
        if now > wake_at + timedelta(minutes=30):
            return          # too late to count as a wake proof
        os.remove(pend)
        p['ok'] = True
        p['woken_at'] = now.isoformat(timespec='seconds')
        with open(os.path.join(APP_DIR, 'sleep_test_result.json'), 'w', encoding='utf-8') as f:
            json.dump(p, f, indent=2)
        log('sleep test: WOKEN at %s (expected %s)' % (
            now.strftime('%H:%M:%S'), wake_at.strftime('%H:%M:%S')))
    except Exception as e:
        log('sleep test mark failed: %s' % e)


def main():
    mark_sleep_test()
    port = None
    try:
        with open(os.path.join(APP_DIR, 'port.txt')) as f:
            port = int(f.read().strip())
    except Exception:
        pass
    if port:
        try:
            req = urllib.request.Request(
                'http://127.0.0.1:%d/nudge' % port,
                data=b'{}',
                headers={'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read().decode('utf-8', 'replace')
            if '"ok"' in body:
                log('nudge ok (port %d)' % port)
                return 0
        except Exception as e:
            log('nudge failed: %s' % e)
    # Server not running -> start it detached; startup recovery will ring
    # immediately if the alarm time is still within the grace window.
    exe = sys.executable
    if not os.path.basename(exe).lower().startswith('pythonw'):
        cand = os.path.join(os.path.dirname(exe), 'pythonw.exe')
        if os.path.exists(cand):
            exe = cand
    flags = 0
    if sys.platform == 'win32':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
    try:
        subprocess.Popen(
            [exe, os.path.join(APP_DIR, 'server.py')],
            cwd=APP_DIR,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log('started server (%s)' % exe)
    except Exception as e:
        log('failed to start server: %s' % e)
    return 0


if __name__ == '__main__':
    sys.exit(main())
