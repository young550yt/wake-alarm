# Wake System

A personal alarm for one Windows laptop. The **backend owns reliability**: it
schedules the alarm, plays escalating sound through Windows' native audio
(`winsound`), speaks your commitment with TTS, validates dismissal tasks
server-side, and detects missed alarms after restarts. The browser page is a
client only — closing or minimizing it does **not** stop the alarm.

Source: https://github.com/young550yt/wake-alarm (private)

## Quick start

| | |
|---|---|
| Start (no console window) | double-click `start.bat` |
| Open the app | http://127.0.0.1:8520/ |
| First-time Windows setup | click **🛠 Run Windows setup** in the app → approve the one UAC prompt |

Python 3.12 is required (already installed on this machine).

## How the alarm works

1. **Arm it** — set a wake time, pick days, write a *commitment* (why you are
   getting up) and optionally a *letter to your future self*.
2. **At ring time** a full-screen window opens (if no page is already open)
   and the sound escalation starts:
   - **Stage 1 — gentle chime** (moderately noticeable)
   - **Stage 2 — alternating two-tone pattern** (different pattern defeats habituation)
   - **Stage 3 — urgent triple beeps** (louder, still within safe levels)
   - **Stage 4 — beeps + low undertone + spoken voice** reading your
     commitment and letter every N seconds
3. **Dismiss with a real waking action** — maths problem, exact sentence
   retype, commitment quiz (with decoys), or colour sequence. The alarm keeps
   ringing until the task is solved. The task is an extra layer, *not* proof
   you are fully awake.
4. **Snooze** — default 5 minutes, max 2 (configurable, can be disabled).
   The ring screen shows a live **"N left"** counter driven by the server's
   own tally; at 0 the button disables itself and the server rejects any
   further snooze request — the UI can never offer a snooze the backend
   would refuse.

Stage durations are configurable (default 45 s / 45 s / 60 s, then voice every
20 s). These are design choices, not scientifically established intervals.

## Windows reliability

Click **🛠 Run Windows setup** once. It applies (and the status card verifies):

| Setting | Value | Why |
|---|---|---|
| Scheduled task `WakeAlarm` (WakeToRun) | registered on every alarm save | Windows wakes the PC at alarm time and runs `wake_task.py` |
| Allow wake timers (AC + battery) | enabled | lets the scheduled task wake from sleep |
| Lid close (plugged in **and** on battery) | **Do nothing** | closing the lid doesn't sleep the alarm away |
| Start with Windows | enabled (HKCU Run key) | server auto-starts at login |

The **Windows status card** shows everything the server can verify, including
the scheduled task's health (state, next run, last run result) and the lid /
wake-timer settings for **both AC and battery**. If the task is missing, its
last run failed, or its next-run time doesn't match your alarm, a visible
warning appears in the UI instead of failing silently.

`wake_task.py` (the scheduled action) first nudges the running server
(`/nudge`), and starts it if it is down. On startup the server checks for
occurrences inside the grace window (default 45 min) and **rings immediately**
if one was missed — with boot-time context so you can see *why* it was missed
(PC asleep/off until HH:MM, app not running, or rang but never dismissed).

### What this can and cannot do

| Laptop condition | Alarm works? |
|---|---|
| Screen off / locked / another app focused | Yes |
| Lid closed (plugged in **or on battery**, after setup) | Yes |
| Asleep | Yes, if wake timers + WakeToRun task are active |
| Hibernated | Not enabled on this machine (Modern Standby laptop) |
| Shut down | No |
| Battery dead | No |
| Forced shutdown / OS power restrictions | No — and this app does **not** try to defeat them |

**Modern Standby caveat:** this laptop has no classic S3 sleep — it uses
Modern Standby, where Windows keeps some wakers but can also deny wake timers
under power pressure. The only honest proof is a real wake: press
**💤 Sleep test** in the app, which schedules a one-shot task 2 minutes out
and puts the PC to sleep; if the alarm rings and wakes the machine,
`wake_task.py` records the outcome in `sleep_test_result.json` and the app
shows it. Run **🛠 Run Windows setup** (admin, one UAC prompt) first to have
`powercfg /waketimers` confirm which timers are actually armed.

Low battery: if charge drops to the warning threshold (default 30 %) while
**not charging** and the alarm is armed, a banner warns you to plug in before
bed. The header chip always shows live battery %.

## Security model

The server binds to `127.0.0.1` only (never a public interface) and applies:

| Check | Result |
|---|---|
| Request `Host` not loopback | 403 — blocks DNS-rebinding / cross-host access |
| `Origin` / `Referer` header present but not our own origin | 403 — blocks cross-site form posts (CSRF) |
| POST `Content-Type` ≠ `application/json` | 415 — no simple-content-type CSRF |
| Second copy of `server.py` launched | exits immediately ("another instance already serves port 8520") — one process, one port, no duplicated alarms |

There is no login: the app is intentionally local-only and unauthenticated
because only processes on this machine can reach it.

## Monitoring & statistics

Every alarm logs: scheduled time, actual dismissal time, ring duration,
snoozes, missed flag + reason, battery % / charging state at ring time, boot
time, and task type. The **Sleep & alarm history** card shows totals
(due / dismissed / missed / snoozes / average time to dismiss / % dismissed
within 5 minutes) plus a per-alarm table — so you can evaluate which
configuration actually works instead of relying on assumptions.

Missed alarms raise a banner on startup until you acknowledge it.

## Settings reference

| Setting | Default | Notes |
|---|---|---|
| Stage 1/2/3 duration | 45 / 45 / 60 s | min 5 s each |
| Voice reminder interval | 20 s | stage 4 only |
| Dismissal task | random | maths / sentence / quiz / sequence |
| Maths difficulty | easy | easy = addition, medium = multiplication, hard = three steps |
| Snooze | 5 min × 2 | 0 disables snooze entirely |
| Ring grace | 45 min | how late it may still ring after the scheduled time |
| Low-battery warning | 30 % | only warns when not charging |
| Auto-open fullscreen | on | only if no page has checked in for 15 s |

Sounds are synthesized at first startup into `sounds/stage1-4.wav` (no
downloads). To use your own recordings, replace those WAV files. Drop-in
testing is available in the app (**🔊 Test current stage sound**,
**🗣 Test voice**).

## Files

| File | Purpose |
|---|---|
| `server.py` | backend: scheduler, sound engine, TTS, tasks, missed detection, HTTP API (port 8520) |
| `index.html` | the UI + full-screen ring interface |
| `win_integration.ps1` | status / setup / register-task / battery / boot-time queries |
| `wake_task.py` | scheduled-task action: nudge server or start it |
| `start.bat` | launches the server hidden (pythonw) |
| `config.json`, `history.json` | settings and alarm history (not committed to git) |
| `server.log`, `wake_task.log` | diagnostics |

Git: runtime state (`config.json`, `history.json`, logs, locks, `sounds/`) is
gitignored — only the source files above are tracked.

## Development

```sh
python -m py_compile server.py wake_task.py   # syntax check
python server.py                              # runs on http://127.0.0.1:8520
```

## Important note about sleeping medication

Some medications cause residual sedation and can impair your ability to hear
or respond to alarms after waking. **Software cannot compensate for
medication-related sedation.** If you are regularly unable to wake to alarms,
tell the prescribing psychiatrist or doctor so they can review whether the
medication, its timing, or its effects are contributing. **Do not change the
dose or timing yourself.** Until that is assessed, having a trusted adult help
ensure you are awake is safer than relying exclusively on software.
