# Iris: 15-second silent demo (shot list)

Silent, captions only. Outputs: `video/iris-15s.mp4` (LinkedIn, 1080p), `video/iris-15s.gif`
(README, 960 px, 12 fps) and `video/iris-thumb.png` (cover image). All footage comes from
`python gui.py --demo` (simulated devices, synthetic camera patterns). No participant data.

Style: each shot is a headline (with a small label above it) over a framed app window on the
brand background. Windows hold a fixed frame (no zooming) and fade in with a short rise
between shots. The Iris logo sits in the bottom-right corner until the results screen, where
a large logo takes over.

| # | Time | Label · headline | On screen |
|---|---|---|---|
| 1 | 0.0–3.0 s | THE PROBLEM · **6 devices. One missed step = lost data.** | Device Status panel: 2 webcams, 2 GoPros, Polar H10, mic, not yet checked. |
| 2 | 3.0–7.0 s | THE FIX · **Each step unlocks only after its prerequisites** | Warmup & Calibration: the 9 checklist items tick one by one, then "Continue to Performance" unlocks. |
| 3a | 7.0–9.0 s | ONE TIMELINE · **Every device logged to one shared timeline** | Device check: every row goes from "Checking…" to green "Connected" (about 1.4x speed, then holds). |
| 3b | 9.0–10.6 s | (same) | Performance phase starts: REC ● and a running timer. |
| 3c | 10.6–12.2 s | (same) | The demo session folder with `sync_manifest.json` highlighted, and one start event per device with millisecond timestamps. |
| 4 | 12.2–15.0 s | THE RESULT · **Zero lost sessions** | Stats rise in: **50** participants, **60+** trials, **1+ TB** captured. Then the Iris logo and "Built for UBC researchers studying flow in athletes". |

## How it's made

1. `python video/record_15s.py` drives the demo app and records four takes into
   `video/build/raw15/` (gitignored). Only Iris is allowed on screen; a take is deleted if
   anything else shows.
2. `python video/edit_15s.py` renders all three outputs from those takes and the demo session.
   Timings and crops are in its `SHOTS` table.

## Facts (confirmed 2026-11-07)

- Data volume: **1+ TB**.
- Scale: **50 participants, 60+ trials** (matches the README).
- Timing: **millisecond**, never microsecond.
- "Each step unlocks only after its prerequisites": the phases run in a fixed order, and the
  gated ones also need their checklist.
