"""Temporary, single-Nest DNS redirection. Run in the host terminal with sudo.

Uses only Python's standard library plus nft/ip. It does not change global DNS,
IP forwarding, system certificate stores, router settings, or other devices.
Only the selected Nest's gateway ARP entry is redirected. DNS A responses point
to this PC; AAAA/other types receive empty answers. Ports 80/443, including
cached remote IPv4 destinations, bridge to the unprivileged loopback proxy.
Other redirected IPv4 WAN traffic is dropped.
The selected Nest's IPv6 gateway neighbor is redirected too; routed IPv6
traffic is rejected locally to force IPv4 fallback. LAN multicast is unchanged.
Ctrl-C, a duration limit, or systemd stopping the service removes the nft table and restores the ARP entry.
"""
import argparse
import errno
import ipaddress
import json
import os
import select
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import threading
import time
import urllib.request
from http.client import HTTPException
from pathlib import Path

STOP = threading.Event()
# Errors from a raw socket whose interface went down, vanished, or is briefly
# out of buffers. These are transient: wait for the link instead of exiting.
LINK_ERRORS = frozenset({errno.ENETDOWN, errno.ENXIO, errno.ENODEV, errno.ENOBUFS})


def open_raw(interface):
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    try:
        sock.bind((interface, 0))
        sock.settimeout(0.3)
    except BaseException:
        sock.close()
        raise
    return sock


class RawLink:
    """Raw Ethernet socket that survives the interface going down or being re-created."""

    def __init__(self, interface, record, opener=open_raw):
        self.interface, self.record, self.opener = interface, record, opener
        self.sock = opener(interface)
        self.down = False

    def _failed(self, error):
        if error.errno not in LINK_ERRORS:
            return False
        if not self.down:
            self.down = True
            self.record(kind='link_down', interface=self.interface, error=str(error))
        self.close()  # Rebind later: a re-created interface gets a new ifindex.
        return True

    def _ready(self):
        if self.sock is None:
            try:
                self.sock = self.opener(self.interface)
            except OSError as error:
                if error.errno not in LINK_ERRORS:
                    raise
                return False
        return True

    def send(self, frame):
        """Send a frame; return False instead of raising while the link is down."""
        if not self._ready():
            return False
        try:
            self.sock.send(frame)
        except OSError as error:
            if not self._failed(error):
                raise
            return False
        if self.down:
            self.down = False
            self.record(kind='link_up', interface=self.interface)
        return True

    def recv(self):
        """Return a frame, or None on timeout or while the link is down."""
        if not self._ready():
            return None
        try:
            return self.sock.recv(65535)
        except socket.timeout:
            return None
        except OSError as error:
            if not self._failed(error):
                raise
            return None

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def monitor_nest(nest, expected_mac, connected, record, stop=STOP):
    """Keep retrying an absent Nest; authorize redirection only for its MAC."""
    previous = None
    try:
        while not stop.is_set():
            try:
                with urllib.request.urlopen(
                        f'http://{nest}:8008/setup/eureka_info', timeout=3) as response:
                    info = json.load(response)
                actual_mac = info.get('mac_address', '') if isinstance(info, dict) else ''
                state = ('connected' if isinstance(actual_mac, str) and
                         actual_mac.lower() == expected_mac.lower() else 'identity_mismatch')
            except (OSError, ValueError, HTTPException):
                state = 'disconnected'
            if stop.is_set():
                break
            if state == 'connected':
                connected.set()
            else:
                connected.clear()
            if state != previous:
                record(kind=f'nest_{state}', nest=nest)
                previous = state
            stop.wait(5)
    finally:
        connected.clear()


