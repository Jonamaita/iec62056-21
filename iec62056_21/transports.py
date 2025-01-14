import time
import logging
from typing import Tuple, Union, Optional

import serial
import socket
from iec62056_21 import utils, exceptions, constants

logger = logging.getLogger(__name__)


class TransportError(Exception):
    """General transport error"""


class BaseTransport:
    """
    Base transport class for IEC 62056-21 communication.
    """

    TRANSPORT_REQUIRES_ADDRESS: bool = True

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def connect(self) -> None:
        raise NotImplemented("Must be defined in subclass")

    def disconnect(self) -> None:
        raise NotImplemented("Must be defined in subclass")

    def read(self, timeout: Optional[int] = None) -> bytes:
        """
        Will read a normal readout. Supports both full and partial block readout.
        When using partial blocks it will recreate the messages as it was not sent with
        partial blocks

        :param timeout:
        :return:
        """
        start_chars = [b"\x01", b"\x02"]
        end_chars = [b"\x03", b"\x04"]
        total_data = b""
        packets = 0
        start_char_received = False
        start_char = None
        end_char = None
        timeout = timeout or self.timeout

        while True:

            in_data = b""
            duration: float = 0.0
            start_time = time.time()
            while True:
                b = self.recv(1)

                duration = time.time() - start_time
                if duration > self.timeout:
                    raise TimeoutError(f"Read in {self.__class__.__name__} timed out")
                if not start_char_received:
                    # is start char?
                    if b in start_chars:
                        in_data += b
                        start_char_received = True
                        start_char = b
                        continue
                    else:
                        continue
                else:
                    # is end char?
                    if b in end_chars:
                        in_data += b
                        end_char = b
                        break
                    else:
                        in_data += b
                        continue

            packets += 1

            bcc = self.recv(1)
            in_data += bcc
            logger.debug(
                f"Received {in_data!r} over transport: {self.__class__.__name__}"
            )

            if start_char == b"\x01":
                # This is a command message, probably Password challange.
                total_data += in_data
                break

            if end_char == b"\x04":  # EOT (partial read)
                # we received a partial block
                if not utils.bcc_valid(in_data):
                    # Nack and read again
                    self.send(constants.NACK.encode(constants.ENCODING))
                    continue
                else:
                    # ack and read next
                    self.send(constants.ACK.encode(constants.ENCODING))
                    # remove bcc and eot and add line end.
                    in_data = in_data[:-2] + constants.LINE_END.encode(
                        constants.ENCODING
                    )
                    if packets > 1:
                        # remove the leading STX
                        in_data = in_data[1:]

                    total_data += in_data
                    continue

            if end_char == b"\x03":
                # Either it was the only message or we got the last message.
                if not utils.bcc_valid(in_data):
                    # Nack and read again
                    self.send(constants.NACK.encode(constants.ENCODING))
                    continue
                else:
                    if packets > 1:
                        in_data = in_data[1:]  # removing the leading STX
                    total_data += in_data
                    if packets > 1:
                        # The last bcc is not correct compared to the whole
                        # message. But we have verified all the bccs along the way so
                        # we just compute it so the message is usable.
                        total_data = utils.add_bcc(total_data[:-1])

                    break

        return total_data

    def simple_read(
        self,
        start_char: Union[str, bytes],
        end_char: Union[str, bytes],
        timeout: Optional[int] = None,
    ) -> bytes:
        """
        A more flexible read for use with some messages.
        """
        _start_char = utils.ensure_bytes(start_char)
        _end_char = utils.ensure_bytes(end_char)

        in_data = b""
        start_char_received = False
        timeout = timeout or self.timeout
        duration: float = 0.0
        start_time = time.time()
        while True:
            b = self.recv(1)
            duration = time.time() - start_time
            if duration > self.timeout:
                raise TimeoutError(f"Read in {self.__class__.__name__} timed out")
            if not start_char_received:
                # is start char?
                if b == _start_char:
                    in_data += b
                    start_char_received = True
                    continue
                else:
                    continue
            else:
                # is end char?
                if b == _end_char:
                    in_data += b
                    break
                else:
                    in_data += b
                    continue

        logger.debug(f"Received {in_data!r} over transport: {self.__class__.__name__}")
        return in_data

    def send(self, data: bytes) -> None:
        """
        Will send data over the transport

        :param data:
        """
        self._send(data)
        logger.debug(f"Sent {data!r} over transport: {self.__class__.__name__}")

    def _send(self, data: bytes) -> None:
        """
        Transport dependant sending functionality.

        :param data:
        """
        raise NotImplemented("Must be defined in subclass")

    def recv(self, chars: int) -> bytes:
        """
        Will receive data over the transport.

        :param chars:
        """
        return self._recv(chars)

    def _recv(self, chars: int) -> bytes:
        """
        Transport dependant sending functionality.

        :param chars:
        """
        raise NotImplemented("Must be defined in subclass")

    def switch_baudrate(self, baud: int) -> None:
        """
        The protocol defines a baudrate switchover process. Though it might not be used
        in all available transports.

        :param baud:
        """
        raise NotImplemented("Must be defined in subclass")


