import errno
import sys
import socket
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import main
from audio_bridge import (
    AsteriskPcmBuffer,
    AudioSocketWriter,
    discord_pcm_to_asterisk,
    recv_exact,
)


class DiscordAudioSinkTests(unittest.TestCase):
    def test_forwards_audio_from_another_bot(self):
        mixer = Mock()
        sink = main.DiscordAudioSink(mixer, bridge_user_id=1)
        pcm = b"\x00\x00" * 1920

        sink.write(
            SimpleNamespace(id=2, bot=True),
            SimpleNamespace(pcm=pcm),
        )

        mixer.push.assert_called_once_with(2, discord_pcm_to_asterisk(pcm))

    def test_drops_only_the_bridge_users_audio(self):
        mixer = Mock()
        sink = main.DiscordAudioSink(mixer, bridge_user_id=1)

        sink.write(
            SimpleNamespace(id=1, bot=True),
            SimpleNamespace(pcm=b"\x00\x00" * 1920),
        )

        mixer.push.assert_not_called()


class AudioSocketServerTests(unittest.TestCase):
    UUID_FRAME = b"\x01\x00\x10" + bytes(range(16))

    def test_new_valid_connection_replaces_current_connection(self):
        pcm_buffer = AsteriskPcmBuffer()
        writer = AudioSocketWriter()
        connected = []
        disconnected = []
        server = main.AudioSocketServer(
            "127.0.0.1",
            0,
            pcm_buffer,
            writer,
            connected.append,
            disconnected.append,
        )
        first = second = None
        try:
            server.start()
            server.wait_until_ready()
            first = socket.create_connection(("127.0.0.1", server.port))
            first.sendall(self.UUID_FRAME)
            self._wait_until(lambda: len(connected) == 1)

            second = socket.create_connection(("127.0.0.1", server.port))
            second.sendall(self.UUID_FRAME)
            self._wait_until(lambda: len(connected) == 2)

            first.settimeout(1)
            self.assertEqual(first.recv(1), b"")
            self.assertTrue(writer.send_audio(b"\x02\x00" * 160))
            self.assertEqual(recv_exact(second, 3), b"\x10\x01\x40")
            self.assertEqual(recv_exact(second, 320), b"\x02\x00" * 160)
            self.assertEqual(disconnected, [])
            self.assertEqual(connected, [1, 2])
        finally:
            if first is not None:
                first.close()
            if second is not None:
                second.close()
            server.close()
            writer.close()

    def test_idle_candidate_times_out_without_becoming_connected(self):
        pcm_buffer = AsteriskPcmBuffer()
        writer = AudioSocketWriter()
        connected = []
        disconnected = []
        server = main.AudioSocketServer(
            "127.0.0.1",
            0,
            pcm_buffer,
            writer,
            connected.append,
            disconnected.append,
        )
        candidate = None
        try:
            with patch.object(main, "AUDIOSOCKET_HANDSHAKE_TIMEOUT_SECONDS", 0.05):
                server.start()
                server.wait_until_ready()
                candidate = socket.create_connection(("127.0.0.1", server.port))
                candidate.settimeout(1)
                self.assertEqual(candidate.recv(1), b"")

            self.assertEqual(connected, [])
            self.assertEqual(disconnected, [])
            self.assertFalse(writer.send_audio(b"\x00\x00" * 160))
        finally:
            if candidate is not None:
                candidate.close()
            server.close()
            writer.close()

    def test_candidate_without_uuid_is_rejected(self):
        pcm_buffer = AsteriskPcmBuffer()
        writer = AudioSocketWriter()
        connected = []
        server = main.AudioSocketServer(
            "127.0.0.1",
            0,
            pcm_buffer,
            writer,
            connected.append,
            lambda generation: None,
        )
        candidate = None
        try:
            server.start()
            server.wait_until_ready()
            candidate = socket.create_connection(("127.0.0.1", server.port))
            candidate.sendall(b"\x10\x00\x02\x00\x00")
            candidate.settimeout(1)

            self.assertEqual(candidate.recv(1), b"")
            self.assertEqual(connected, [])
            self.assertFalse(writer.send_audio(b"\x00\x00" * 160))
        finally:
            if candidate is not None:
                candidate.close()
            server.close()
            writer.close()

    def test_transient_accept_error_is_retried(self):
        listener = MagicMock()
        listener.getsockname.return_value = ("127.0.0.1", 5000)
        release_accept = threading.Event()
        accept_calls = 0

        def accept():
            nonlocal accept_calls
            accept_calls += 1
            if accept_calls == 1:
                raise OSError(errno.ECONNABORTED, "connection aborted")
            release_accept.wait(1)
            raise OSError(errno.EBADF, "listener closed")

        listener.accept.side_effect = accept
        listener.close.side_effect = release_accept.set
        failed = []
        writer = AudioSocketWriter()
        server = main.AudioSocketServer(
            "127.0.0.1",
            5000,
            AsteriskPcmBuffer(),
            writer,
            lambda generation: None,
            lambda generation: None,
            failed.append,
        )
        try:
            with (
                patch.object(main.socket, "socket", return_value=listener),
                patch.object(main, "AUDIOSOCKET_ACCEPT_RETRY_DELAY_SECONDS", 0),
            ):
                server.start()
                server.wait_until_ready()
                self._wait_until(lambda: accept_calls >= 2)

            self.assertEqual(failed, [])
        finally:
            server.close()
            writer.close()

    def test_fatal_accept_error_notifies_bridge(self):
        listener = MagicMock()
        listener.getsockname.return_value = ("127.0.0.1", 5000)
        listener.accept.side_effect = OSError(errno.EBADF, "invalid listener")
        failed = []
        writer = AudioSocketWriter()
        server = main.AudioSocketServer(
            "127.0.0.1",
            5000,
            AsteriskPcmBuffer(),
            writer,
            lambda generation: None,
            lambda generation: None,
            failed.append,
        )
        try:
            with patch.object(main.socket, "socket", return_value=listener):
                server.start()
                server.wait_until_ready()
                self._wait_until(lambda: len(failed) == 1)

            self.assertEqual(failed[0].errno, errno.EBADF)
        finally:
            server.close()
            writer.close()

    def _wait_until(self, condition, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("condition was not met before timeout")
            time.sleep(0.01)


class BridgeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fatal_audiosocket_listener_error_closes_client(self):
        app = object.__new__(main.BridgeApp)
        app.client = SimpleNamespace(close=AsyncMock())
        app._shutdown_task = None

        app._handle_audiosocket_failed(OSError(errno.EBADF, "invalid listener"))
        await main.asyncio.sleep(0)

        app.client.close.assert_awaited_once_with()

    async def test_startup_failure_closes_discord_client(self):
        bridge = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("bind failed")))
        client = SimpleNamespace(user="bridge", close=AsyncMock())

        with (
            patch.object(main, "bridge", bridge),
            patch.object(main, "client", client),
            self.assertRaisesRegex(RuntimeError, "bind failed"),
        ):
            await main.on_ready()

        client.close.assert_awaited_once_with()

    async def test_stale_audiosocket_disconnect_does_not_clear_new_connection(self):
        app = object.__new__(main.BridgeApp)
        app._audiosocket_connected = main.asyncio.Event()
        app._audiosocket_generation = 0
        app._schedule_originate = Mock()

        app._handle_audiosocket_connected(2)
        app._handle_audiosocket_disconnected(1)

        self.assertTrue(app._audiosocket_connected.is_set())
        app._schedule_originate.assert_not_called()

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
