import socket
import sys
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from audio_bridge import (
    ASTERISK_FRAME_BYTES,
    DISCORD_FRAME_BYTES,
    AsteriskPcmBuffer,
    AudioSocketWriter,
    DiscordPcmMixer,
    asterisk_pcm_to_discord,
    discord_pcm_to_asterisk,
    make_audiosocket_audio_frame,
    recv_exact,
)


class AudioConversionTests(unittest.TestCase):
    def test_asterisk_frame_becomes_one_discord_frame(self):
        source = np.arange(160, dtype="<i2")

        converted = asterisk_pcm_to_discord(source.tobytes())
        samples = np.frombuffer(converted, dtype="<i2").reshape(-1, 2)

        self.assertEqual(len(converted), DISCORD_FRAME_BYTES)
        np.testing.assert_array_equal(samples[:, 0], np.repeat(source, 6))
        np.testing.assert_array_equal(samples[:, 1], np.repeat(source, 6))

    def test_discord_frame_becomes_one_asterisk_frame(self):
        expected = np.arange(-80, 80, dtype="<i2")
        mono_48k = np.repeat(expected, 6)
        stereo_48k = np.repeat(mono_48k[:, None], 2, axis=1).astype("<i2")

        converted = discord_pcm_to_asterisk(stereo_48k.tobytes())

        self.assertEqual(len(converted), ASTERISK_FRAME_BYTES)
        np.testing.assert_array_equal(np.frombuffer(converted, dtype="<i2"), expected)


class AsteriskPcmBufferTests(unittest.TestCase):
    def test_aggregates_partial_audiosocket_messages(self):
        pcm = np.arange(160, dtype="<i2").tobytes()
        buffer = AsteriskPcmBuffer()
        buffer.feed(pcm[:100])

        self.assertEqual(buffer.read_discord_frame(), b"\x00" * DISCORD_FRAME_BYTES)

        buffer.feed(pcm[100:])
        self.assertEqual(buffer.read_discord_frame(), asterisk_pcm_to_discord(pcm))

    def test_clear_drops_audio_from_a_previous_connection(self):
        buffer = AsteriskPcmBuffer()
        buffer.feed(np.arange(160, dtype="<i2").tobytes())

        buffer.clear()

        self.assertEqual(buffer.read_discord_frame(), b"\x00" * DISCORD_FRAME_BYTES)


class AudioSocketTests(unittest.TestCase):
    def test_audio_frame_uses_big_endian_length(self):
        pcm = b"\x01\x02" * 160
        frame = make_audiosocket_audio_frame(pcm)

        self.assertEqual(frame[:3], b"\x10\x01\x40")
        self.assertEqual(frame[3:], pcm)

    def test_recv_exact_combines_partial_reads(self):
        sender, receiver = socket.socketpair()
        try:
            sender.sendall(b"abc")
            sender.sendall(b"def")
            self.assertEqual(recv_exact(receiver, 6), b"abcdef")
        finally:
            sender.close()
            receiver.close()

    def test_writer_sends_to_attached_full_duplex_connection(self):
        bridge, asterisk = socket.socketpair()
        writer = AudioSocketWriter()
        try:
            writer.attach(bridge)
            self.assertTrue(writer.send_audio(b"\x01\x00" * 160))
            self.assertEqual(recv_exact(asterisk, 3), b"\x10\x01\x40")
            self.assertEqual(recv_exact(asterisk, 320), b"\x01\x00" * 160)

            writer.detach(bridge)
            self.assertFalse(writer.send_audio(b"\x00\x00"))
        finally:
            writer.close()
            bridge.close()
            asterisk.close()

    def test_writer_enqueue_does_not_wait_for_socket(self):
        bridge, asterisk = socket.socketpair()
        writer = AudioSocketWriter(max_frames=2, autostart=False)
        try:
            writer.attach(bridge)
            started = time.monotonic()
            for _ in range(100):
                writer.send_audio(b"\x00\x00" * 160)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.1)
        finally:
            writer.close()
            bridge.close()
            asterisk.close()


class DiscordPcmMixerTests(unittest.TestCase):
    def test_mixes_simultaneous_speakers_into_one_frame(self):
        output = []
        mixer = DiscordPcmMixer(lambda pcm: output.append(pcm) or True, autostart=False)
        try:
            mixer.push(1, np.full(160, 1000, dtype="<i2").tobytes())
            mixer.push(2, np.full(160, -250, dtype="<i2").tobytes())

            mixed = mixer.mix_once()

            self.assertEqual(output, [mixed])
            np.testing.assert_array_equal(
                np.frombuffer(mixed, dtype="<i2"),
                np.full(160, 750, dtype="<i2"),
            )
        finally:
            mixer.close()

    def test_clips_mixed_audio_and_emits_only_one_frame_per_tick(self):
        output = []
        mixer = DiscordPcmMixer(lambda pcm: output.append(pcm) or True, autostart=False)
        try:
            two_frames = np.full(320, 30000, dtype="<i2").tobytes()
            mixer.push(1, two_frames)
            mixer.push(2, two_frames)

            first = mixer.mix_once()
            second = mixer.mix_once()

            self.assertEqual(len(output), 2)
            np.testing.assert_array_equal(
                np.frombuffer(first, dtype="<i2"),
                np.full(160, 32767, dtype="<i2"),
            )
            np.testing.assert_array_equal(
                np.frombuffer(second, dtype="<i2"),
                np.full(160, 32767, dtype="<i2"),
            )
        finally:
            mixer.close()


if __name__ == "__main__":
    unittest.main()
