# Playback API (unofficial)

A small, dependency-free Python library and service that reads — and optionally controls — MultiTracks
**Playback** over its local-network Remote channel, and turns it into clean events and an HTTP/SSE API
that any automation tool (show control, lighting, scripts) can use.

> **Unofficial.** Not affiliated with or endorsed by MultiTracks. The protocol was worked out by observing
> Playback 8.5.4 on macOS 26.5 and is undocumented, so a Playback update can break it. Unknown messages are
> surfaced as `message.unknown` so changes are visible. Evidence for everything is in `PROTOCOL.md`.

## What you get
- Live state: song (id **and number 1–N**), position, duration/remaining, playing, pad, section, setlist order.
  No MIDI cues needed.
- Normalized events: `song.started`, `song.resumed`, `song.paused`, `song.stopped`, `song.changed`
  (with `continuesPlaying`), `section.jump`, `section.entered`, `fade.out/in`, `pad.on/off`, `position.jump`, …
- Control (opt-in): play, pause, return to start, seek, jump to section, pad, fade, loop, select/next/previous song.
- Addressing by **song number** (`3`) or **song id** (`91000003`), and sections by number within a song.
- Setlist and section discovery with no account access and no Playback files read.
- Runs on **any computer on the same network** as Playback. Nothing is installed on the Playback computer.

## Requirements
- Playback with **Allow Remote Connections ON**. It did not persist across an app relaunch in testing, so check it
  after restarting Playback. Port 8080 refuses connections while it is off.
- Network reach to the Playback computer's TCP **8080**. Python 3.9+. No packages.

## Quick start
Run it on the **Playback computer or any other computer on the same network**. Nothing is installed on the
Playback computer.
```bash
python3 -m playback_api find                    # is Playback running on THIS computer?
python3 -m playback_api find --scan             # no? search the connected network for it
python3 -m playback_api state                   # uses this computer's Playback
python3 -m playback_api state --scan            # ...or finds it on the network automatically
python3 -m playback_api listen --scan           # JSON-lines event stream (read-only)
python3 -m playback_api --host 192.168.1.50 state    # or name the device yourself (or set $PLAYBACK_HOST)
python3 -m playback_api walk-setlist --scan     # discover order (briefly moves the selection)
python3 -m playback_api select 3 --scan         # select song number 3 (stopped only)
python3 -m playback_api play 2 --scan           # select song 2, then play
python3 -m playback_api pause --scan
python3 -m playback_api serve --scan --setlist-file data/example-setlist.json                   # HTTP :8787, read-only
python3 -m playback_api serve --scan --allow-control --setlist-file data/example-setlist.json   # + POST /command/*
```
Flags such as `--scan`, `--host` and `--port` work before or after the command.

## Finding Playback
Order of lookup, stopping at the first hit:
1. `--host` (or `$PLAYBACK_HOST`) if you give one. Nothing is searched.
2. **This computer** (`127.0.0.1:8080`).
3. Only with `--scan`: the **last Playback that worked** (remembered in `~/.cache/playback-api/last-host.json`).
4. Only with `--scan`: every address on the networks this computer is attached to (or `--subnet 192.168.1.0/24`).
   Networks bigger than a /24 are narrowed to the /24 around this computer. Loopback, link-local and tunnel
   interfaces are skipped, and nothing outside your own network is ever contacted.

A device counts as Playback only if it completes the `pr-protocol` WebSocket handshake **and** sends a Playback
heartbeat, so other web servers on port 8080 are ignored. The scan sends no commands and takes a few seconds.
If it finds several, it uses the first and tells you; `find --all` lists them and `--host` picks one.
Without `--scan` the network is never searched. If nothing is found you get a message saying what to check.

Python:
```python
from playback_api.find import find_playback
from playback_api.client import PlaybackClient
hit = find_playback(scan=True)[0]                      # {'host','port','songId','source',...}
c = PlaybackClient(hit["host"], hit["port"], allow_control=True).start()
```
```python
from playback_api.client import PlaybackClient
c = PlaybackClient("192.168.1.50", allow_control=True,
                   on_event=lambda e: print(e["type"], e)).start()
c.wait_for(lambda s: s["songId"] is not None)
c.walk_setlist()                 # [91000001, 91000002, ...]  -> numbers 1..5 now work
c.select_song(3); c.play()
c.jump_to_section(4, song=2)     # section 4 of song 2 (needs a section map from the data file)
print(c.state["songNumber"], c.state["remaining"])
```

## Song numbers and the setlist
Playback only exposes opaque song IDs, which are **not** in setlist order. A song **number** is its 1-based
position in the setlist. The API learns that order in one of three ways:
1. **`walk-setlist`** (or the first command that needs it, if control is on). It steps Previous to the start, then
   Next to the end (about 2 s per song) and restores the selected song. Do it while stopped. No data file needed.
2. **A data file** (`--setlist-file`): used only if its `setlistVersion` matches the one Playback reports. If the
   setlist is edited, Playback's version changes, the API drops the order and asks for a re-walk.
3. Read-only without either: numbers are refused with a clear error; real IDs always work.

Integers below 10,000 are treated as numbers, larger ones as IDs.

## Data file (`data/example-setlist.json`)
Optional. Holds song order, durations, names and section maps, kept out of the code. Playback does not expose
names or lengths, so those come from the file (your own tools can write it). `durationSeconds` drives
`state.duration` / `state.remaining`. Section starts drive `section.entered` and `state.sectionNumber`.

## HTTP API
| Request | Purpose |
|---|---|
| `GET /state` | connection, songId/songNumber/songName/songCount, position, duration, remaining, playing, pad, sectionId/sectionNumber, fadedOut, setlist |
| `GET /events?since=N` | recent events with `seq > N` |
| `GET /stream` | Server-Sent Events, one named event per normalized event |
| `GET /setlist` | order with numbers, durations, names and sections |
| `POST /command/<name>` | control. 403 if control is off; 409 if a safety rule blocks it or a reference is unknown |

Commands: `play`, `pause`, `return-to-start`, `seek {seconds}`, `section {section, song?}`, `pad {on}`, `fade {out}`,
`loop-section {section, active, song?}`, `select-song {song}`, `next-song`, `previous-song`, `walk-setlist`.
`song` / `section` take a number or an ID (`songId` / `sectionId` also accepted).

## Safety defaults
- **Read-only unless you pass `--allow-control`.** Control is a separate permission.
- Song selection is refused unless the transport is **stopped** (Playback's own UI forbids it too).
- Playback's port has **no authentication**, and none is added here. Anyone who can reach 8080 can control
  Playback. Keep it on a trusted LAN. `serve` binds to `127.0.0.1` by default; put your own auth in front of it
  if you expose it.
- `walk-setlist` and section discovery move the selected song and position. Run them when idle.

## Known limits
- **Fade state is not in the heartbeat.** `fadedOut` is `null` (unknown) until a fade command is seen.
- **The last song wraps to song 1 and stops.** A song set to loop forever never ends.
- The pad starts with Play and follows pad commands about 5–7 s later.
- Names and lengths are not exposed by Playback (file-supplied). Infinite loop is a Playback config option and
  is not controllable or visible here.
- Remote Connections must be re-enabled after Playback restarts. The API retries quietly and reports
  `connected: false` with the reason.

## Tests
`python3 -m unittest discover -s tests` runs the offline tests, including a replay of a real 955-frame operator
session and a fake Playback on loopback for discovery. Nothing in the tests touches a real Playback.
