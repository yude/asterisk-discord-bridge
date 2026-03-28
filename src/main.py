import discord
import socket
import threading
import asyncio
import numpy as np
import os
import discord.opus

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

pcm_queue = asyncio.Queue(maxsize=200)

_originate_done = False

def join_confbridge():
    global _originate_done
    if _originate_done:
        print("Already joined ConfBridge, skipping")
        return

    _originate_done = True

    print("Joining ConfBridge via AMI...")

    try:
        s = socket.socket()
        s.connect((ASTERISK_AMI_HOST, 5038))

        # Login
        login_cmd = f"""Action: Login
Username: {ASTERISK_AMI_USER}
Secret: {ASTERISK_AMI_SECRET}

"""
        s.sendall(login_cmd.encode())

        resp = s.recv(1024)
        print("AMI login:", resp.decode(errors="ignore"))

        # Originate
        originate_cmd = """Action: Originate
Channel: Local/discord@default
Exten: 160
Context: default
Priority: 1
Async: true

"""
        s.sendall(originate_cmd.encode())

        resp = s.recv(1024)
        print("AMI originate:", resp.decode(errors="ignore"))

        # Logoff
        s.sendall(b"Action: Logoff\n\n")
        s.close()

    except Exception as e:
        print("AMI error:", e)


def recv_exact(conn, size):
    buf = b""
    while len(buf) < size:
        chunk = conn.recv(size - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

def asterisk_receiver():
    s = socket.socket()
    s.bind((AST_HOST, AST_PORT))
    s.listen(5)

    print("AudioSocket listening on", AST_PORT)

    while True:
        conn, addr = s.accept()
        print("AudioSocket connected:", addr)

        try:
            while True:
                header = recv_exact(conn, 3)
                if not header:
                    break

                typ = header[0]
                length = int.from_bytes(header[1:], "big")

                payload = recv_exact(conn, length)
                if not payload:
                    break

                if typ == 0x10:
                    if not pcm_queue.full():
                        asyncio.run_coroutine_threadsafe(
                            pcm_queue.put(payload),
                            client.loop
                        )
        except Exception as e:
            print("AudioSocket error:", e)
        finally:
            print("AudioSocket disconnected")
            conn.close()

FRAME_SIZE = 3840  # 48kHz stereo 20ms

class AsteriskAudio(discord.AudioSource):
    def read(self):
        try:
            data = pcm_queue.get_nowait()
        except:
            return b"\x00" * FRAME_SIZE

        pcm = np.frombuffer(data, dtype=np.int16)

        # 8kHz → 48kHz
        pcm48 = np.repeat(pcm, 6)

        # mono → stereo
        pcm48 = np.repeat(pcm48[:, None], 2, axis=1).flatten()

        out = pcm48.tobytes()

        if len(out) < FRAME_SIZE:
            out += b"\x00" * (FRAME_SIZE - len(out))

        return out[:FRAME_SIZE]

    def is_opus(self):
        return False

@client.event
async def on_ready():
    print("Logged in")

    guild = client.get_guild(GUILD_ID)
    channel = guild.get_channel(VOICE_CHANNEL_ID)

    await asyncio.sleep(2)

    vc = await channel.connect()
    vc.play(AsteriskAudio())

    print("Connected to Discord")

    # AudioSocket受信開始
    threading.Thread(target=asterisk_receiver, daemon=True).start()

    # ConfBridge参加
    await asyncio.sleep(1)
    join_confbridge()


client.run(TOKEN)
