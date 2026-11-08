"""Follow a verified Nest across DHCP changes without targeting other devices."""

import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.nest_discovery import Discovery, VerifiedNest, monitor_nest
from nest_network_helper import arp_reply, macbytes

MAC = '20:1f:3b:97:cb:30'
OTHER = '02:00:00:00:00:01'


def device(mac):
    return io.BytesIO(json.dumps({'mac_address': mac}).encode())


class DiscoveryTests(unittest.TestCase):
    def test_neighbors_are_matched_by_mac_and_interface_and_lan(self):
        discovery = Discovery('eno1', '192.0.2.10', '192.0.2.0/24', MAC)
        rows = [{'dst': '192.0.2.100', 'lladdr': MAC.upper(), 'state': ['REACHABLE']},
                {'dst': '192.0.2.101', 'lladdr': OTHER, 'state': ['REACHABLE']},
                {'dst': '198.51.100.2', 'lladdr': MAC, 'state': ['STALE']},
                {'dst': '192.0.2.102', 'lladdr': MAC, 'state': ['FAILED']}]
        with patch('app.nest_discovery.subprocess.check_output', return_value=json.dumps(rows)) as command:
            self.assertEqual(discovery.candidates(), ['192.0.2.100'])
        self.assertEqual(command.call_args.args[0][-2:], ['dev', 'eno1'])

    def test_boot_arp_and_ipv4_observations_supply_new_candidates(self):
        discovery = Discovery('eno1', '192.0.2.10', '192.0.2.0/24', MAC)
        own, other = macbytes(MAC), macbytes(OTHER)
        frame = arp_reply(own, '192.0.2.101', b'\xff' * 6, '192.0.2.101')
        discovery.observe(frame)
        discovery.observe(arp_reply(other, '192.0.2.102', b'\xff' * 6, '192.0.2.102'))
        ip = bytearray(20)
        ip[0] = 0x45
        ip[12:16] = socket.inet_aton('192.0.2.103')
        discovery.observe(b'\xff' * 6 + own + b'\x08\x00' + ip)
        with patch('app.nest_discovery.subprocess.check_output', return_value='[]'):
            self.assertEqual(discovery.candidates(), ['192.0.2.103', '192.0.2.101'])

    def test_missing_device_probe_is_bounded_cast_multicast_discovery(self):
        discovery = Discovery('eno1', '192.0.2.10', '192.0.2.0/24', MAC)
        sock = Mock()
        sock.recvfrom.side_effect = BlockingIOError()
        with patch('app.nest_discovery.socket.socket', return_value=sock), \
             patch('app.nest_discovery.time.monotonic', return_value=100):
            discovery.probe()
            discovery.probe()
        sock.bind.assert_called_once_with(('192.0.2.10', 0))
        sock.sendto.assert_called_once()
        packet, destination = sock.sendto.call_args.args
        self.assertEqual(destination, ('224.0.0.251', 5353))
        self.assertIn(b'_googlecast', packet)
        self.assertTrue(packet.endswith(b'\x00\x0c\x80\x01'))
        discovery.close()
        sock.close.assert_called_once()

    def test_dhcp_change_and_ip_reuse_withdraw_the_old_address(self):
        connected, stop = threading.Event(), threading.Event()
        published, states, events = [], [], []
        discoveries = iter([['192.0.2.100'], ['192.0.2.101'], ['192.0.2.101'], ['192.0.2.100']])

        def wait(seconds):
            states.append((connected.is_set(), published[-1]))
            if len(states) == 4:
                stop.set()

        with patch('app.nest_discovery.urllib.request.urlopen', side_effect=[
                device(MAC), device(OTHER), device(MAC), TimeoutError(),
                device(OTHER), device(OTHER)]), patch.object(stop, 'wait', side_effect=wait):
            monitor_nest('', MAC, connected, lambda **row: events.append(row), stop,
                         discover=lambda: next(discoveries), publish=published.append)
        self.assertEqual(states, [(True, '192.0.2.100'), (True, '192.0.2.101'),
                                  (False, None), (False, None)])
        self.assertIn(None, published[1:3])  # Old IP was withdrawn before accepting the new one.
        self.assertEqual(published[-1], None)  # Shutdown invalidates the lease too.
        self.assertEqual([r for r in events if r['kind'] == 'nest_address_changed'],
                         [{'kind': 'nest_address_changed', 'previous': '192.0.2.100', 'nest': '192.0.2.101'}])

    def test_missing_device_solicits_discovery_but_does_not_authorize_a_hint(self):
        stop, connected = threading.Event(), threading.Event()
        probe, published = [], []
        with patch('app.nest_discovery.urllib.request.urlopen', side_effect=OSError()), \
             patch.object(stop, 'wait', side_effect=lambda seconds: stop.set()):
            monitor_nest('192.0.2.100', MAC, connected, lambda **row: None, stop,
                         discover=lambda: ['192.0.2.100'], publish=published.append,
                         probe=lambda: probe.append(True))
        self.assertEqual(probe, [True])
        self.assertTrue(all(value is None for value in published))

    def test_verified_lease_expires_and_withdrawal_is_published(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'lease.json'
            now = [100.0]
            target = VerifiedNest(MAC, path, clock=lambda: now[0])
            target.publish('192.0.2.100')
            self.assertEqual(target.current(), '192.0.2.100')
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            now[0] = 121.0
            self.assertIsNone(target.current())
            target.publish(None)
            self.assertIsNone(json.loads(path.read_text())['address'])

    def test_temporary_route_update_failure_retries_without_authorizing_the_ip(self):
        stop, connected = threading.Event(), threading.Event()
        states, errors = [], []
        publish = Mock(side_effect=[OSError('temporary nft failure'), None, None])

        def wait(seconds):
            states.append(connected.is_set())
            if len(states) == 2:
                stop.set()

        with patch('app.nest_discovery.urllib.request.urlopen', side_effect=lambda *a, **kw: device(MAC)), \
             patch.object(stop, 'wait', side_effect=wait):
            monitor_nest('192.0.2.100', MAC, connected, lambda **row: errors.append(row), stop,
                         publish=publish)
        self.assertEqual(states, [False, True])
        self.assertEqual(publish.call_count, 3)
        self.assertTrue(any(row['kind'] == 'nest_route_error' for row in errors))
