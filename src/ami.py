from __future__ import annotations

import socket
import uuid
from collections.abc import Mapping


class AmiError(RuntimeError):
    """Raised when AMI rejects an action or returns a malformed response."""


class AmiClient:
    def __init__(
        self,
        host: str,
        username: str,
        secret: str,
        *,
        port: int = 5038,
        timeout: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.timeout = timeout

    def originate(
        self,
        *,
        channel: str,
        context: str,
        extension: str,
        priority: int = 1,
    ) -> None:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as connection:
            connection.settimeout(self.timeout)
            with connection.makefile("rb") as reader:
                banner = reader.readline().decode("utf-8", errors="replace").strip()
                if not banner.startswith("Asterisk Call Manager/"):
                    raise AmiError(f"Unexpected AMI banner: {banner!r}")

                self._send_action(
                    connection,
                    reader,
                    "Login",
                    {"Username": self.username, "Secret": self.secret, "Events": "off"},
                )
                self._send_action(
                    connection,
                    reader,
                    "Originate",
                    {
                        "Channel": channel,
                        "Exten": extension,
                        "Context": context,
                        "Priority": str(priority),
                        "Async": "true",
                    },
                )
            try:
                self._write_action(connection, "Logoff", {}, str(uuid.uuid4()))
            except OSError:
                pass

    def _send_action(
        self,
        connection: socket.socket,
        reader,
        action: str,
        fields: Mapping[str, str],
    ) -> dict[str, str]:
        action_id = str(uuid.uuid4())
        self._write_action(connection, action, fields, action_id)

        for _ in range(100):
            response = self._read_message(reader)
            if response.get("actionid") != action_id:
                continue
            if response.get("response", "").lower() != "success":
                message = response.get("message", "AMI action was rejected")
                raise AmiError(f"AMI {action} failed: {message}")
            return response
        raise AmiError(f"AMI {action} returned too many unrelated events")

    @staticmethod
    def _write_action(
        connection: socket.socket,
        action: str,
        fields: Mapping[str, str],
        action_id: str,
    ) -> None:
        lines = [f"Action: {action}", f"ActionID: {action_id}"]
        lines.extend(f"{name}: {value}" for name, value in fields.items())
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))

    @staticmethod
    def _read_message(reader) -> dict[str, str]:
        message: dict[str, str] = {}
        while True:
            line = reader.readline()
            if not line:
                raise AmiError("AMI closed the connection before replying")
            decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not decoded:
                if message:
                    return message
                continue
            name, separator, value = decoded.partition(":")
            if not separator:
                raise AmiError(f"Malformed AMI response line: {decoded!r}")
            message[name.strip().lower()] = value.strip()
