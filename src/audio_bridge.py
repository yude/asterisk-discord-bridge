import queue
import select
import socket
import threading
import time
from collections.abc import Callable

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

    def __init__(self, max_frames: int = 10):
        if max_frames < 1:
            raise ValueError("max_frames must be at least one")
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

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def read_discord_frame(self) -> bytes:
        with self._lock:
            if len(self._buffer) < ASTERISK_FRAME_BYTES:
                return b"\x00" * DISCORD_FRAME_BYTES

            # Audio cannot be played faster than real time. If more than one
            # complete frame accumulated, skip stale frames rather than keeping
            # the Discord side permanently behind the live conversation.
            complete_frames = len(self._buffer) // ASTERISK_FRAME_BYTES
            if complete_frames > 1:
                stale_bytes = (complete_frames - 1) * ASTERISK_FRAME_BYTES
                del self._buffer[:stale_bytes]
            pcm = bytes(self._buffer[:ASTERISK_FRAME_BYTES])
            del self._buffer[:ASTERISK_FRAME_BYTES]

        return asterisk_pcm_to_discord(pcm)


class AudioSocketWriter:
    """Queue AudioSocket frames without blocking Discord's receive thread."""

    def __init__(
        self,
        *,
        max_frames: int = 5,
        send_timeout: float = 1.0,
        autostart: bool = True,
    ):
        if max_frames < 1:
            raise ValueError("max_frames must be at least one")
        self._connection: socket.socket | None = None
        self._lock = threading.Lock()
        self._generation = 0
        self._queue: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=max_frames)
        self._send_timeout = send_timeout
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"audiosocket-writer-{id(self):x}",
        )
        if autostart:
            self.start()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def attach(self, connection: socket.socket) -> None:
        with self._lock:
            self._generation += 1
            self._connection = connection
            generation = self._generation
        self._discard_stale_frames(generation)

    def detach(self, connection: socket.socket) -> None:
        with self._lock:
            if self._connection is connection:
                self._connection = None
                self._generation += 1

    def send_audio(self, pcm: bytes) -> bool:
        if not pcm:
            return False

        frame = make_audiosocket_audio_frame(pcm)
        with self._lock:
            if self._connection is None:
                return False
            generation = self._generation

        item = (generation, frame)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Once the latency budget is exhausted, catch up to live audio in
            # one step instead of staying a full queue behind indefinitely.
            self._clear_queue()
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                return False
        return True

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _discard_stale_frames(self, generation: int) -> None:
        retained: list[tuple[int, bytes]] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == generation:
                retained.append(item)
        for item in retained:
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                break

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _current_connection(self, generation: int) -> socket.socket | None:
        with self._lock:
            if generation != self._generation:
                return None
            return self._connection

    def _send_frame(self, connection: socket.socket, frame: bytes) -> None:
        view = memoryview(frame)
        deadline = time.monotonic() + self._send_timeout
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("AudioSocket write timed out")
            _, writable, exceptional = select.select([], [connection], [connection], remaining)
            if exceptional or not writable:
                raise TimeoutError("AudioSocket write timed out")
            sent = connection.send(view)
            if sent <= 0:
                raise ConnectionError("AudioSocket connection closed while writing")
            view = view[sent:]

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                generation, frame = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            connection = self._current_connection(generation)
            if connection is None:
                continue

            try:
                self._send_frame(connection, frame)
            except (OSError, TimeoutError, ValueError):
                self.detach(connection)
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass


class DiscordPcmMixer:
    """Mix Discord speakers into one paced 8 kHz AudioSocket stream."""

    def __init__(
        self,
        output: Callable[[bytes], bool],
        *,
        frame_interval: float = 0.02,
        max_buffered_frames: int = 5,
        autostart: bool = True,
    ):
        if max_buffered_frames < 1:
            raise ValueError("max_buffered_frames must be at least one")
        self._output = output
        self._frame_interval = frame_interval
        self._max_buffered_bytes = ASTERISK_FRAME_BYTES * max_buffered_frames
        self._buffers: dict[int, bytearray] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"discord-mixer-{id(self):x}",
        )
        if autostart:
            self.start()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def push(self, speaker_id: int, pcm: bytes) -> None:
        pcm = pcm[: len(pcm) - (len(pcm) % 2)]
        if not pcm:
            return

        with self._lock:
            speaker_buffer = self._buffers.setdefault(speaker_id, bytearray())
            speaker_buffer.extend(pcm)
            overflow = len(speaker_buffer) - self._max_buffered_bytes
            if overflow > 0:
                # Preserve only the newest frame. Keeping a full buffer would
                # make the speaker remain delayed after the stall has ended.
                del speaker_buffer[:-ASTERISK_FRAME_BYTES]

    def mix_once(self) -> bytes | None:
        frames: list[np.ndarray] = []
        with self._lock:
            inactive: list[int] = []
            for speaker_id, speaker_buffer in self._buffers.items():
                if len(speaker_buffer) >= ASTERISK_FRAME_BYTES:
                    pcm = bytes(speaker_buffer[:ASTERISK_FRAME_BYTES])
                    del speaker_buffer[:ASTERISK_FRAME_BYTES]
                    frames.append(np.frombuffer(pcm, dtype="<i2").astype(np.int32))
                if not speaker_buffer:
                    inactive.append(speaker_id)
            for speaker_id in inactive:
                del self._buffers[speaker_id]

        if not frames:
            return None

        mixed = np.sum(frames, axis=0, dtype=np.int32)
        pcm = np.clip(mixed, -32768, 32767).astype("<i2").tobytes()
        self._output(pcm)
        return pcm

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        next_frame = time.monotonic()
        while not self._stop.is_set():
            next_frame += self._frame_interval
            self.mix_once()
            delay = max(0.0, next_frame - time.monotonic())
            if self._stop.wait(delay):
                break
            if delay == 0.0:
                next_frame = time.monotonic()
