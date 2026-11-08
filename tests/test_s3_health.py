"""A readiness probe must complete TLS/H2 and leave no errors or voice requests."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import s3_server
from app.s3_health import check_endpoint


class S3HealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .sign(key, hashes.SHA256()))
        pem = Path(self.temp.name) / "fixture.pem"
        pem.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())
                        + cert.public_bytes(serialization.Encoding.PEM))
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(pem)
        self.context.set_alpn_protocols(["h2"])

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_probe_finishes_cleanly_without_voice_requests(self):
        server = s3_server.Server()
        listener = await asyncio.start_server(server.handle, "127.0.0.1", 0, ssl=self.context)
        try:
            with patch.object(s3_server, "log") as log, patch.object(s3_server, "_run_query") as query:
                await asyncio.to_thread(check_endpoint, listener.sockets[0].getsockname()[1])
                await asyncio.sleep(.01)
                self.assertEqual([call.args[0] for call in log.call_args_list], ["s3_tls_established"])
                query.assert_not_called()
                self.assertFalse(server.connections)
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_probe_rejects_listener_without_http2(self):
        self.context.set_alpn_protocols(["http/1.1"])

        async def close(reader, writer):
            await reader.read()
            writer.close()
            await writer.wait_closed()

        listener = await asyncio.start_server(close, "127.0.0.1", 0, ssl=self.context)
        try:
            with self.assertRaisesRegex(RuntimeError, "did not negotiate HTTP/2"):
                await asyncio.to_thread(check_endpoint, listener.sockets[0].getsockname()[1])
        finally:
            listener.close()
            await listener.wait_closed()
