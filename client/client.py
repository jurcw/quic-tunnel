"""
QUIC Client which consumes any UDP traffic and forwards it over QUIC to remote QUIC Server
Designed originally for application protocol with its own re-try mechanism - CoAP (LwM2M).
QUIC tunnel uses unreliable QUIC Datagram (https://www.rfc-editor.org/rfc/rfc9221.html)
"""

import argparse
import asyncio
import logging
import ssl
import struct
from typing import cast

from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import ConnectionTerminated, DatagramFrameReceived, QuicEvent


# declaring globals to store info about UDP and QUIC connections
# It probably be shouldn't done like this, but I don't have better idea, needs to be fixed
quic_connection: list = [None]
udp_connection_transport: list = [None]
udp_connection_address: list = [None]
allowed_alpns = ["http/0.9", "http/1.0", "http/1.1", "spdy/1", "spdy/2", "spdy/3", 
"stun.turn", "stun.nat-discovery", "h2", "h2c", "webrtc", "c-webrtc", "ftp", "imap", 
"pop3", "managesieve", "coap", "xmpp-client", "xmpp-server", "acme-tls/1", 
"mqtt", "dot", "ntske/1", "sunrpc", "h3", "smb", "irc", "nntp", "nnsp", "doq", "sip/2", "tds/8.0"]

class QuicClientProtocol(QuicConnectionProtocol):
    """
    Main Class to handle forwarding UDP over QUIC
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def quic_forwarder(self) -> None:
        """
        Entry point
        """
        quic_connection[0] = self
        await asyncio.Future()

    def quic_event_received(self, event: QuicEvent) -> None:
        logger = logging.getLogger("[QUIC Client]")

        if isinstance(event, DatagramFrameReceived):
            # parse data from server
            length = struct.unpack("!H", bytes(event.data[:2]))[0]
            data_from_server = event.data[2 : 2 + length]
            logger.info("Received data from QUIC server \n%s ...", {data_from_server[:15]})
            if udp_connection_transport[0] is not None:
                logger.info(
                    "Trying to forward QUIC data via UDP towards the UDP client"
                )
                udp_connection_transport[0].sendto(
                    data_from_server, udp_connection_address[0]
                )
                logger.debug("Forwarding done")

        if isinstance(event, ConnectionTerminated):
            logger.info("QUIC Connection termination reason: %s", {event.reason_phrase})


class UdpServerProtocol(asyncio.DatagramProtocol):
    """
    Main Class to handle receiving UDP traffic and forward it via QUIC
    """
    def __init__(self):
        super().__init__()
        self.transport = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport
        udp_connection_transport[0] = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        udp_connection_address[0] = addr
        logger = logging.getLogger("[UDP receiver]")
        logger.info("Received UDP message \n%s", {data[:15]})
        if quic_connection[0] is not None:
            try:
                # try to parse the payload as str
                str_data = data.decode().strip()
                logger.debug("Looks like input data is string: %s", {str_data})
            except UnicodeDecodeError:
                logger.debug("Looks like input data are bytes already: %s", {data})
            data = struct.pack("!H", len(data)) + data
            logger.debug("Attempting to forward over existing QUIC connection: %s", {data})
            QuicConnection.send_datagram_frame(quic_connection[0]._quic, data) # pylint: disable=protected-access
            QuicConnectionProtocol.transmit(quic_connection[0])
            logger.info("Forwarding via QUIC done")
        else:
            logger.error(
                "Unable to forward data, because QUIC connection is not established"
            )


async def udp_listener(local_udp_port: int) -> None:
    """
    Start UDP Server
    """
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        UdpServerProtocol, local_addr=("127.0.0.1", local_udp_port)
    )


async def main(
    config: QuicConfiguration,
    remote_quic_host: str,
    remote_quic_port: int,
    local_udp_port: int,
) -> None:
    """
    Main function
    """
    async with connect(
        configuration=config,
        host=remote_quic_host,
        port=remote_quic_port,
        create_protocol=QuicClientProtocol,
    ) as client:

        client = cast(QuicClientProtocol, client)
        # start UDP listener
        await udp_listener(local_udp_port)
        # start QUIC Client and UDP->QUIC forwarder
        await client.quic_forwarder()


def parse_args() -> argparse.Namespace:
    """
    Parse the startup arguments.
    """
    parser = argparse.ArgumentParser(description="UDP over QUIC tunneling client")
    parser.add_argument(
        "--remoteQuicHost",
        type=str,
        default="localhost",
        help="The remote peer's remoteQuicHost name or IP address",
    )
    parser.add_argument(
        "--remoteQuicPort",
        type=int,
        default=4784,
        help="The remote peer's remoteQuicPort number",
    )
    parser.add_argument(
        "--localUdpPort",
        type=int,
        default=5683,
        help="The local UDP port on which application should listen on",
    )
    parser.add_argument(
        "--alpnProtocol",
        type=str,
        default="coap",
        help="ALPN protocol introduced during handshake",
    )
    parser.add_argument(
        "--ca-certs", type=str, help="load CA certificates from the specified file"
    )
    parser.add_argument(
        "-l",
        "--secrets-log",
        type=str,
        help="log secrets to a file, for use with Wireshark",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="increase logging verbosity"
    )
    return parser.parse_args()

if __name__ == "__main__":
    startArgs = parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.DEBUG if startArgs.verbose else logging.INFO,
    )

    if startArgs.alpnProtocol in allowed_alpns:
        selected_alpn = [startArgs.alpnProtocol]
    else:
        logging.warning("Selected ALPN %s not supported, fallback to default 'coap'", startArgs.alpnProtocol)   
        selected_alpn = ["coap"]

    quic_configuration = QuicConfiguration(
        alpn_protocols = selected_alpn,
        is_client = True,
        max_datagram_frame_size = 65535,
        verify_mode = ssl.CERT_REQUIRED,
        idle_timeout = 86400,
    )

    if startArgs.ca_certs:
        quic_configuration.load_verify_locations(startArgs.ca_certs)
    if startArgs.secrets_log:
        quic_configuration.secrets_log_file = open(startArgs.secrets_log, "a", encoding="UTF-8")

    try:
        # Starting main program
        asyncio.run(
            main(
                config=quic_configuration,
                remote_quic_host=startArgs.remoteQuicHost,
                remote_quic_port=startArgs.remoteQuicPort,
                local_udp_port=startArgs.localUdpPort,
            )
        )
    except KeyboardInterrupt:
        print("Shutting down")
