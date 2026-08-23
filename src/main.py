from __future__ import annotations

import asyncio
import os
import socket
import threading
from dataclasses import dataclass

import discord
import discord.opus
from discord.ext import voice_recv

from ami import AmiClient
from audio_bridge import (
    AUDIOSOCKET_AUDIO_TYPE,
    AsteriskPcmBuffer,
    AudioSocketWriter,
    DiscordPcmMixer,
    discord_pcm_to_asterisk,
    recv_exact,
)
from voice_recv_compat import install_voice_recv_fixes

MAX_RECOVERY_ATTEMPTS = 5


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_id: int
    voice_channel_id: int
    ami_host: str
    ami_port: int
    ami_user: str
    ami_secret: str
    asterisk_context: str
    audiosocket_extension: str
    conference_extension: str
    audiosocket_host: str
    audiosocket_port: int

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            guild_id=int(os.environ["GUILD_ID"]),
            voice_channel_id=int(os.environ["VOICE_CHANNEL_ID"]),
            ami_host=os.environ.get("ASTERISK_AMI_HOST", "127.0.0.1"),
            ami_port=int(os.environ.get("ASTERISK_AMI_PORT", "5038")),
            ami_user=os.environ["ASTERISK_AMI_USER"],
            ami_secret=os.environ["ASTERISK_AMI_SECRET"],
            asterisk_context=os.environ.get("ASTERISK_CONTEXT", "default"),
            audiosocket_extension=os.environ.get("ASTERISK_AUDIOSOCKET_EXTENSION", "discord"),
            conference_extension=os.environ.get("ASTERISK_CONFERENCE_EXTENSION", "160"),
            audiosocket_host=os.environ.get("AUDIOSOCKET_LISTEN_HOST", "0.0.0.0"),
            audiosocket_port=int(os.environ.get("AUDIOSOCKET_LISTEN_PORT", "5000")),
        )


class AsteriskAudio(discord.AudioSource):
    def __init__(self, pcm_buffer: AsteriskPcmBuffer):
        self._pcm_buffer = pcm_buffer

    def read(self) -> bytes:
        return self._pcm_buffer.read_discord_frame()

    def is_opus(self) -> bool:
        return False


class DiscordAudioSink(voice_recv.AudioSink):
    def __init__(self, mixer: DiscordPcmMixer):
        super().__init__()
        self._mixer = mixer

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        # Unknown senders and bots are excluded to avoid feeding the bridge's
        # own transmission back into Asterisk.
        if user is None or user.bot or data.pcm is None:
            return
        self._mixer.push(user.id, discord_pcm_to_asterisk(data.pcm))

    def cleanup(self) -> None:
        # The mixer is shared across receive sessions.
        pass


class AudioSocketServer:
    def __init__(
        self,
        host: str,
        port: int,
        pcm_buffer: AsteriskPcmBuffer,
        writer: AudioSocketWriter,
        on_connected,
        on_disconnected,
    ):
        self.host = host
        self.port = port
        self._pcm_buffer = pcm_buffer
        self._writer = writer
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="audiosocket-receiver")

    def start(self) -> None:
        self._thread.start()

    def wait_until_ready(self, timeout: float = 10.0) -> None:
        if not self._ready.wait(timeout):
            raise TimeoutError("AudioSocket server did not start in time")
        if self._startup_error is not None:
            raise RuntimeError("AudioSocket server failed to start") from self._startup_error

    def _run(self) -> None:
        try:
            server = socket.socket()
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(5)
        except BaseException as error:
            self._startup_error = error
            self._ready.set()
            return

        self._ready.set()
        print(f"AudioSocket listening on {self.host}:{self.port}")
        with server:
            while True:
                connection, address = server.accept()
                print("AudioSocket connected:", address)
                self._pcm_buffer.clear()
                self._writer.attach(connection)
                self._on_connected()
                try:
                    self._receive(connection)
                except OSError as error:
                    print("AudioSocket error:", error)
                finally:
                    self._writer.detach(connection)
                    connection.close()
                    self._on_disconnected()
                    print("AudioSocket disconnected")

    def _receive(self, connection: socket.socket) -> None:
        while True:
            header = recv_exact(connection, 3)
            if header is None:
                return
            message_type = header[0]
            length = int.from_bytes(header[1:], "big")
            if message_type == 0x00:
                return
            payload = recv_exact(connection, length)
            if payload is None:
                return
            if message_type == AUDIOSOCKET_AUDIO_TYPE:
                self._pcm_buffer.feed(payload)
            elif message_type == 0xFF:
                print("AudioSocket peer reported error:", payload.hex())


