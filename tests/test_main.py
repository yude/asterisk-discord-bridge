import sys
import socket
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import main
from audio_bridge import AsteriskPcmBuffer, AudioSocketWriter, recv_exact


class AudioSocketServerTests(unittest.TestCase):
    def test_new_connection_replaces_an_idle_connection(self):
        pcm_buffer = AsteriskPcmBuffer()
        writer = AudioSocketWriter()
        connected = []
        disconnected = []
        server = main.AudioSocketServer(
            "127.0.0.1",
            0,
            pcm_buffer,
            writer,
            lambda: connected.append(True),
            lambda: disconnected.append(True),
        )
        first = second = None
        try:
            server.start()
            server.wait_until_ready()
            first = socket.create_connection(("127.0.0.1", server.port))
            self._wait_until(lambda: len(connected) == 1)

            second = socket.create_connection(("127.0.0.1", server.port))
            self._wait_until(lambda: len(connected) == 2)

            first.settimeout(1)
            self.assertEqual(first.recv(1), b"")
            self.assertTrue(writer.send_audio(b"\x02\x00" * 160))
            self.assertEqual(recv_exact(second, 3), b"\x10\x01\x40")
            self.assertEqual(recv_exact(second, 320), b"\x02\x00" * 160)
            self.assertEqual(disconnected, [])
        finally:
            if first is not None:
                first.close()
            if second is not None:
                second.close()
            server.close()
            writer.close()

    def _wait_until(self, condition, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("condition was not met before timeout")
            time.sleep(0.01)


class BridgeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_recovery_is_bounded(self):
        app = object.__new__(main.BridgeApp)
        app._connect_voice = AsyncMock(side_effect=RuntimeError("voice unavailable"))
        app._schedule_originate = Mock()

        with (
            patch.object(main, "MAX_RECOVERY_ATTEMPTS", 3),
            patch.object(main.asyncio, "sleep", new=AsyncMock()),
        ):
            recovered = await app._recover_voice_cycle()

        self.assertEqual(app._connect_voice.await_count, 3)
        app._schedule_originate.assert_not_called()
        self.assertFalse(recovered)

    async def test_successful_discord_recovery_starts_ami_recovery(self):
        app = object.__new__(main.BridgeApp)
        app._connect_voice = AsyncMock()
        app._schedule_originate = Mock()
        app.voice_client = SimpleNamespace(
            is_connected=lambda: True,
            is_playing=lambda: True,
            is_listening=lambda: True,
        )

        with patch.object(main.asyncio, "sleep", new=AsyncMock()):
            recovered = await app._recover_voice_cycle()

        app._schedule_originate.assert_called_once_with()
        self.assertTrue(recovered)

    async def test_ami_recovery_is_bounded(self):
        app = object.__new__(main.BridgeApp)
        app._audiosocket_connected = main.asyncio.Event()
        app.ami = SimpleNamespace(originate=Mock(side_effect=RuntimeError("AMI unavailable")))
        app.settings = SimpleNamespace(
            audiosocket_extension="discord",
            asterisk_context="default",
            conference_extension="160",
        )

        with (
            patch.object(main, "MAX_RECOVERY_ATTEMPTS", 3),
            patch.object(main.asyncio, "sleep", new=AsyncMock()),
        ):
            recovered = await app._recover_audiosocket_cycle()

        self.assertEqual(app.ami.originate.call_count, 3)
        self.assertFalse(recovered)

    async def test_discord_recovery_retries_after_cooldown(self):
        app = object.__new__(main.BridgeApp)
        app._recover_voice_cycle = AsyncMock(side_effect=[False, True])

        with (
            patch.object(main, "RECOVERY_COOLDOWN_SECONDS", 300.0),
            patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            await app._recover_voice()

        self.assertEqual(app._recover_voice_cycle.await_count, 2)
        sleep.assert_awaited_once_with(300.0)

    async def test_ami_recovery_retries_after_cooldown(self):
        app = object.__new__(main.BridgeApp)
        app._recover_audiosocket_cycle = AsyncMock(side_effect=[False, True])

        with (
            patch.object(main, "RECOVERY_COOLDOWN_SECONDS", 300.0),
            patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            await app._recover_audiosocket()

        self.assertEqual(app._recover_audiosocket_cycle.await_count, 2)
        sleep.assert_awaited_once_with(300.0)


if __name__ == "__main__":
    unittest.main()
