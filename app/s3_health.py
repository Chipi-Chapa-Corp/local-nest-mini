"""Check the local S3 listener using TLS and HTTP/2 without creating a voice turn."""

import socket
import ssl
import sys

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import RemoteSettingsChanged


def check_endpoint(port=18444, timeout=1):
    # This loopback-only listener intentionally uses the owned-device lab chain.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["h2"])
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname="assistant-s3-pa.googleapis.com") as tls:
            if tls.selected_alpn_protocol() != "h2":
                raise RuntimeError("Local S3 listener did not negotiate HTTP/2")
            client = H2Connection(H2Configuration(client_side=True))
            client.initiate_connection()
            tls.sendall(client.data_to_send())
            while True:
                data = tls.recv(65536)
                if not data:
                    raise RuntimeError("Local S3 listener closed before HTTP/2 settings")
                if any(isinstance(event, RemoteSettingsChanged) for event in client.receive_data(data)):
                    break
            client.close_connection()
            tls.sendall(client.data_to_send())
            # Drain settings acknowledgements and the peer's TLS close-notify.
            while tls.recv(65536):
                pass
            tls.unwrap().close()


if __name__ == "__main__":
    try:
        check_endpoint()
    except Exception as exc:
        print(f"Local S3 readiness check failed: {exc}", file=sys.stderr)
        sys.exit(1)