def checksum(data):
    if len(data) % 2:
        data += b'\0'
    total = sum(struct.unpack('!' + 'H' * (len(data) // 2), data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return (~total) & 65535


def macbytes(text):
    return bytes.fromhex(text.replace(':', ''))


def dns_answer(data, local_ip):
    if len(data) < 17 or data[2] & 128 or struct.unpack('!H', data[4:6])[0] != 1:
        return None
    pos = 12
    labels = []
    while pos < len(data) and data[pos]:
        length = data[pos]
        if length > 63 or pos + length + 1 >= len(data):
            return None
        labels.append(data[pos + 1:pos + length + 1].decode('ascii', errors='replace'))
        pos += length + 1
    end = pos + 5
    if end > len(data):
        return None
    qtype, qclass = struct.unpack('!HH', data[pos + 1:end])
    answer = b''
    if qtype == 1 and qclass == 1:
        answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 5, 4) + socket.inet_aton(local_ip)
    flags = 0x8080 | (struct.unpack('!H', data[2:4])[0] & 0x0100)
    header = data[:2] + struct.pack('!HHHHH', flags, 1, int(bool(answer)), 0, 0)
    return header + data[12:end] + answer, '.'.join(labels), qtype


def udp_frame(src_mac, dst_mac, src_ip, dst_ip, src_port, dst_port, payload):
    udp = struct.pack('!HHHH', src_port, dst_port, 8 + len(payload), 0) + payload
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                     socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    ip = ip[:10] + struct.pack('!H', checksum(ip)) + ip[12:]
    return dst_mac + src_mac + b'\x08\x00' + ip + udp


def arp_reply(sender_mac, sender_ip, recipient_mac, recipient_ip, ethernet_sender=None):
    return (recipient_mac + (ethernet_sender or sender_mac) + b'\x08\x06' +
            struct.pack('!HHBBH', 1, 0x0800, 6, 4, 2) + sender_mac +
            socket.inet_aton(sender_ip) + recipient_mac + socket.inet_aton(recipient_ip))


def ipv6_frame(src_mac, dst_mac, src_ip, dst_ip, payload, hop_limit=255):
    src, dst = socket.inet_pton(socket.AF_INET6, src_ip), socket.inet_pton(socket.AF_INET6, dst_ip)
    pseudo = src + dst + struct.pack('!I3xB', len(payload), 58)
    payload = payload[:2] + struct.pack('!H', checksum(pseudo + payload)) + payload[4:]
    header = struct.pack('!IHBB', 6 << 28, len(payload), 58, hop_limit) + src + dst
    return dst_mac + src_mac + b'\x86\xdd' + header + payload


def neighbor_advertisement(local_mac, recipient_mac, router_ip, recipient_ip, advertised_mac):
    payload = struct.pack('!BBHI', 136, 0, 0, 0xA0000000)
    payload += socket.inet_pton(socket.AF_INET6, router_ip) + b'\x02\x01' + advertised_mac
    return ipv6_frame(local_mac, recipient_mac, router_ip, recipient_ip, payload)


def link_local(mac):
    iid = bytes([mac[0] ^ 2]) + mac[1:3] + b'\xff\xfe' + mac[3:]
    return str(ipaddress.IPv6Address(b'\xfe\x80' + bytes(6) + iid))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', required=True)
    parser.add_argument('--nest', required=True)
    parser.add_argument('--nest-mac', required=True)
    parser.add_argument('--pc', required=True)
    parser.add_argument('--gateway', required=True)
    parser.add_argument('--duration', type=int, default=900, help='seconds; 0 means run until stopped')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run in the host terminal with sudo; raw Ethernet and temporary nft rules require root.')
    for value in (args.nest, args.pc, args.gateway):
        ipaddress.IPv4Address(value)
    if args.duration != 0 and not 30 <= args.duration <= 3600:
        parser.error('Duration must be 0 or 30..3600 seconds')
    folder = Path(__file__).resolve().parent
    stop_file = folder / 'network-helper.stop'
    stop_file.unlink(missing_ok=True)
    log_fd = os.open(folder / 'network-helper-events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if os.environ.get('SUDO_UID'):
        os.fchown(log_fd, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))

    def record(**event):
        event['time'] = time.time()
        line = json.dumps(event)
        print(line, flush=True)
        os.write(log_fd, (line + '\n').encode())
    nft = shutil.which('nft')
    ipcmd = shutil.which('ip')
    if not nft or not ipcmd:
        parser.error('The host needs nft and ip installed')
    local_mac = macbytes((Path('/sys/class/net') / args.interface / 'address').read_text().strip())
    nest_mac = macbytes(args.nest_mac)
    addresses = json.loads(subprocess.check_output([ipcmd, '-j', '-4', 'addr', 'show', 'dev', args.interface]))
    if not any(a['local'] == args.pc for item in addresses for a in item.get('addr_info', [])):
        parser.error('PC address/interface mismatch')
    # Warm up the gateway neighbor entry without any authentication attempt.
    try:
        urllib.request.urlopen(f'http://{args.gateway}/', timeout=3).close()
    except Exception:
        pass
    neighbors = json.loads(subprocess.check_output([ipcmd, '-j', 'neigh', 'show', 'to', args.gateway, 'dev', args.interface]))
    gateway_mac = next((macbytes(n['lladdr']) for n in neighbors if n.get('lladdr')), None)
    if gateway_mac is None:
        parser.error('Gateway MAC is not in the neighbor table')
    routes6 = json.loads(subprocess.check_output([ipcmd, '-j', '-6', 'route', 'show', 'default', 'dev', args.interface]))
    gateway6 = next((route['gateway'] for route in routes6 if route.get('gateway')), None)
    gateway6_mac = None
    if gateway6:
        peers6 = json.loads(subprocess.check_output([ipcmd, '-j', '-6', 'neigh', 'show', 'to', gateway6, 'dev', args.interface]))
        gateway6_mac = next((macbytes(n['lladdr']) for n in peers6 if n.get('lladdr')), None)
        if gateway6_mac is None:
            parser.error('IPv6 gateway MAC is unknown; refusing to leave an unhandled IPv6 route')
    nest6 = link_local(nest_mac)
    for port in (18445, 18081):
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            pass
    table = f'nest_lab_{os.getpid()}'
    try:
        link = RawLink(args.interface, record)
    except PermissionError:
        parser.error('Raw networking is denied even as root. This is commonly a rootless container; run the helper in the host OS terminal, outside the container. No ARP or nft changes have been made.')
    connected = threading.Event()
    monitor = threading.Thread(target=monitor_nest,
                               args=(args.nest, args.nest_mac, connected, record), daemon=True)

    class Bridge(socketserver.BaseRequestHandler):
        def handle(self):
            if self.client_address[0] != args.nest or not connected.is_set():
                return
            destination = 18445 if self.server.server_address[1] == 443 else 18081
            try:
                with socket.create_connection(('127.0.0.1', destination), timeout=5) as upstream:
                    self.request.settimeout(5)
                    while not STOP.is_set() and connected.is_set():
                        ready, _, _ = select.select([self.request, upstream], [], [], 0.5)
                        for current in ready:
                            data = current.recv(65536)
                            if not data:
                                return
                            (upstream if current is self.request else self.request).sendall(data)
            except (OSError, ConnectionError):
                return

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    servers = []
    applied = False
    try:
        for port in (443, 80):
            server = Server((args.pc, port), Bridge)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        # Prevent the kernel forwarding/rejecting the gateway-addressed packets.
        # Raw DNS replies are generated below, before this hook. Local PC traffic
        # remains local. No global firewall chains are edited or flushed.
        rule = f'''table ip {table} {{
 chain ingress_nat {{ type nat hook prerouting priority dstnat; policy accept;
  iifname "{args.interface}" ether daddr {local_mac.hex(':')} ether saddr {args.nest_mac} ip saddr {args.nest} tcp dport {{ 80, 443 }} dnat to {args.pc}
 }}
 chain gate {{ type filter hook prerouting priority -90; policy accept;
  iifname "{args.interface}" ether daddr {local_mac.hex(':')} ip saddr {args.nest} ip daddr != {args.pc} udp dport {{ 443, 18445 }} reject with icmp type port-unreachable
  iifname "{args.interface}" ether daddr {local_mac.hex(':')} ip saddr {args.nest} ip daddr != {args.pc} drop
 }}
}}\n'''
        if gateway6:
            rule += f'''table ip6 {table} {{
 chain gate {{ type filter hook prerouting priority -90; policy accept;
  iifname "{args.interface}" ether daddr {local_mac.hex(':')} ether saddr {args.nest_mac} drop
 }}
}}\n'''
        subprocess.run([nft, '-f', '-'], input=rule, text=True, check=True)
        applied = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: STOP.set())
        monitor.start()
        limit = 'until stopped' if args.duration == 0 else f'auto-stop after {args.duration}s'
        print(f'READY: waiting for {args.nest} / {args.nest_mac}; {limit}. Ctrl-C restores networking.', flush=True)
        if gateway6:
            print(f'IPv6 gateway {gateway6} is redirected for this Nest only; IPv6 WAN is rejected locally.', flush=True)
            record(kind='ipv6_redirect_active', nest=nest6, gateway=gateway6)
        record(kind='active', nest=args.nest, pid=os.getpid(), table=table)
        deadline = None if args.duration == 0 else time.monotonic() + args.duration
        next_arp = 0
        last_ipv6_reject = 0
        was_connected = False
        while not STOP.is_set() and (deadline is None or time.monotonic() < deadline):
            if stop_file.exists():
                break
            if not connected.is_set():
                was_connected = False
                STOP.wait(0.2)
                continue
            if not was_connected:
                next_arp = 0  # Reapply the gateway announcement immediately on reconnect.
                was_connected = True
            now = time.monotonic()
            if now >= next_arp:
                # The first successful send after an outage reapplies the redirect.
                if link.send(arp_reply(local_mac, args.gateway, nest_mac, args.nest)) and gateway6:
                    link.send(neighbor_advertisement(local_mac, nest_mac, gateway6, nest6, local_mac))
                next_arp = now + 1
            frame = link.recv()
            if frame is None:
                if link.down:
                    STOP.wait(1)
                continue
            if (len(frame) >= 54 and frame[:6] == local_mac and frame[6:12] == nest_mac
                    and frame[12:14] == b'\x86\xdd'):
                packet6 = frame[14:]
                observed = time.monotonic()
                # Do not answer ICMPv6 errors or neighbor discovery with errors.
                if packet6[6] != 58 and observed - last_ipv6_reject >= 0.1 and gateway6:
                    source6 = socket.inet_ntop(socket.AF_INET6, packet6[8:24])
                    destination6 = socket.inet_ntop(socket.AF_INET6, packet6[24:40])
                    error = struct.pack('!BBHI', 1, 1, 0, 0) + packet6[:1232]
                    link.send(ipv6_frame(local_mac, nest_mac, gateway6, source6, error, 64))
                    record(kind='ipv6_rejected', source=source6, dst=destination6, protocol=packet6[6])
                    last_ipv6_reject = observed
                continue
            if len(frame) < 42 or frame[:6] != local_mac or frame[6:12] != nest_mac or frame[12:14] != b'\x08\x00':
                continue
            ip = frame[14:]
            if socket.inet_ntoa(ip[12:16]) != args.nest:
                continue
            ihl = (ip[0] & 15) * 4
            if len(ip) < ihl + 8:
                continue
            if ip[9] == 6:
                tcp = ip[ihl:]
                if len(tcp) >= 20 and tcp[13] & 0x02:
                    dst_port = struct.unpack('!H', tcp[2:4])[0]
                    if dst_port in (80, 443):
                        record(kind='tcp_syn', dst=socket.inet_ntoa(ip[16:20]), port=dst_port)
                continue
            if ip[9] != 17:
                continue
            udp = ip[ihl:]
            src_port, dst_port, size, _ = struct.unpack('!HHHH', udp[:8])
            if dst_port in (443, 18445):
                record(kind=f'udp{dst_port}', dst=socket.inet_ntoa(ip[16:20]))
                continue
            if dst_port != 53:
                continue
            result = dns_answer(udp[8:size], args.pc)
            if result:
                answer, name, qtype = result
                link.send(udp_frame(local_mac, nest_mac, socket.inet_ntoa(ip[16:20]), args.nest, 53, src_port, answer))
                record(kind='dns', name=name, type=qtype, answer=args.pc if qtype == 1 else 'empty')
    finally:
        STOP.set()
        if monitor.is_alive():
            monitor.join(timeout=4)
        if applied:
            subprocess.run([nft, 'delete', 'table', 'ip', table], check=False)
            if gateway6:
                subprocess.run([nft, 'delete', 'table', 'ip6', table], check=False)
            for _ in range(5):
                # Keep the Ethernet source as this PC to avoid teaching the LAN
                # switch that the router's own MAC moved to this PC's port.
                link.send(arp_reply(gateway_mac, args.gateway, nest_mac, args.nest, local_mac))
                if gateway6:
                    link.send(neighbor_advertisement(local_mac, nest_mac, gateway6, nest6, gateway6_mac))
                time.sleep(0.1)
        for server in servers:
            server.shutdown()
            server.server_close()
        link.close()
        print('Stopped; temporary nft table removed and Nest gateway ARP restored.', flush=True)
        record(kind='stopped', table=table)
        os.close(log_fd)


if __name__ == '__main__':
    main()
