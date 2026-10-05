"""Find a running Playback: this computer first, then (only if asked) the connected network.

Playback's Remote channel is a WebSocket on TCP 8080 that answers the `pr-protocol` subprotocol and then
sends a `heartbeat` about once a second. A candidate only counts as Playback if it completes that handshake
AND sends a heartbeat with Playback's `stateData`; any other service on port 8080 is ignored.

The scan is passive: one TCP connect per address, then a WebSocket handshake with the few that answer.
It never sends a command. It only looks at the networks this computer is directly attached to (or the
CIDR ranges you pass explicitly), never the wider internet.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional

from .ws import WebSocket, WSClosed, WSHandshakeError

DEFAULT_PORT = 8080
MAX_HOSTS_PER_NETWORK = 1024  # larger networks are narrowed to the /24 around this computer's address
CACHE_PATH = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(os.path.expanduser("~"), ".cache")),
                          "playback-api", "last-host.json")


# ---- identifying Playback ----------------------------------------------------------------------
def probe(host: str, port: int = DEFAULT_PORT, timeout: float = 3.0) -> Optional[dict]:
    """Return {'host','port','songId','setlistVersion','playing'} if Playback answers there, else None."""
    try:
        ws = WebSocket(host, port, timeout=timeout)
    except (OSError, WSHandshakeError):
        return None
    try:
        end = time.time() + timeout
        while time.time() < end:
            raw = ws.recv(timeout=max(0.1, end - time.time()))
            if raw is None:
                continue
            try:
                sd = json.loads(raw)["heartbeat"]["stateData"]
                return {"host": host, "port": port, "songId": sd["setlistSongID"],
                        "setlistVersion": sd.get("setlistCloudVersion"),
                        "playing": "playing" in sd.get("sequencerPlayState", {})}
            except (ValueError, KeyError, TypeError):
                continue
    except (OSError, WSClosed):
        pass
    finally:
        ws.close()
    return None


# ---- which networks is this computer on? -------------------------------------------------------
def _mask_to_prefix(mask: str) -> Optional[int]:
    try:
        n = int(mask, 16) if mask.lower().startswith("0x") else int(ipaddress.IPv4Address(mask))
        return bin(n).count("1")
    except ValueError:
        return None


def parse_interfaces(text: str) -> list:
    """Parse `ip -o -4 addr` or `ifconfig` output into [(ip, prefixlen)]. Pure function (testable)."""
    out = []
    for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", text):  # ip -o -4 addr
        out.append((m.group(1), int(m.group(2))))
    for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)\s+(?:-->\s+\S+\s+)?netmask\s+(\S+)", text):  # ifconfig
        p = _mask_to_prefix(m.group(2))
        if p is not None:
            out.append((m.group(1), p))
    return out


def usable_networks(ifaces: Iterable) -> list:
    """Keep real, scannable LAN networks: no loopback, link-local, point-to-point/host routes or tunnels."""
    nets, seen = [], set()
    for ip, prefix in ifaces:
        a = ipaddress.IPv4Address(ip)
        if a.is_loopback or a.is_link_local or a.is_multicast or prefix >= 31 or prefix == 0:
            continue
        net = ipaddress.IPv4Network("%s/%d" % (ip, prefix), strict=False)
        if net.num_addresses - 2 > MAX_HOSTS_PER_NETWORK:
            net = ipaddress.IPv4Network("%s/24" % ip, strict=False)
        if net not in seen:
            seen.add(net)
            nets.append(net)
    return nets


def local_networks() -> list:
    ifaces: list = []
    for cmd in (["ip", "-o", "-4", "addr", "show"], ["/sbin/ifconfig"], ["ifconfig"]):
        try:
            ifaces = parse_interfaces(subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout)
        except (OSError, subprocess.SubprocessError):
            continue
        if ifaces:
            break
    if not ifaces:  # last resort: the address used for the default route (sends no packets), assume /24
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 9))
            ifaces = [(s.getsockname()[0], 24)]
            s.close()
        except OSError:
            pass
    return usable_networks(ifaces)


def own_addresses() -> set:
    out = {"127.0.0.1"}
    for cmd in (["ip", "-o", "-4", "addr", "show"], ["/sbin/ifconfig"], ["ifconfig"]):
        try:
            out |= {ip for ip, _ in parse_interfaces(subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout)}
            break
        except (OSError, subprocess.SubprocessError):
            continue
    return out


# ---- scanning ------------------------------------------------------------------------------------
def _open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_hosts(hosts: Iterable, port: int = DEFAULT_PORT, connect_timeout: float = 0.6,
               probe_timeout: float = 3.0, workers: int = 128) -> list:
    """Return probe() results for every host that is really Playback."""
    hosts = list(hosts)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        openers = [h for h, ok in zip(hosts, ex.map(lambda h: _open(h, port, connect_timeout), hosts)) if ok]
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = [r for r in ex.map(lambda h: probe(h, port, probe_timeout), openers) if r]
    return results


def _remember(host: str, port: int) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump({"host": host, "port": port, "ts": time.time()}, f)
    except OSError:
        pass


def _recall() -> Optional[tuple]:
    try:
        with open(CACHE_PATH) as f:
            d = json.load(f)
        return d["host"], int(d["port"])
    except (OSError, ValueError, KeyError):
        return None


def find_playback(port: int = DEFAULT_PORT, scan: bool = False, find_all: bool = False,
                  subnets: Optional[list] = None, log: Callable[[str], None] = lambda m: None) -> list:
    """Look on this computer first. If nothing is there and `scan` is true, try the last host that worked,
    then scan the attached networks (or `subnets`). Returns a list of probe() dicts, each with 'source'."""
    found = []
    here = probe("127.0.0.1", port, timeout=2.5)
    if here:
        here["source"] = "this computer"
        found.append(here)
        if not find_all:
            return found
    elif not scan:
        return []
    log("Playback not running on this computer." if not here else "Scanning the network for more...")

    if not find_all or not found:
        cached = _recall()
        if cached:
            r = probe(cached[0], cached[1], timeout=2.5)
            if r:
                r["source"] = "last known host"
                found.append(r)
                if not find_all:
                    return found

    nets = [ipaddress.IPv4Network(s, strict=False) for s in subnets] if subnets else local_networks()
    if not nets:
        log("No network to scan: this computer has no usable LAN address.")
        return found
    mine = own_addresses()
    have = {(f["host"], f["port"]) for f in found}
    for net in nets:
        log("Scanning %s for Playback on port %d ..." % (net, port))
        hosts = [str(h) for h in net.hosts() if str(h) not in mine]
        for r in scan_hosts(hosts, port):
            if (r["host"], r["port"]) not in have:
                r["source"] = "network scan"
                found.append(r)
                have.add((r["host"], r["port"]))
    if found:
        _remember(found[0]["host"], found[0]["port"])
    return found
