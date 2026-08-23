import socket
import threading

import numpy as np

AUDIOSOCKET_AUDIO_TYPE = 0x10
ASTERISK_FRAME_BYTES = 320  # 8 kHz, mono, signed 16-bit, 20 ms
DISCORD_FRAME_BYTES = 3840  # 48 kHz, stereo, signed 16-bit, 20 ms


def recv_exact(connection: socket.socket, size: int) -> bytes | None:
    """Receive exactly size bytes, or None if the peer closes early."""
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def make_audiosocket_audio_frame(pcm: bytes) -> bytes:
    if len(pcm) > 0xFFFF:
        raise ValueError("AudioSocket payload exceeds the 16-bit length field")
    return bytes((AUDIOSOCKET_AUDIO_TYPE,)) + len(pcm).to_bytes(2, "big") + pcm


def asterisk_pcm_to_discord(pcm: bytes) -> bytes:
    """Convert 8 kHz mono s16le PCM to 48 kHz stereo s16le PCM."""
    samples = np.frombuffer(pcm, dtype="<i2")
    upsampled = np.repeat(samples, 6)
    stereo = np.repeat(upsampled[:, None], 2, axis=1)
    return stereo.astype("<i2", copy=False).tobytes()


def discord_pcm_to_asterisk(pcm: bytes) -> bytes:
    """Convert 48 kHz stereo s16le PCM to 8 kHz mono s16le PCM.

    Averaging each six-sample window also supplies a small low-pass filter before
    decimation. Discord voice receive normally supplies one 20 ms frame.
    """
    samples = np.frombuffer(pcm, dtype="<i2")
    complete_stereo_samples = samples.size - (samples.size % 2)
    if complete_stereo_samples == 0:
        return b""

    stereo = samples[:complete_stereo_samples].reshape(-1, 2).astype(np.int32)
    mono = np.rint(stereo.mean(axis=1)).astype(np.int32)

    complete_windows = mono.size - (mono.size % 6)
    if complete_windows == 0:
        return b""

    downsampled = np.rint(mono[:complete_windows].reshape(-1, 6).mean(axis=1))
    return np.clip(downsampled, -32768, 32767).astype("<i2").tobytes()


class AsteriskPcmBuffer:
    """Thread-safe buffer that presents AudioSocket PCM as Discord frames."""

    def __init__(self, max_frames: int = 200):
        self._buffer = bytearray()
        self._max_bytes = ASTERISK_FRAME_BYTES * max_frames
        self._lock = threading.Lock()

    def feed(self, pcm: bytes) -> None:
        # A signed 16-bit sample cannot be split on an odd byte boundary.
        pcm = pcm[: len(pcm) - (len(pcm) % 2)]
        if not pcm:
            return

        with self._lock:
            self._buffer.extend(pcm)
            overflow = len(self._buffer) - self._max_bytes
            if overflow > 0:
                overflow += overflow % 2
                del self._buffer[:overflow]

    def read_discord_frame(self) -> bytes:
        with self._lock:
            if len(self._buffer) < ASTERISK_FRAME_BYTES:
                return b"\x00" * DISCORD_FRAME_BYTES
            pcm = bytes(self._buffer[:ASTERISK_FRAME_BYTES])
            del self._buffer[:ASTERISK_FRAME_BYTES]

        return asterisk_pcm_to_discord(pcm)


class AudioSocketWriter:
    """Thread-safe writer for the current full-duplex AudioSocket connection."""

    def __init__(self):
        self._connection: socket.socket | None = None
        self._lock = threading.Lock()

    def attach(self, connection: socket.socket) -> None:
        with self._lock:
            self._connection = connection

    def detach(self, connection: socket.socket) -> None:
        with self._lock:
            if self._connection is connection:
                self._connection = None

    def send_audio(self, pcm: bytes) -> bool:
        if not pcm:
            return False

        frame = make_audiosocket_audio_frame(pcm)
        with self._lock:
            connection = self._connection
            if connection is None:
                return False
            try:
                connection.sendall(frame)
            except OSError:
                if self._connection is connection:
                    self._connection = None
                return False
        return True
