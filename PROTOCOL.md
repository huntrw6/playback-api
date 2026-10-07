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
| `mixerInfiniteLoop` | `active: bool` | OBSERVED 15x in a practice capture (operator toggling the loop, 8 on / 7 off). Sent by a client it is relayed to others but the heartbeat does not change, so its effect on Playback is **not verified**. Guessed `waveformPresetInfiniteLoop` / `transportInfiniteLoop` payloads had no effect |
| `transportNavigateToSongMapElementIndex` | `index` | OBSERVED 251x in bursts (index 0..20) while the operator stepped through sections; the position then moved. Sent by a client it is relayed but **has no effect** (tested at several positions, while stopped and playing, with several payload shapes), so it looks like a notification from the Playback app, not a command |
| `setlistSelectSongTransition` | `songIndex`, `transition` | OBSERVED 6x. `transition` 0, 1 and 4 seen; meaning unknown. A client-sent copy is relayed with no effect |
| `contentLoadSetlist` | `setlistData{setlistID, isDemo, setlistName, liveData…}` | OBSERVED 3x, a setlist was loaded on the Playback computer. Contains the setlist name: treat as private |
| `contentUpdateSetlist` | `rentalData[]`, `modularClickSongData[]` | OBSERVED 3x, content metadata refresh |
| `mixerTrackVolume` | `mappingIDs[]`, `level` 0..1 | OBSERVED 13,816x (about 55% of all frames) while faders moved. `TrackVolume_SongTrack_<n>` or `TrackVolume_Bus_<n>` |
| `mixerTrackMute` / `mixerTrackSolo` | `mappingIDs[]`, `muteState` / `soloState` = `{muted|unmuted: {}}` / `{soloed|unsoloed: {}}` | OBSERVED 17x / 198x |
| `audioDeviceChanged` | `{}` | OBSERVED 4x. Both long stalls below ended with one |

**Playback relays every message a client sends to all other clients, unmodified, without validating it** (tested with a track number that does not exist). Seeing a message on the wire is therefore not proof that Playback acted on it. Only the heartbeat is the truth. Tested with a second observer connection.

Section IDs and song IDs are plain integers. Song IDs are not in setlist order.

## Reading the heartbeat
- **song.started**: play edge with position < 2 s that follows a select, return-to-start, seek-to-start, or an
  automatic transition. Otherwise **song.resumed**. A seek to 0 followed by play is indistinguishable from a
  restart, which is why `transportReturnToStart` is the clean restart signal.
- **Natural end**: the song ID changes with no select message. If the transport is stopped on the new song, the
  setlist is "open next, stay paused". If it is playing at ~0 s, it is "transition and keep playing". Both seen.
- **Last song**: at the end the selection wraps to song 1 and stops. CONFIRMED once.
- **Queued/loop jumps**: while playing, position moving by more than ~1.5 s with no command is `position.jump`.

## Heartbeat facts from a 4 h 16 min practice capture
- Interval 1.000 s (p99 1.021 s) over 14,136 heartbeats.
- `setlistSongID` is **absent** while a setlist is empty or loading (509 heartbeats, about 8.5 minutes in the capture). Consumers must accept a missing song.
- `setlistState` values: `ready`, `downloadingContent`, `changed{_0:{setlistUnsaved}}` and `changed{_0:{midiCuesNotSaved}}`. The `changed` states are about 40% of the capture: they mean "edited and not saved", not a new setlist, and `setlistCloudVersion` stays the same.
- `setlistCloudVersion` moves when a different setlist is loaded (11, 1, 2, 3, 4 in this capture).
- **Stalls without a disconnect:** Playback stopped sending anything, with the socket still open, for 925 s and again for 319 s. Both ended with `audioDeviceChanged`. The computer did not sleep. A connected socket is not proof of a live Playback: use heartbeat age (the client treats 5 s of silence as a lost connection).
- The pad was reported on about 76% of the time; it is independent of the sequencer.

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
- `mixerInfiniteLoop` is a wire message (see above), but nothing in the heartbeat shows the loop state and its effect when sent by a client is not verified. Controlling it is out of scope.
- The meaning of `setlistSelectSongTransition.transition` values (0, 1, 4).
- Whether Playback itself sends `transportNavigateToSongMapElementIndex`, or only the Playback Remote app does.
- Setlist switching and behaviour across a Playback update.
