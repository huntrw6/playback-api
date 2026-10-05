# Playback Remote protocol (as observed)

Evidence labels: **CONFIRMED** = observed on a live Playback 8.5.4 / macOS 26.5.2. **INFERRED** = reasoned
from behaviour. **UNKNOWN** = not tested.

## Transport
- CONFIRMED: plain `ws://HOST:8080/` (RFC 6455, no TLS). Subprotocol **`pr-protocol`** is accepted with no
  authentication from loopback and from other hosts on the LAN. `ps-protocol` (Playback Sync/ChartBuilder) is
  refused for unpaired clients and was not pursued.
- CONFIRMED: the listener exists only while **Allow Remote Connections** is on; it did not persist across relaunch.
- CONFIRMED: multiple clients at once. Commands from one client are **broadcast to other clients but not echoed
  back to the sender**.
- CONFIRMED: client frames are masked JSON text. Server sends a `heartbeat` about once a second, always.
- Latency: command effects appear in the next heartbeat (~0.2–1 s). Messages from other clients arrive instantly.

## Server → client
```json
{"heartbeat":{"stateData":{"setlistSongID":91000001,"sequenceTime":12.4,
  "sequencerPlayState":{"playing":{}},"padPlayerPlayState":{"stopped":{}},
  "setlistCloudVersion":11,"setlistState":{"changed":{"_0":{"midiCuesNotSaved":{}}}}}}}
```
`sequencerPlayState` is the authoritative transport. The pad has its own state. There is **no fade field, no
section field, no song length, and no names**.

## Messages (sent by operators' clients, broadcast to everyone; also accepted as commands)
| Message | Fields | Notes |
|---|---|---|
| `transportPlay` | `playing: bool` | CONFIRMED play/pause |
| `transportReturnToStart` | – | CONFIRMED. Position→0 and **stops** the transport, also if sent while playing |
| `waveformSeek` | `sequenceTime` | CONFIRMED. Clamps to [0, end]. The UI blocks it while playing; the protocol does not |
| `waveformDoubleTap` | `setlistSongSectionID` | CONFIRMED section jump to the section start. IDs of other songs are ignored. While playing the jump is queued to the next boundary (INFERRED, 1 sample) |
| `waveformLoop` | `setlistSongSectionID`, `active` | CONFIRMED section loop; position wraps inside the section |
| `transportPad` | `playing: bool` | CONFIRMED. Pad state follows in 5–7 s (pad fade) |
| `transportFade` | `direction` 1=out, 0=in | CONFIRMED. Playback keeps running; **not visible in the heartbeat** |
| `setlistSelectSong` | `setlistSongID` | CONFIRMED. Stopped only |
| `transportNextSong` / `transportPreviousSong` | – | CONFIRMED. Stopped only, no wrap-around; no change at either end |
| `mixerInfiniteLoop`, `waveformPresetInfiniteLoop`, `transportInfiniteLoop` | unknown | Infinite loop is a Playback config option, not an operator control. Ten guessed payloads had no effect; not pursued |

Section IDs and song IDs are plain integers. Song IDs are not in setlist order.

## Reading the heartbeat
- **song.started**: play edge with position < 2 s that follows a select, return-to-start, seek-to-start, or an
  automatic transition. Otherwise **song.resumed**. A seek to 0 followed by play is indistinguishable from a
  restart, which is why `transportReturnToStart` is the clean restart signal.
- **Natural end**: the song ID changes with no select message. If the transport is stopped on the new song, the
  setlist is "open next, stay paused". If it is playing at ~0 s, it is "transition and keep playing". Both seen.
- **Last song**: at the end the selection wraps to song 1 and stops. CONFIRMED once.
- **Queued/loop jumps**: while playing, position moving by more than ~1.5 s with no command is `position.jump`.

## Discovery without credentials
- **Setlist order**: CONFIRMED. Step `transportPreviousSong` to the start, then `transportNextSong` to the end
  (about 2 s per song). Songs: 91000001, 91000002, 91000003, 91000004, 91000005.
- **Section IDs**: CONFIRMED. A valid ID moves the position; anything else is ignored. A burst of up to ~5000
  candidate IDs in one go works (50000 stalls the socket), then bisect. Sections are consecutive integers per
  song, one block per song, and the first section starts at 0.
- **Durations**: CONFIRMED to ~0.3 s, and operator-verified against Playback's display (m:ss): 309 / 335 / 324 / 337 /
  608 s. Method: seek to the end, play, and watch the heartbeat where the song ID changes. Measured 308.4 / 334.9 /
  323.0 / 336.5 / 607.2 s.
- **Names**: not available over this channel. Supply them from your own data.

## Not verified
- Song 5 (91000005) has one section; the operator confirmed that is correct (it is background music set to loop
  forever, so it never reaches its end). Its 608 s length was measured before it wrapped.
- Infinite loop is a Playback configuration option, not a wire message. It is out of scope.
- Setlist switching and behaviour across a Playback update.
