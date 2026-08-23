import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from ami import AmiClient, AmiError


def read_action(connection: socket.socket) -> dict[str, str]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(1024)
        if not chunk:
            raise ConnectionError("client disconnected before completing an AMI action")
        data.extend(chunk)
    fields = {}
    for line in bytes(data).split(b"\r\n"):
        if b":" in line:
            name, value = line.decode().split(":", 1)
            fields[name.lower()] = value.strip()
    return fields


class FakeAmiServer:
    def __init__(self, reject_action: str | None = None):
        self.reject_action = reject_action
        self.actions: list[dict[str, str]] = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.thread.join(2)
        self.listener.close()
        if self.error is not None:
            raise self.error

    def run(self) -> None:
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.sendall(b"Asterisk Call Manager/7.0\r\n")
                for _ in range(2):
                    action = read_action(connection)
                    self.actions.append(action)
                    if action["action"].lower() == self.reject_action:
                        response = "Error"
                        message = "rejected for test"
                    else:
                        response = "Success"
                        message = "accepted"

                    # An unrelated event verifies that the client waits for its
                    # matching ActionID instead of treating the next packet as
                    # the response.
                    connection.sendall(b"Event: TestEvent\r\n\r\n")
                    payload = (
                        f"Response: {response}\r\n"
                        f"ActionID: {action['actionid']}\r\n"
                        f"Message: {message}\r\n\r\n"
                    ).encode()
                    connection.sendall(payload[:9])
                    connection.sendall(payload[9:])
                    if response == "Error":
                        return
                self.actions.append(read_action(connection))
        except BaseException as error:
            self.error = error


class AmiClientTests(unittest.TestCase):
    def test_validates_login_and_originate_responses(self):
        with FakeAmiServer() as server:
            client = AmiClient("127.0.0.1", "discord", "secret", port=server.port)
            client.originate(
                channel="Local/discord@default",
                context="default",
                extension="160",
            )

        self.assertEqual([action["action"] for action in server.actions], ["Login", "Originate", "Logoff"])
        self.assertEqual(server.actions[1]["channel"], "Local/discord@default")
        self.assertEqual(server.actions[1]["exten"], "160")

    def test_rejected_login_raises(self):
        with FakeAmiServer(reject_action="login") as server:
            client = AmiClient("127.0.0.1", "discord", "wrong", port=server.port)
            with self.assertRaisesRegex(AmiError, "AMI Login failed: rejected for test"):
                client.originate(
                    channel="Local/discord@default",
                    context="default",
                    extension="160",
                )

        self.assertEqual(len(server.actions), 1)


if __name__ == "__main__":
    unittest.main()
