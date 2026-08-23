import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import main


class BridgeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_recovery_is_bounded(self):
        app = object.__new__(main.BridgeApp)
        app._connect_voice = AsyncMock(side_effect=RuntimeError("voice unavailable"))
        app._schedule_originate = Mock()

        with (
            patch.object(main, "MAX_RECOVERY_ATTEMPTS", 3),
            patch.object(main.asyncio, "sleep", new=AsyncMock()),
        ):
            await app._recover_voice()

        self.assertEqual(app._connect_voice.await_count, 3)
        app._schedule_originate.assert_not_called()

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
            await app._recover_voice()

        app._schedule_originate.assert_called_once_with()

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
            await app._recover_audiosocket()

        self.assertEqual(app.ami.originate.call_count, 3)


if __name__ == "__main__":
    unittest.main()
