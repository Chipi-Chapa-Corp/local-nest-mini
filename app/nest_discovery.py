"""Find one owned Nest by MAC and publish a short-lived verified address lease."""

from collections import deque
from http.client import HTTPException
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request

LEASE_SECONDS = 20


def set_ipv4_address(nft, table, address):
    address = str(ipaddress.IPv4Address(address)) if address else None
    elements = (f'add element ip {table} nest_ipv4 {{ {address} timeout {LEASE_SECONDS}s }}\n'
                if address else '')
    subprocess.run([nft, '-f', '-'], text=True, check=True, capture_output=True, timeout=3,
                   input=f'flush set ip {table} nest_ipv4\n' + elements)


class VerifiedNest:
    def __init__(self, mac, path, *, clock=time.time):
        self.mac, self.path, self.clock = mac.lower(), Path(path), clock
        self.lock = threading.Lock()
        self.address = None
        self.expires = 0

    def current(self):
        with self.lock:
            return self.address if self.clock() < self.expires else None

    def publish(self, address):
        address = str(ipaddress.IPv4Address(address)) if address else None
        now = self.clock()
        payload = {'address': address, 'mac': self.mac,
                   'expires_at': now + LEASE_SECONDS if address else 0}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False) as out:
            temporary = Path(out.name)
            try:
                json.dump(payload, out)
                out.flush()
                os.fchmod(out.fileno(), 0o644)  # The unprivileged gateway reads this lease.
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        with self.lock:
            self.address, self.expires = address, payload['expires_at']


class Discovery:
    def __init__(self, interface, pc, network, mac, *, ipcmd='ip', hint=''):
        self.interface, self.pc = interface, pc
        self.network, self.mac = ipaddress.IPv4Network(network), mac.lower()
        self.mac_bytes = bytes.fromhex(mac.replace(':', ''))
        self.ipcmd, self.hint = ipcmd, hint
        self.observed = deque(maxlen=8)
        self.lock = threading.Lock()
        self.socket = None
        self.last_probe = -float('inf')

    def allowed(self, address):
        try:
            ip = ipaddress.IPv4Address(address)
            return (ip in self.network and ip not in
                    {self.network.network_address, self.network.broadcast_address,
                     ipaddress.IPv4Address(self.pc)})
        except (ValueError, TypeError):
            return False

    def observe(self, frame):
        """Accept address hints only from this Nest's Ethernet source MAC."""
        if len(frame) < 34 or frame[6:12] != self.mac_bytes:
            return
        if frame[12:14] == b'\x08\x00' and frame[14] >> 4 == 4:
            address = socket.inet_ntoa(frame[26:30])
        elif (len(frame) >= 42 and frame[12:14] == b'\x08\x06'
              and frame[14:20] == b'\x00\x01\x08\x00\x06\x04'
              and frame[22:28] == self.mac_bytes):
            address = socket.inet_ntoa(frame[28:32])
        else:
            return
        if self.allowed(address):
            with self.lock:
                self.observed.appendleft((time.monotonic(), address))

    def candidates(self):
        addresses = []
        with self.lock:
            addresses.extend(address for seen, address in self.observed
                             if time.monotonic() - seen < 30)
        try:
            rows = json.loads(subprocess.check_output(
                [self.ipcmd, '-j', '-4', 'neigh', 'show', 'dev', self.interface], timeout=2))
            rows.sort(key=lambda row: 'REACHABLE' not in row.get('state', []))
            addresses.extend(row['dst'] for row in rows
                             if str(row.get('lladdr', '')).lower() == self.mac
                             and not {'FAILED', 'INCOMPLETE'}.intersection(row.get('state', [])))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        addresses.append(self.hint)
        return list(dict.fromkeys(address for address in addresses if self.allowed(address)))[:8]

    def probe(self):
        """Solicit Cast mDNS replies; the raw receiver learns only our MAC's IP."""
        if time.monotonic() - self.last_probe < 15:
            return
        self.last_probe = time.monotonic()
        try:
            if self.socket is None:
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.socket.bind((self.pc, 0))
                self.socket.setblocking(False)
                interface = socket.inet_aton(self.pc)
                self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, interface)
                self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                       socket.inet_aton('224.0.0.251') + interface)
            for _ in range(32):
                try:
                    self.socket.recvfrom(65535)
                except BlockingIOError:
                    break
            name = b''.join(bytes([len(label)]) + label
                            for label in (b'_googlecast', b'_tcp', b'local')) + b'\0'
            packet = struct.pack('!6H', 0, 0, 1, 0, 0, 0) + name + struct.pack('!HH', 12, 0x8001)
            self.socket.sendto(packet, ('224.0.0.251', 5353))
        except OSError:
            self.close()

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None


def monitor_nest(nest, expected_mac, connected, record, stop, *, discover=None,
                 publish=None, probe=None):
    """Re-verify identity every five seconds, withdrawing stale addresses on failure."""
    previous, address = None, None

    def set_address(value):
        if value is None or value != address:
            connected.clear()
        if publish:
            try:
                publish(value)
            except (OSError, subprocess.SubprocessError) as exc:
                connected.clear()
                record(kind='nest_route_error', error=str(exc))
                return False
        if value is not None:
            connected.set()
        return True

    try:
        while not stop.is_set():
            candidates = list(dict.fromkeys(([address] if address else []) +
                                            (discover() if discover else [nest])))
            verified, mismatch = None, False
            for candidate in candidates:
                if stop.is_set():
                    break
                try:
                    with urllib.request.urlopen(
                            f'http://{candidate}:8008/setup/eureka_info', timeout=3) as response:
                        info = json.load(response)
                    actual = info.get('mac_address', '') if isinstance(info, dict) else ''
                    if isinstance(actual, str) and actual.lower() == expected_mac.lower():
                        verified = candidate
                        break
                    mismatch = True
                except (OSError, ValueError, HTTPException):
                    pass
                # An address that stopped identifying as our Nest must stop routing immediately.
                if candidate == address:
                    set_address(None)
            if stop.is_set():
                break
            state = 'connected' if verified else ('identity_mismatch' if mismatch else 'disconnected')
            if not set_address(verified):
                stop.wait(5)
                continue
            if verified and address and verified != address:
                record(kind='nest_address_changed', previous=address, nest=verified)
            if state != previous or verified != address:
                record(kind=f'nest_{state}', nest=verified or address or nest)
            previous, address = state, verified
            if verified is None and probe:
                probe()
            stop.wait(5)
    finally:
        set_address(None)
