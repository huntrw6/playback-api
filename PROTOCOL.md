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
| `mixerInfiniteLoop` | `active: bool` | CONFIRMED (v2.0). Arms the *current* section to repeat until switched off; it can be armed while stopped. While armed and playing, `sequenceTime` jumps back to the section start each time the section ends (tested live: wraps at ~7.2-7.4 s in a 7.5 s first section). The heartbeat does not carry the loop state; read it from this message |
| `mixerLoop` | `active: bool` | CONFIRMED (v2.0). The Loop button: repeats the playing section **once**, then switches itself off. Playback sends no message for the self-disarm; the API infers it from the wrap (`loop.single` `active:false`, `reason:"wrapped"`). Tested live: armed at ~1.5 s, wrapped 7.3 s -> 0.7 s once, then played on |
| `mixerMuteMIDI` | `active: bool` | CONFIRMED (v2.0). The MUTE MIDI toggle (stops MIDI cue output). Playback relays it; Playback's own MIDI output was not measured |
| `transportNavigateToSongMapElementIndex` | `index` | OBSERVED 251x in bursts (index 0..20) while the operator stepped through sections; the position then moved. Sent by a client it is relayed but **has no effect** (re-tested in v2.0 while stopped and while playing: it is a browse cursor, not a jump, and it does not start playback from that section; tested at several positions, while stopped and playing, with several payload shapes), so it looks like a notification from the Playback app, not a command |
| `setlistSelectSongTransition` | `songIndex`, `transition` | OBSERVED 6x. `transition` 0, 1 and 4 seen; meaning unknown. A client-sent copy is relayed with no effect |
| `contentLoadSetlist` | `setlistData{setlistID, isDemo, setlistName, liveData…}` | OBSERVED 3x, a setlist was loaded on the Playback computer. Contains the setlist name: treat as private |
| `contentUpdateSetlist` | `rentalData[]`, `modularClickSongData[]` | OBSERVED 3x, content metadata refresh |
| `mixerTrackVolume` | `mappingIDs[]`, `level` 0..1 | OBSERVED 13,816x (about 55% of all frames) while faders moved. `TrackVolume_SongTrack_<n>` or `TrackVolume_Bus_<n>` |
| `mixerTrackMute` / `mixerTrackSolo` | `mappingIDs[]`, `muteState` / `soloState` = `{muted|unmuted: {}}` / `{soloed|unsoloed: {}}` | OBSERVED 17x / 198x |
| `audioDeviceChanged` | `{}` | OBSERVED 4x. Both long stalls below ended with one |

**Playback relays most messages a client sends to all other clients without checking them** (tested with a track number that does not exist). It does *not* relay a name it does not know: about 230 plausible guesses were tried and only `mixerLoop` (new) was recognised. Use a second listening connection as the "does Playback know this command" oracle. The sender itself gets no echo. Seeing a message on the wire is therefore not proof that Playback acted on it. Only the heartbeat is the truth. Tested with a second observer connection.

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

## What v2.0 added (active tests on a live Playback, tracks faded out, everything restored)
- `setlistCloudVersion` is **per setlist** (11, 3, 1, 2, 4 were all seen), not an identity. `contentLoadSetlist` carries the setlist's own `setlistID` and name: key a saved song order by `setlistID` plus version.
- `setlistSelectSong` while playing switches the song and **stops** the transport (no crossfade). `transportNextSong` / `transportPreviousSong` while playing do nothing.
- `setlistSelectSongTransition` is relayed but changes nothing, with every value tried (0, 1, 4), playing or stopped.
- The user guide describes Live ReOrder (Return key) and Live Crossfade (number keys while playing). Their wire messages were **not** found by sending or by guessing, so they are probably UI-only or need a Mac keyboard path; they can only be learned by recording an operator pressing them (`playback-api capture`).
- `transportFade` direction 1 = out, 0 = in; the pad fades out over about 5-7 s after `transportPad {playing:false}`.

## Song length (v2.2, measured)
- `waveformSeek` far past the end is **clamped** by Playback and the next heartbeat reports the clamped position. **The clamped playhead is the start of the song's final measure** (operator-confirmed), which is where the song ends for a service, so for countdowns and service plans it is the song's length, not an approximation. On five real songs it read 279.53 / 265.85 / 260.17 / 410.83 / 606 s. Playing a song to its very last audio reaches a later point (282.2 / 267.6 / 264.5 / 414.6 / 607.3 s on the same songs), which is the end of the audio tail, not the end of the last measure.
- The true end is the last heartbeat position seen while playing, plus about 0.5 s (heartbeats are 1 s apart). Repeated runs agreed to within 0.3 s.
- What the end does depends on the song: the next song is selected and stopped (4 of 5), or the next song starts playing by itself (1 of 5, the `setlistSelectSongTransition` behaviour). Playback has no field that says which.
- No tempo or BPM field exists in any message.

## Latency (measured end to end, v2.1)
Playback relays a client's command to every other client within **7-50 ms** over a routed VLAN (about 1 ms ping). It does **not** put play/pause/return in any message of its own: the new state appears only in the once-per-second heartbeat, so a state-based listener sees play/pause/stop **31-946 ms (median ~360 ms) late**, and a play+pause pair under one second apart is **invisible**. `fast_transport` reads the command itself instead (median 430 ms earlier on a real 4-hour capture; 214 of 215 events matched; 50 quick toggles the heartbeat path missed; 0 unconfirmed). It is a prediction until the heartbeat confirms it. It is only as good as the commands that reach the wire; Playback's own buttons do relay (play appeared 249 times in an operator capture and every heartbeat change in the capture followed one).
- Selecting another song while one is playing stops the transport. Only the select message and a heartbeat with a new song id and `stopped` show it; v2.1 reports `song.stopped` for the old song (`reason: song-selected`).

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
- The Live ReOrder confirm, Live Crossfade, and any per-section Infinite Loop *Preset* message (not seen).
- The meaning of `setlistSelectSongTransition.transition` values (0, 1, 4).
- Whether Playback itself sends `transportNavigateToSongMapElementIndex`, or only the Playback Remote app does.
- Setlist switching and behaviour across a Playback update.
