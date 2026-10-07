# Playback user guide: notes that explain protocol signals

Source: https://helpcenter.multitracks.com/en/articles/4944485-playback-user-guide (page dated 2026-07-31, read 2026-10-07).
This is a user manual, not a protocol spec. It says what each control means; it says nothing about wire messages.
Evidence labels: DOCUMENTED = stated in the guide. Protocol facts live in PROTOCOL.md.

## Transport area (left to right) - DOCUMENTED
Tempo/time signature, time display, Pad on/off, Back To Start, Play/Pause, Fade, Edit mode selector.

## Fade - DOCUMENTED
Fades the *tracks* in or out while playing. Click, Guide and Pad Player keep playing. (Matches the the operator's "mute/unmute" description.)

## Loops - DOCUMENTED (Pro features)
- Section loop icon on a section: cue that section for a single loop. Playback replays it when it reaches the end.
- Loop button (console, while playing): arms the *currently playing* section for ONE repeat; the button then disables itself and the song continues.
- Infinite Loop button (console): arms the current section to loop until pressed again.
- Infinite Loop Preset (Premium; song map edit): per-section setting; when Playback reaches that section it loops until Infinite Loop is pressed. (Likely how a background song is made to repeat. This is a configuration option, out of scope for the API unless a wire message is ever observed.)
- Disable Loop (MIDI cue setting): a cue fires once and is ignored on later passes of a looping section.

## Live navigation - DOCUMENTED
- Live ReOrder: while playing, Left/Right arrows browse sections; Return/Enter confirms the jump to the new section. While paused, arrows just browse sections.
- Live Crossfade: while playing, pressing a number key crossfades to that song number in the setlist. While paused, the number selects that song.
- Mac keyboard shortcuts (only while Playback is foregrounded): Space = Play/Pause, P = Pad, F = Fade, L = Loop, I = Infinite Loop.
- MUTE MIDI (console button): temporarily stops all MIDI cue output.

## Setlists - DOCUMENTED
- Setlists open from MultiTracks.com, ChartBuilder or another Playback device; save to the cloud so versions sync across devices.
- Playback saving a new version of the synced setlist updates ChartBuilder automatically (relates to `setlistCloudVersion` changing and our `setlist.changed`).
- Opening a different setlist in Playback prompts ChartBuilder to open it.
- Setlist edit modes: Setlist/Song Map (order, sections), MIDI Cues, MIDI Mapping (map controllers to Playback functions), Pad Player settings.

## Playback Sync and Remote - DOCUMENTED
- Playback Sync is a broadcast switched on in Playback; ChartBuilder sees available Playback devices, connects, and follows playback (section scroll/highlight, song switches via programmed transitions, Loop, Live ReOrder, Live Crossfade, section scrolling and song selection while paused).
- Playback Remote: a Playback device can act as a host so other Playback devices on the same network can control it.
- Settings has a **Network** picker choosing which IP address/network Sync and Remote broadcast on (matters on a Mac with several networks, e.g. Wi-Fi + Ethernet + VPN). A wrong choice can make a Playback invisible from another computer.

## Still unobserved on the wire (to look for in captures) - UNKNOWN
Live ReOrder confirm, Live Crossfade (as distinct from a normal song change), single Loop arming and auto-disarm, Infinite Loop on/off, MUTE MIDI, setlist-opened-from-elsewhere, Network picker effect.

## Findings from a 4h16m passive capture on the test computer (2026-10-07 00:21-04:37 UTC, ~28.8k frames) - CONFIRMED unless noted
Message types seen (new ones marked *): heartbeat, setlistSelectSong, transportPlay, transportPad, waveformSeek, waveformDoubleTap, `contentLoadSetlist`*, `contentUpdateSetlist`*, `mixerTrackVolume`* (13.8k, fader moves), `mixerTrackMute`*, `mixerTrackSolo`*, `mixerInfiniteLoop`* {"active":bool}, `transportNavigateToSongMapElementIndex`* {"index":n}, `setlistSelectSongTransition`* {"songIndex","transition"}, `audioDeviceChanged`*.
- Infinite loop IS observable: `mixerInfiniteLoop {active:true|false}`. It can be armed while stopped (before Play). While active and playing, heartbeat `sequenceTime` jumps back by the section length repeatedly (e.g. 115.6 -> 94.0 about every 22 s). Backward jumps with active=false are seeks or other loops.
- Setlist identity: `contentLoadSetlist` carries `setlistID` and `setlistName` when a setlist is opened. `setlistCloudVersion` is PER SETLIST and is not monotonic across setlists (seen 11, 3, 1, 2, 4). A saved song order should be keyed by `setlistID` (plus version), not version alone. Heartbeat `setlistSongID` can be absent (509 heartbeats) while a setlist loads. `setlistState` values: ready, downloadingContent, changed/setlistUnsaved, changed/midiCuesNotSaved.
- `transportNavigateToSongMapElementIndex {index}` (251 times, index 0-20): section browse cursor (arrow keys / Live ReOrder). Sent while stopped it does NOT move `sequenceTime` (tested live, indices 0-23, then restored). It likely needs a confirm (Return) while playing to jump. UNKNOWN: the confirm message.
- `setlistSelectSongTransition {songIndex, transition}`: transition values seen 0, 1, 4. Meaning UNKNOWN (likely crossfade/transition type for Live Crossfade).
- `mixerTrackVolume` is high-volume noise (fader moves); `audioDeviceChanged {}` appears when the audio device changes. Neither affects song state; ignore them for StagePilot.

## Active experiments on the test computer (2026-10-07, tracks faded out, everything restored) - CONFIRMED
Playback state at test time: setlist version 4, two songs reachable via Next, song order walked by Next/Previous.
- **Section browse is a cursor, not a jump.** `transportNavigateToSongMapElementIndex {index}` while STOPPED does not move the playhead, and a following `transportPlay` just starts from the current position (t=0 -> ~1.4 s after 2.2 s), so the index is NOT a way to start from a section. While PLAYING it also does not jump by itself (t advanced +2.0 s as normal). Sending `transportPlay {playing:true}` again while playing did nothing visible. The real confirm (Return key in Live ReOrder) is still UNKNOWN. In the operator capture, 55 browse messages while playing were followed within 2 s by `transportPlay` (74 times), `waveformSeek` (1) or `mixerTrackSolo` (2), so the the operator's confirm is probably a `transportPlay` after the cursor moves, OR the confirm is only a UI action with no wire message.
- **Section jump that works:** `waveformDoubleTap {setlistSongSectionID}` (the operator capture shows a 37 s jump from one) and `waveformSeek {sequenceTime}`.
- **Song change while playing (`setlistSelectSong` sent by us):** Playback changed the selected song and STOPPED the sequencer (t=0, stopped). It did not crossfade. So a live crossfade is not triggered by `setlistSelectSong`. Likely `setlistSelectSongTransition` is the crossfade trigger; sending it while stopped with transition 0, 1 or 4 changed nothing (no state or other messages), so it needs the song to be playing, or a different songIndex than the current one. UNKNOWN. Not tested while playing.
- **Single-loop guesses `mixerLoop`, `transportLoop`, `mixerSectionLoop`:** no effect (playhead kept advancing, no other messages). (Superseded in v2.0: the single Loop button is `mixerLoop {active}`; see the summary at the end.) The infinite loop message `mixerInfiniteLoop {active}` is confirmed from the operator capture.
- **Setlist payloads are small.** `contentLoadSetlist` carries `setlistID`, `setlistName`, `isDemo`, `liveData.setlistSnapshot.version`, and arrays that were EMPTY in every capture (`items`, `rentalsData`, `contentMetadataVersions`, `modularClickSongData`). Song titles are not in it, which is why names cannot be read. `contentUpdateSetlist` carries `rentalData` (per song `contentID`, `order`, `ownershipType`, `isPreview`) and `modularClickSongData` (per song `songIndex`, `modularClickLength`) for 5 songs. `contentID` per `order` may let StagePilot map a song to MultiTracks catalogue content: UNVERIFIED.
- Safety notes for tests: `transportFade` direction 1 = out, 0 = in. The pad turns off over about 5-7 s. macOS has no `timeout` command.

## Second experiment round on the test computer (2026-10-07, tracks faded out, everything restored) - CONFIRMED
All sent while a song was PLAYING; "no effect" = the playhead advanced normally and Playback broadcast nothing.
- `setlistSelectSongTransition {songIndex 0|1, transition 0|1|4}`: no effect, so it is not a command Playback accepts as a crossfade trigger (it appears only as something the UI or another client broadcasts). Treat as an observed-only message.
- `transportNextSong` while playing: no effect (the song and playhead continued). Next/Previous only work while stopped.
- `waveformDoubleTap {setlistSongSectionID: 0}`: no effect, because 0 is not a real section ID. Real section IDs are needed.
- Guessed names with `{active:true}` all had no effect: mixerLoopOnce, mixerSingleLoop, transportSingleLoop, waveformSingleLoop, mixerLoop, waveformLoopCurrent, transportConfirmReOrder, transportReorderConfirm, waveformReorder, transportLiveReorder, mixerMuteMIDI, mixerMuteMidi, transportMuteMIDI. (Superseded in v2.0: sends were inconclusive because Playback does not echo to the sender. A second listening connection showed `mixerLoop` and `mixerMuteMIDI` are real; only Live ReOrder confirm and Live Crossfade remain unknown.)

## v2.0 summary of what is now CONFIRMED vs still unknown
- CONFIRMED and supported by the API: `mixerInfiniteLoop {active}`, `mixerLoop {active}` (single Loop, disarms itself after one wrap), `mixerMuteMIDI {active}` (MUTE MIDI), `waveformSeek`, `waveformDoubleTap`, `waveformLoop`, `transportPlay/Pad/Fade/ReturnToStart`, `setlistSelectSong`.
- Observed only (watch, do not send): `transportNavigateToSongMapElementIndex` (browse cursor), `setlistSelectSongTransition`, `contentLoadSetlist`, `contentUpdateSetlist`, `mixerTrackVolume/Mute/Solo`, `audioDeviceChanged`.
- Still unknown: the Live ReOrder confirm and Live Crossfade messages (probably UI-only). Record an operator pressing them with `playback-api capture` to learn them.
