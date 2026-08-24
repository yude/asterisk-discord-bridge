import socket
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
        buffer = AsteriskPcmBuffer(target_frames=1)
        buffer.feed(pcm[:100])

        self.assertEqual(buffer.read_discord_frame(), b"\x00" * DISCORD_FRAME_BYTES)

        buffer.feed(pcm[100:])
        self.assertEqual(buffer.read_discord_frame(), asterisk_pcm_to_discord(pcm))

    def test_clear_drops_audio_from_a_previous_connection(self):
        buffer = AsteriskPcmBuffer()
        previous = np.arange(320, dtype="<i2").tobytes()
        current = np.arange(160, dtype="<i2").tobytes()
        buffer.feed(previous)
        buffer.read_discord_frame()

        buffer.clear()
        buffer.feed(current)

        self.assertEqual(buffer.read_discord_frame(), b"\x00" * DISCORD_FRAME_BYTES)

    def test_read_preserves_a_small_jitter_window(self):
        buffer = AsteriskPcmBuffer()
        first = np.full(160, 1, dtype="<i2").tobytes()
        second = np.full(160, 2, dtype="<i2").tobytes()
        buffer.feed(first + second)

        self.assertEqual(buffer.read_discord_frame(), asterisk_pcm_to_discord(first))
        self.assertEqual(buffer.read_discord_frame(), asterisk_pcm_to_discord(second))

    def test_feed_catches_up_after_exceeding_the_high_watermark(self):
        buffer = AsteriskPcmBuffer(max_frames=3, target_frames=2)
        frames = [np.full(160, value, dtype="<i2").tobytes() for value in range(1, 5)]
        buffer.feed(b"".join(frames))

        self.assertEqual(buffer.read_discord_frame(), asterisk_pcm_to_discord(frames[2]))
        self.assertEqual(buffer.read_discord_frame(), asterisk_pcm_to_discord(frames[3]))

    def test_underflow_rebuffers_before_resuming_playback(self):
        buffer = AsteriskPcmBuffer(max_frames=5, target_frames=2)
        first = np.full(160, 1, dtype="<i2").tobytes()
        second = np.full(160, 2, dtype="<i2").tobytes()
        third = np.full(160, 3, dtype="<i2").tobytes()
        buffer.feed(first + second)
        buffer.read_discord_frame()
        buffer.read_discord_frame()

        self.assertEqual(buffer.read_discord_frame(), b"\x00" * DISCORD_FRAME_BYTES)
        buffer.feed(third)
        self.assertEqual(buffer.read_discord_frame(), b"\x00" * DISCORD_FRAME_BYTES)

        buffer.feed(first)
        self.assertEqual(buffer.read_discord_frame(), asterisk_pcm_to_discord(third))


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

    def test_writer_survives_closed_socket_select_race(self):
        first_bridge, first_asterisk = socket.socketpair()
        second_bridge, second_asterisk = socket.socketpair()
        writer = AudioSocketWriter(send_timeout=0.1)
        try:
            writer.attach(first_bridge)
            with patch("audio_bridge.select.select", side_effect=ValueError("closed fd")):
                self.assertTrue(writer.send_audio(b"\x01\x00" * 160))
                deadline = time.monotonic() + 1
                while writer.send_audio(b"\x00\x00") and time.monotonic() < deadline:
                    time.sleep(0.01)

            self.assertTrue(writer._thread.is_alive())
            writer.attach(second_bridge)
            self.assertTrue(writer.send_audio(b"\x02\x00" * 160))
            self.assertEqual(recv_exact(second_asterisk, 3), b"\x10\x01\x40")
            self.assertEqual(recv_exact(second_asterisk, 320), b"\x02\x00" * 160)
        finally:
            writer.close()
            first_bridge.close()
            first_asterisk.close()
            second_bridge.close()
            second_asterisk.close()

    def test_writer_drops_the_entire_backlog_when_latency_budget_is_full(self):
        bridge, asterisk = socket.socketpair()
        writer = AudioSocketWriter(max_frames=2, autostart=False)
        try:
            writer.attach(bridge)
            self.assertTrue(writer.send_audio(b"\x01\x00" * 160))
            self.assertTrue(writer.send_audio(b"\x02\x00" * 160))
            self.assertTrue(writer.send_audio(b"\x03\x00" * 160))
            writer.start()

            self.assertEqual(recv_exact(asterisk, 3), b"\x10\x01\x40")
            self.assertEqual(recv_exact(asterisk, 320), b"\x03\x00" * 160)
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

    def test_speaker_buffer_catches_up_after_exceeding_latency_budget(self):
        output = []
        mixer = DiscordPcmMixer(
            lambda pcm: output.append(pcm) or True,
            max_buffered_frames=2,
            autostart=False,
        )
        try:
            pcm = np.concatenate(
                [
                    np.full(160, 1, dtype="<i2"),
                    np.full(160, 2, dtype="<i2"),
                    np.full(160, 3, dtype="<i2"),
                ]
            ).tobytes()
            mixer.push(1, pcm)

            mixed = mixer.mix_once()

            np.testing.assert_array_equal(
                np.frombuffer(mixed, dtype="<i2"),
                np.full(160, 3, dtype="<i2"),
            )
        finally:
            mixer.close()


if __name__ == "__main__":
    unittest.main()
