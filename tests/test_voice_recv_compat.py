import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from discord.ext.voice_recv.opus import PacketDecoder
from discord.ext.voice_recv.reader import AudioReader

from voice_recv_compat import (
    DavePacketState,
    install_voice_recv_fixes,
    prepare_dave_packet,
    sync_reader_secret_key,
)


class FakePacket:
    def __init__(self, data: bytes, *, silence: bool = False):
        self.decrypted_data = data
        self.sequence = 1
        self.timestamp = 960
        self._silence = silence

    def __bool__(self):
        return True

    def is_silence(self):
        return self._silence


class FakeDaveSession:
    ready = True

    def __init__(self, result: bytes | Exception):
        self.result = result
        self.calls = []

    def decrypt(self, user_id, media_type, data):
        self.calls.append((user_id, media_type, data))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_decoder(session, user_id=42):
    voice_client = SimpleNamespace(
        _connection=SimpleNamespace(dave_protocol_version=1, dave_session=session),
        _get_id_from_ssrc=lambda ssrc: user_id,
    )
    return SimpleNamespace(
        sink=SimpleNamespace(voice_client=voice_client),
        ssrc=1234,
        _cached_id=None,
    )


class DaveCompatibilityTests(unittest.TestCase):
    def test_runtime_patch_is_idempotent(self):
        install_voice_recv_fixes()
        process_packet = PacketDecoder._process_packet
        callback = AudioReader.callback

        install_voice_recv_fixes()

        self.assertIs(PacketDecoder._process_packet, process_packet)
        self.assertIs(AudioReader.callback, callback)

    def test_decrypts_dave_audio_with_the_mapped_sender(self):
        session = FakeDaveSession(b"opus")
        decoder = make_decoder(session)
        packet = FakePacket(b"encrypted")

        state = prepare_dave_packet(decoder, packet)

        self.assertIs(state, DavePacketState.DECRYPTED)
        self.assertEqual(packet.decrypted_data, b"opus")
        self.assertEqual(session.calls[0][0], 42)
        self.assertEqual(decoder._cached_id, 42)

    def test_drops_encrypted_audio_until_ssrc_is_mapped(self):
        decoder = make_decoder(FakeDaveSession(b"opus"), user_id=None)

        state = prepare_dave_packet(decoder, FakePacket(b"encrypted"))

        self.assertIs(state, DavePacketState.DROP)

    def test_plaintext_passthrough_survives_a_decrypt_error(self):
        decoder = make_decoder(FakeDaveSession(RuntimeError("not encrypted")))

        state = prepare_dave_packet(decoder, FakePacket(b"plaintext"))

        self.assertIs(state, DavePacketState.PASSTHROUGH)

    def test_resynchronizes_transport_key_only_when_it_changes(self):
        updates = []
        reader = SimpleNamespace(
            voice_client=SimpleNamespace(secret_key=[1, 2, 3]),
            update_secret_key=updates.append,
        )

        sync_reader_secret_key(reader)
        sync_reader_secret_key(reader)
        reader.voice_client.secret_key = [4, 5, 6]
        sync_reader_secret_key(reader)

        self.assertEqual(updates, [b"\x01\x02\x03", b"\x04\x05\x06"])


if __name__ == "__main__":
    unittest.main()
