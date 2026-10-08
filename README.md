# Playback Application Programming Interface

Capture what **MultiTracks Playback** is doing — which song, play or pause, where it is — and control it from a
script or another computer on the local network. No MIDI cues. No extra software on the Playback computer.

## You need
- Playback running, with **Allow Remote Connections** turned **on** (it can turn off when Playback restarts).
- Python 3.9 or newer. Nothing else to install.

> Unofficial. Not made by or connected to MultiTracks. It can stop working if Playback changes.

## Install
Download the `.whl` file from the [latest release](../../releases/latest), then:

```bash
pip install playback_api-*-py3-none-any.whl
```
Now you can type `playback-api` anywhere. Or skip installing: download this folder and use
`python3 -m playback_api` from inside it.

## Try it
Run these (use `python3 -m playback_api` instead of `playback-api` if you didn't install):

```bash
playback-api find            # is Playback on this computer?
playback-api find --scan     # not here? search your network for it
playback-api state --scan    # show what Playback is doing now
playback-api listen --scan   # live feed: song started, paused, resumed, ...
playback-api replay capture.jsonl   # replay a saved capture offline
```

It looks on the computer you run it on first. `--scan` also searches your network. Without `--scan` it never
searches. If you know the address, use `--host 192.168.1.50` instead.

## Control Playback
Control is available but you have to ask for it. These commands send controls:

```bash
playback-api play 2 --scan     # select song 2, then play
playback-api pause --scan
playback-api select 3 --scan   # choose song 3 (only works while stopped)
```
Song numbers are the song's place in the setlist (1, 2, 3 ...). The first time, it learns the order by stepping
through the setlist, so do it when Playback is stopped.

## Record a rehearsal
```bash
playback-api capture rehearsal.jsonl --hours 5     # listens only; sends nothing
playback-api replay rehearsal.jsonl                # turn it into events later
```
It reconnects by itself, so it can run through a whole practice.

## Faster play/pause/stop
Playback only reports its transport state once a second. With `--fast` (or `fast_transport=True`), play, pause and
stop are announced from the command itself, about half a second sooner on average, and quick taps are no longer lost.
Those events carry `provisional: true`; the heartbeat then confirms them quietly, or you get `transport.reverted` with
the real state if it never does.
```bash
playback-api listen --fast
```

## Loops
```bash
curl -X POST localhost:8787/command/loop-once -d '{"active": true}'      # repeat the playing section once
curl -X POST localhost:8787/command/loop-infinite -d '{"active": true}'  # repeat until switched off
```
State shows `singleLoop`, `infiniteLoop` and `midiMuted`.

## Use it from other programs
Start a small web service, then call it from anything:

```bash
playback-api serve --scan                    # read-only
playback-api serve --scan --allow-control    # also lets programs control Playback
```
Then open `http://127.0.0.1:8787/state` for the current state, or `/stream` for a live event feed.
To control: `POST /command/play`, `/command/pause`, `/command/select-song` with `{"song": 3}`.

## Please know
- **There is no password.** Playback does not use one on this connection, so anyone on your network could
  control it. Use only on a network you trust.
- Playback does not share song names or lengths. You can add them in a file (see `data/example-setlist.json`).
- More detail is in `docs-reference.md` and `PROTOCOL.md`.

## License
GNU GPL v3. See `LICENSE`.
