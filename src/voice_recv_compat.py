from __future__ import annotations

import logging
from enum import Enum, auto

from davey import MediaType
from discord.ext import voice_recv
from discord.ext.voice_recv.opus import PacketDecoder, VoiceData
from discord.ext.voice_recv.reader import AudioReader

SUPPORTED_VOICE_RECV_VERSION = "0.5.2a"

log = logging.getLogger(__name__)


class DavePacketState(Enum):
    PASSTHROUGH = auto()
    DECRYPTED = auto()
    DROP = auto()


def prepare_dave_packet(decoder: PacketDecoder, packet) -> DavePacketState:
    """Decrypt an inbound DAVE packet or identify packets that must be dropped."""
    if not packet or packet.is_silence() or packet.decrypted_data is None:
        return DavePacketState.PASSTHROUGH

    voice_client = decoder.sink.voice_client
    connection_state = getattr(voice_client, "_connection", None)
    if connection_state is None or getattr(connection_state, "dave_protocol_version", 0) == 0:
        return DavePacketState.PASSTHROUGH

    session = getattr(connection_state, "dave_session", None)
    if session is None or not session.ready:
        return DavePacketState.DROP

    user_id = decoder._cached_id or voice_client._get_id_from_ssrc(decoder.ssrc)
    if user_id is None:
        log.debug("DAVE packet dropped before SSRC %s was mapped to a user", decoder.ssrc)
        return DavePacketState.DROP
    decoder._cached_id = user_id

    try:
        packet.decrypted_data = session.decrypt(
            int(user_id),
            MediaType.audio,
            bytes(packet.decrypted_data),
        )
    except Exception as error:
        # Discord also sends a small number of plaintext passthrough packets.
        # Let the Opus decoder decide whether these are valid. Invalid encrypted
        # data stops the reader and is handled by the reconnect supervisor.
        log.debug("DAVE decrypt passthrough for user %s: %s", user_id, error)
        return DavePacketState.PASSTHROUGH
    return DavePacketState.DECRYPTED


def sync_reader_secret_key(reader: AudioReader) -> None:
    """Keep voice-recv's transport decryptor synchronized after fast resumes."""
    secret_key = bytes(reader.voice_client.secret_key)
    if getattr(reader, "_bridge_secret_key", None) == secret_key:
        return
    reader.update_secret_key(secret_key)
    reader._bridge_secret_key = secret_key


def install_voice_recv_fixes() -> None:
    """Install compatibility fixes required by voice-recv 0.5.2a179."""
    if voice_recv.__version__ != SUPPORTED_VOICE_RECV_VERSION:
        raise RuntimeError(
            "Unsupported discord-ext-voice-recv version "
            f"{voice_recv.__version__!r}; expected {SUPPORTED_VOICE_RECV_VERSION!r}"
        )

    if not getattr(PacketDecoder, "_bridge_dave_fix", False):
        original_process_packet = PacketDecoder._process_packet

        def process_packet(self: PacketDecoder, packet):
            state = prepare_dave_packet(self, packet)
            if state is DavePacketState.DROP:
                self._last_seq = packet.sequence
                self._last_ts = packet.timestamp
                return VoiceData(packet, None, pcm=b"")
            return original_process_packet(self, packet)

        PacketDecoder._process_packet = process_packet
        PacketDecoder._bridge_dave_fix = True

    if not getattr(AudioReader, "_bridge_key_sync_fix", False):
        original_callback = AudioReader.callback

        def callback(self: AudioReader, packet_data: bytes) -> None:
            sync_reader_secret_key(self)
            original_callback(self, packet_data)

        AudioReader.callback = callback
        AudioReader._bridge_key_sync_fix = True