class SerialTransport(BaseTransport):
    """
    Transport class for communication over serial interface.
    Mostly used with Optical probes or USB converters.

    """

    TRANSPORT_REQUIRES_ADDRESS = False

    def __init__(self, port: str, timeout: int = 10):

        super().__init__(timeout=timeout)
        self.port_name: str = port
        self.port: Optional[serial.Serial] = None

    def connect(self, baudrate: int = 300) -> None:
        """
        Creates a serial port.
        """
        self.port = serial.Serial(
            self.port_name,
            baudrate=baudrate,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.SEVENBITS,
            writeTimeout=0,
            timeout=self.timeout / 2,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )

    def disconnect(self) -> None:
        """
        Closes and removes the serial port.
        """
        if self.port is None:
            raise TransportError("Serial port is closed.")

        self.port.close()
        self.port = None

    def _send(self, data: bytes) -> None:
        """
        Sends data over the serial port.

        :param data:
        """
        if self.port is None:
            raise TransportError("Serial port is closed.")

        self.port.write(data)
        self.port.flush()

    def _recv(self, chars: int = 1) -> bytes:
        """
        Receives data over the serial port.

        :param chars:
        """
        if self.port is None:
            raise TransportError("Serial port is closed.")

        return self.port.read(chars)

    def switch_baudrate(self, baud: int) -> None:
        """
        Creates a new serial port with the correct baudrate.

        :param baud:
        """
        if self.port is None:
            raise TransportError("Serial port is closed.")

        logger.info(f"Switching baudrate to: {baud}")
        self.port = self.port = serial.Serial(
            self.port_name,
            baudrate=baud,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.SEVENBITS,
            writeTimeout=0,
            timeout=self.timeout,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
            f"port={self.port_name!r}, "
            f"timeout={self.timeout!r})"
        )


class TcpTransport(BaseTransport):

    """
    Transport class for TCP/IP communication.
    """

    def __init__(self, address: Tuple[str, int], timeout: int = 30):

        super().__init__(timeout=timeout)
        self.address = address
        self.socket: Optional[socket.socket] = self._get_socket()

    def connect(self) -> None:
        """
        Connects the socket to the device network interface.
        """

        if not self.socket:
            self.socket = self._get_socket()
        logger.debug(f"Connecting to {self.address}")
        self.socket.connect(self.address)

    def disconnect(self) -> None:
        """
        Closes and removes the socket.
        """
        if self.socket is None:
            raise TransportError("Socket is closed")

        self.socket.close()
        self.socket = None

    def _send(self, data: bytes) -> None:
        """
        Sends data over the socket.

        :param data:
        """
        if self.socket is None:
            raise TransportError("Socket is closed")

        self.socket.sendall(data)

    def _recv(self, chars: int = 1) -> bytes:
        """
        Receives data from the socket.

        :param chars:
        """
        if self.socket is None:
            raise TransportError("Socket is closed")

        try:
            b = self.socket.recv(chars)
        except (OSError, IOError, socket.timeout, socket.error) as e:
            raise TransportError from e
        return b

    def switch_baudrate(self, baud: int) -> None:
        """
        Baudrate has not meaning in TCP/IP so we just dont do anything.

        :param baud:
        """
        pass

    def _get_socket(self) -> socket.socket:
        """
        Create a correct socket.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        return s

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
            f"address={self.address!r}, "
            f"timeout={self.timeout!r}"
        )