class BridgeApp:
    def __init__(self, client: discord.Client, settings: Settings):
        self.client = client
        self.settings = settings
        self.pcm_buffer = AsteriskPcmBuffer()
        self.writer = AudioSocketWriter()
        self.mixer = DiscordPcmMixer(self.writer.send_audio)
        self.ami = AmiClient(
            settings.ami_host,
            settings.ami_user,
            settings.ami_secret,
            port=settings.ami_port,
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        self.channel: discord.VoiceChannel | None = None
        self.voice_client: voice_recv.VoiceRecvClient | None = None
        self._started = False
        self._voice_generation = 0
        self._voice_recovery_task: asyncio.Task | None = None
        self._originate_task: asyncio.Task | None = None
        self._audiosocket_connected = asyncio.Event()
        self.server = AudioSocketServer(
            settings.audiosocket_host,
            settings.audiosocket_port,
            self.pcm_buffer,
            self.writer,
            self._notify_audiosocket_connected,
            self._notify_audiosocket_disconnected,
        )

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.loop = asyncio.get_running_loop()
        guild = self.client.get_guild(self.settings.guild_id)
        if guild is None:
            raise RuntimeError(f"Discord guild {self.settings.guild_id} was not found")
        channel = guild.get_channel(self.settings.voice_channel_id)
        if channel is None:
            raise LookupError(f"Discord channel {self.settings.voice_channel_id} was not found")
        if not isinstance(channel, discord.VoiceChannel):
            raise TypeError(f"Discord channel {self.settings.voice_channel_id} is not a voice channel")
        self.channel = channel
        self.server.start()
        await asyncio.to_thread(self.server.wait_until_ready)
        self._schedule_voice_recovery()

    def _notify_audiosocket_connected(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._audiosocket_connected.set)

    def _notify_audiosocket_disconnected(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._handle_audiosocket_disconnected)

    def _handle_audiosocket_disconnected(self) -> None:
        self._audiosocket_connected.clear()
        self._schedule_originate()

    def _voice_path_stopped(
        self,
        generation: int,
        direction: str,
        error: Exception | None,
    ) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(
                self._handle_voice_path_stopped,
                generation,
                direction,
                error,
            )

    def _handle_voice_path_stopped(
        self,
        generation: int,
        direction: str,
        error: Exception | None,
    ) -> None:
        if generation != self._voice_generation:
            return
        print(f"Discord {direction} stopped; reconnecting:", error or "no error reported")
        self._schedule_voice_recovery()

    def _schedule_voice_recovery(self) -> None:
        if self._voice_recovery_task is None or self._voice_recovery_task.done():
            self._voice_recovery_task = asyncio.create_task(
                self._recover_voice(),
                name="discord-voice-recovery",
            )

    async def _recover_voice(self) -> None:
        retry_delay = 1.0
        for attempt in range(1, MAX_RECOVERY_ATTEMPTS + 1):
            try:
                await self._connect_voice()
            except Exception as error:
                print(
                    f"Discord voice attempt {attempt}/{MAX_RECOVERY_ATTEMPTS} failed:",
                    error,
                )
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
            else:
                # Let the player and reader threads enter their running state
                # before declaring recovery successful. An immediate failure
                # is consumed by this same bounded retry series.
                await asyncio.sleep(0.1)
                if (
                    self.voice_client is None
                    or not self.voice_client.is_connected()
                    or not self.voice_client.is_playing()
                    or not self.voice_client.is_listening()
                ):
                    print("Discord voice path stopped immediately after connecting")
                    continue
                self._schedule_originate()
                return
        print("Discord voice recovery exhausted; restart the bridge after correcting the cause")

    async def _connect_voice(self) -> None:
        if self.channel is None:
            raise RuntimeError("Discord voice channel has not been resolved")
        self._voice_generation += 1
        generation = self._voice_generation
        old_voice_client = self.voice_client
        self.voice_client = None
        if old_voice_client is not None:
            await old_voice_client.disconnect(force=True)

        voice_client = await self.channel.connect(cls=voice_recv.VoiceRecvClient, reconnect=True)
        try:
            voice_client.play(
                AsteriskAudio(self.pcm_buffer),
                after=lambda error: self._voice_path_stopped(generation, "transmit", error),
            )
            voice_client.listen(
                DiscordAudioSink(self.mixer),
                after=lambda error: self._voice_path_stopped(generation, "receive", error),
            )
        except BaseException:
            await voice_client.disconnect(force=True)
            raise
        self.voice_client = voice_client
        print("Connected to Discord (send and receive)")

    def _schedule_originate(self) -> None:
        if self._audiosocket_connected.is_set():
            return
        if self._originate_task is None or self._originate_task.done():
            self._originate_task = asyncio.create_task(
                self._recover_audiosocket(),
                name="ami-originate-recovery",
            )

    async def _recover_audiosocket(self) -> None:
        retry_delay = 1.0
        for attempt in range(1, MAX_RECOVERY_ATTEMPTS + 1):
            if self._audiosocket_connected.is_set():
                return
            try:
                await asyncio.to_thread(
                    self.ami.originate,
                    channel=(
                        f"Local/{self.settings.audiosocket_extension}"
                        f"@{self.settings.asterisk_context}"
                    ),
                    context=self.settings.asterisk_context,
                    extension=self.settings.conference_extension,
                )
                print("AMI originate accepted; waiting for AudioSocket")
                await asyncio.wait_for(self._audiosocket_connected.wait(), timeout=20.0)
                await asyncio.sleep(0.1)
                if not self._audiosocket_connected.is_set():
                    raise ConnectionError("AudioSocket disconnected immediately after connecting")
                return
            except Exception as error:
                print(
                    f"AMI originate attempt {attempt}/{MAX_RECOVERY_ATTEMPTS} failed:",
                    error,
                )
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
        print("AMI recovery exhausted; restart the bridge after correcting the cause")


intents = discord.Intents.default()
client = discord.Client(intents=intents)
bridge: BridgeApp | None = None


@client.event
async def on_ready() -> None:
    if bridge is None:
        raise RuntimeError("Bridge application was not initialized")
    print("Logged in as", client.user)
    await bridge.start()


def main() -> None:
    global bridge
    settings = Settings.from_environment()
    install_voice_recv_fixes()
    if not discord.opus.is_loaded():
        discord.opus.load_opus("libopus.so.0")
    bridge = BridgeApp(client, settings)
    client.run(settings.discord_token)


if __name__ == "__main__":
    main()
