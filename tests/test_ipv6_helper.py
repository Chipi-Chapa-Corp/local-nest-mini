import socket
import struct
import unittest

from nest_network_helper import checksum, link_local, macbytes, neighbor_advertisement


class IPv6HelperTest(unittest.TestCase):
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
