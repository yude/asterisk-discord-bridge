import asyncio
import os
import socket
import threading

import discord
import discord.opus
from discord.ext import voice_recv

from audio_bridge import (
    AUDIOSOCKET_AUDIO_TYPE,
    AsteriskPcmBuffer,
    AudioSocketWriter,
    discord_pcm_to_asterisk,
    recv_exact,
)

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
VOICE_CHANNEL_ID = int(os.environ["VOICE_CHANNEL_ID"])
ASTERISK_AMI_HOST = os.environ.get("ASTERISK_AMI_HOST", "127.0.0.1")
ASTERISK_AMI_USER = os.environ["ASTERISK_AMI_USER"]
ASTERISK_AMI_SECRET = os.environ["ASTERISK_AMI_SECRET"]

AST_HOST = "0.0.0.0"
AST_PORT = 5000

intents = discord.Intents.default()
client = discord.Client(intents=intents)

if not discord.opus.is_loaded():
    discord.opus.load_opus("libopus.so.0")

asterisk_pcm = AsteriskPcmBuffer()
audiosocket_writer = AudioSocketWriter()

_originate_done = False
_bridge_started = False


def join_confbridge():
    global _originate_done
    if _originate_done:
        print("Already joined ConfBridge, skipping")
        return

    _originate_done = True
    print("Joining ConfBridge via AMI...")

    try:
        with socket.create_connection((ASTERISK_AMI_HOST, 5038), timeout=10) as ami:
            login_cmd = f"""Action: Login
Username: {ASTERISK_AMI_USER}
Secret: {ASTERISK_AMI_SECRET}

"""
            ami.sendall(login_cmd.encode())
            response = ami.recv(1024)
            print("AMI login:", response.decode(errors="ignore"))

            originate_cmd = """Action: Originate
Channel: Local/discord@default
Exten: 160
Context: default
Priority: 1
Async: true

"""
            ami.sendall(originate_cmd.encode())
            response = ami.recv(1024)
            print("AMI originate:", response.decode(errors="ignore"))
            ami.sendall(b"Action: Logoff\n\n")
    except OSError as error:
        _originate_done = False
        print("AMI error:", error)


def asterisk_receiver():
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((AST_HOST, AST_PORT))
        server.listen(5)
        print("AudioSocket listening on", AST_PORT)

        while True:
            connection, address = server.accept()
            print("AudioSocket connected:", address)
            audiosocket_writer.attach(connection)

            try:
                while True:
                    header = recv_exact(connection, 3)
                    if header is None:
                        break

                    message_type = header[0]
                    length = int.from_bytes(header[1:], "big")
                    if message_type == 0x00:
                        break

                    payload = recv_exact(connection, length)
                    if payload is None:
                        break

                    if message_type == AUDIOSOCKET_AUDIO_TYPE:
                        asterisk_pcm.feed(payload)
                    elif message_type == 0xFF:
                        print("AudioSocket peer reported error:", payload.hex())
            except OSError as error:
                print("AudioSocket error:", error)
            finally:
                audiosocket_writer.detach(connection)
                connection.close()
                print("AudioSocket disconnected")


class AsteriskAudio(discord.AudioSource):
    def read(self):
        return asterisk_pcm.read_discord_frame()

    def is_opus(self):
        return False


class DiscordAudioSink(voice_recv.AudioSink):
    def __init__(self):
        super().__init__()
        self._warned_no_audiosocket = False

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        # Unknown senders and bots are excluded to avoid feeding the bridge's own
        # transmission back into Asterisk.
        if user is None or user.bot or data.pcm is None:
            return

        pcm = discord_pcm_to_asterisk(data.pcm)
        sent = audiosocket_writer.send_audio(pcm)
        if not sent and not self._warned_no_audiosocket:
            print("Discord audio received before AudioSocket connected; dropping it")
            self._warned_no_audiosocket = True
        elif sent:
            self._warned_no_audiosocket = False

    def cleanup(self) -> None:
        pass


def voice_receive_stopped(error: Exception | None) -> None:
    if error is not None:
        print("Discord voice receive stopped:", error)


@client.event
async def on_ready():
    global _bridge_started
    if _bridge_started:
        return
    _bridge_started = True

    print("Logged in as", client.user)
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        raise RuntimeError(f"Discord guild {GUILD_ID} was not found")
    channel = guild.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        raise LookupError(f"Discord channel {VOICE_CHANNEL_ID} was not found")
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        raise TypeError(f"Discord channel {VOICE_CHANNEL_ID} is not a voice channel")

    threading.Thread(target=asterisk_receiver, daemon=True).start()
    await asyncio.sleep(2)

    voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
    voice_client.play(AsteriskAudio())
    voice_client.listen(DiscordAudioSink(), after=voice_receive_stopped)
    print("Connected to Discord (send and receive)")

    await asyncio.sleep(1)
    join_confbridge()


client.run(TOKEN)
