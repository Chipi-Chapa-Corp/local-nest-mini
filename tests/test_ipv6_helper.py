import io
import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from nest_network_helper import checksum, link_local, macbytes, monitor_nest, neighbor_advertisement


class IPv6HelperTest(unittest.TestCase):
    def test_offline_start_disconnect_and_reconnect_keep_retrying(self):
        connected, stop = threading.Event(), threading.Event()
        events, states = [], []
        mac = "02:00:00:00:00:01"

        def device(value):
            return io.BytesIO(json.dumps({"mac_address": value}).encode())

        def next_check(seconds):
            states.append(connected.is_set())
            if len(states) == 5:
                stop.set()

        with patch("nest_network_helper.urllib.request.urlopen", side_effect=[
                OSError("No route to host"), device(mac), TimeoutError(), device(mac),
                device("02:00:00:00:00:02")]), patch.object(stop, "wait", side_effect=next_check):
            monitor_nest("192.0.2.2", mac, connected,
                         lambda **event: events.append(event["kind"]), stop)
        self.assertEqual(states, [False, True, False, True, False])
        self.assertEqual(events, ["nest_disconnected", "nest_connected", "nest_disconnected",
                                  "nest_connected", "nest_identity_mismatch"])
        self.assertFalse(connected.is_set())

    def test_targeted_neighbor_update_has_valid_ipv6_checksum(self):
        pc = macbytes("bc:fc:e7:05:4e:61")
        nest = macbytes("02:00:00:00:00:01")
        router = "fe80::1"
        address = link_local(nest)
        self.assertEqual(address, "fe80::ff:fe00:1")
        packet = neighbor_advertisement(pc, nest, router, address, pc)
        self.assertEqual(packet[:6], nest)  # unicast to the owned device
        self.assertEqual(packet[6:12], pc)  # never teach the switch a moved router MAC
        header, payload = packet[14:54], packet[54:]
        self.assertEqual(header[7], 255)
        self.assertEqual(header[8:24], socket.inet_pton(socket.AF_INET6, router))
        self.assertEqual(header[24:40], socket.inet_pton(socket.AF_INET6, address))
        self.assertEqual(payload[0], 136)
        pseudo = header[8:40] + struct.pack('!I3xB', len(payload), 58)
        self.assertEqual(checksum(pseudo + payload), 0)
        self.assertEqual(payload[-8:], b'\x02\x01' + pc)

    def test_restore_advertises_real_router_mac_to_only_nest(self):
        pc, nest, router = (macbytes(x) for x in
                            ("bc:fc:e7:05:4e:61", "02:00:00:00:00:01", "70:df:f7:d2:88:d6"))
        packet = neighbor_advertisement(pc, nest, "fe80::1", link_local(nest), router)
        self.assertEqual(packet[:12], nest + pc)
        self.assertEqual(packet[-6:], router)


if __name__ == "__main__":
    unittest.main()
