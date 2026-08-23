# asterisk-discord-bridge

Discord のボイスチャンネルと Asterisk の ConfBridge 会議室を相互接続するブリッジです。

音声は次の両方向に変換されます。

* Asterisk AudioSocket (8 kHz / mono / signed linear PCM) → Discord (48 kHz / stereo PCM)
* Discord voice receive (48 kHz / stereo PCM) → Asterisk AudioSocket (8 kHz / mono / signed linear PCM)

Discord からの音声受信には [`discord-ext-voice-recv`](https://github.com/imayhaveborkedit/discord-ext-voice-recv) を使用します。DiscordのDAVEエンドツーエンド暗号化に対応するため、受信フレームの復号と高速再接続時のtransport鍵再同期を `src/voice_recv_compat.py` で補完しています。この互換層は固定済みの `discord-ext-voice-recv` バージョンだけを対象とし、互換性が確認されていないバージョンでは起動を中止します。

## 前提条件

* Linux ホスト上で Docker Engine と Docker Compose v2 が利用できること
* Asterisk 18 以降が動作していること
    * `AudioSocket()` は Asterisk 18.0.0 から利用できます。
    * このREADMEでは [`tkytel/asterisk-quickstart`](https://github.com/tkytel/asterisk-quickstart) を同じホストで動かす構成を例にします。
* Asterisk の `default` context の内線 `160` で ConfBridge 会議室へ参加できること
* Discord サーバーへBotを追加できる権限があること
* TCPポート `5000` を別のプロセスが使用していないこと

本リポジトリのCompose設定と `asterisk-quickstart` は、どちらも `network_mode: host` を使用します。以下の手順は、両コンテナを同じLinuxホストで動かすことを前提としています。

## 1. Discord Botを準備する

1. [Discord Developer Portal](https://discord.com/developers/applications) でApplicationを作成し、Botユーザーを追加します。
2. Botページでトークンを発行します。トークンはパスワードと同様に扱い、Gitへコミットしないでください。
3. InstallationページでGuild Installに `bot` scopeを追加し、次の権限を付けて対象サーバーへインストールします。
    * View Channels
    * Connect
    * Speak
4. Discordクライアントの「ユーザー設定」→「詳細設定」から開発者モードを有効にします。
5. 対象サーバーを右クリックして「サーバーIDをコピー」、対象ボイスチャンネルを右クリックして「チャンネルIDをコピー」を実行します。

特権Gateway Intentの有効化は不要です。チャンネル単位の権限上書きでも、Botに上記3権限が許可されていることを確認してください。

## 2. Asteriskを設定する

### 必要なモジュールを確認する

`asterisk-quickstart` のディレクトリで、AudioSocketとUUID関数が利用できることを確認します。

```console
docker compose exec asterisk asterisk -rx 'module show like app_audiosocket.so'
docker compose exec asterisk asterisk -rx 'module show like func_uuid.so'
```

各コマンドでモジュールが1件表示される必要があります。表示されない場合は、使用中のAsteriskイメージがAudioSocketを含むAsterisk 18以降か確認してください。

### AudioSocket用のdialplanを追加する

`asterisk-quickstart/config/extensions/internal/discord.conf` を作成します。

```asterisk
exten => discord,1,Answer()
    same => n,Set(UUID=${UUID()})
    same => n,AudioSocket(${UUID},127.0.0.1:5000)
    same => n,Hangup()
```

`127.0.0.1:5000` は、このブリッジが待ち受けるAudioSocketのアドレスです。同一ホストで `network_mode: host` を使用する場合は変更不要です。

`asterisk-quickstart` では `default` context の内線 `160` がConfBridge会議室へ接続されます。このブリッジも次の値を前提としています。

| 項目 | 既定値 |
|---|---|
| AudioSocket用extension | `discord` |
| Conference用extension | `160` |
| Context | `default` |

異なる番号やcontextを使用する場合は、後述する `.env` の `ASTERISK_CONTEXT`、`ASTERISK_AUDIOSOCKET_EXTENSION`、`ASTERISK_CONFERENCE_EXTENSION` を変更してください。

### AMIを有効にする

ブリッジはAsterisk Manager Interface (AMI) の `Originate` actionを使用して、AudioSocket側とConfBridge側の通話を開始します。

まず、AMI用のパスワードを生成します。出力された値は、後ほど `.env` の `ASTERISK_AMI_SECRET` にも設定します。

```console
openssl rand -hex 32
```

`asterisk-quickstart/config/manager.conf` を、生成したパスワードを使って作成します。

```ini
[general]
enabled = yes
webenabled = no
port = 5038
bindaddr = 127.0.0.1

[discord]
secret = <十分に長いランダムなパスワード>
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
read = none
write = originate
```

次に、`asterisk-quickstart/compose.yaml` の `asterisk.volumes` に以下を追加します。

```yaml
- ./config/manager.conf:/etc/asterisk/manager.conf:ro
```

設定を反映するため、Asteriskコンテナを再作成します。

```console
docker compose up -d --force-recreate asterisk
```

AMIのTCPポート `5038` はインターネットへ公開しないでください。この手順では同一ホストからのみ接続できるよう、待受アドレスとAMIユーザーのACLをloopbackに制限しています。

## 3. ブリッジを設定する

本リポジトリをcloneし、環境変数ファイルを作成します。

```console
git clone https://github.com/yude/asterisk-discord-bridge.git
cd asterisk-discord-bridge
cp .env.example .env
chmod 600 .env
```

`.env` に次の値を設定します。

| 変数 | 内容 | 例 |
|---|---|---|
| `DISCORD_TOKEN` | Developer Portalで発行したBotトークン | 必須・秘密情報 |
| `GUILD_ID` | Botを追加したDiscordサーバーID | `123456789012345678` |
| `VOICE_CHANNEL_ID` | 接続先ボイスチャンネルID | `234567890123456789` |
| `ASTERISK_AMI_HOST` | AMIの接続先 | 同一ホストでは `127.0.0.1` |
| `ASTERISK_AMI_PORT` | AMIのTCPポート | `5038` |
| `ASTERISK_AMI_USER` | `manager.conf` のAMIユーザー名 | `discord` |
| `ASTERISK_AMI_SECRET` | `manager.conf` の `secret` と同じ値 | 必須・秘密情報 |
| `ASTERISK_CONTEXT` | AudioSocket側と会議室側のdialplan context | `default` |
| `ASTERISK_AUDIOSOCKET_EXTENSION` | `AudioSocket()` を実行するextension | `discord` |
| `ASTERISK_CONFERENCE_EXTENSION` | ConfBridgeへ参加するextension | `160` |
| `AUDIOSOCKET_LISTEN_HOST` | AudioSocketの待受アドレス | `0.0.0.0` |
| `AUDIOSOCKET_LISTEN_PORT` | AudioSocketの待受TCPポート | `5000` |

上記のように16進数でAMIパスワードを生成した場合、`.env` の値を引用符で囲む必要はありません。BotトークンとAMIパスワードは、README、ソースコード、Issue、ログへ貼り付けないでください。

## 4. 起動する

Asteriskが起動済みであることを確認してから、ブリッジをビルドして起動します。

```console
docker compose up -d --build
docker compose logs -f discord-bridge
```

正常に起動すると、概ね次のログが順に表示されます。

```text
Logged in as ...
AudioSocket listening on 5000
Connected to Discord (send and receive)
AMI originate accepted; waiting for AudioSocket
AudioSocket connected: ...
```

AMIのログインとOriginate応答は内部で `Response: Success` と `ActionID` を検証します。失敗時やAudioSocket切断時は指数バックオフ付きで最大5回まで再試行します。Discordの送受信処理が停止した場合も、音声接続全体を最大5回まで再作成します。上限へ到達するとサーキットブレーカーが5分間再試行を停止し、その後に新しい復旧系列を開始します。

音声キューはリアルタイム性を優先します。処理停止やネットワーク停滞で保持上限を超えた場合、古いフレームを破棄して現在の通話位置へ追いつくため、数秒前の音声を遅れて再生し続けることはありません。

Botが対象ボイスチャンネルへ参加し、Asteriskの内線 `160` へ参加した端末と双方向に音声が届くことを確認してください。

## 動作確認とトラブルシューティング

### BotがDiscordへ参加しない

* `GUILD_ID` と `VOICE_CHANNEL_ID` が正しいか確認します。
* Botが対象サーバーへGuild Installされているか確認します。
* 対象ボイスチャンネルでView Channels、Connect、Speakが許可されているか確認します。
* Botがサーバーミュートまたはサーバーdeafenされていないか確認します。

### AMI loginまたはoriginateが失敗する

* `.env` と `manager.conf` のユーザー名・パスワードが一致しているか確認します。
* Asteriskコンテナへ `manager.conf` がマウントされ、再作成済みか確認します。
* `asterisk-quickstart` のディレクトリで、Asterisk側のAMIユーザーを確認します。

```console
docker compose exec asterisk asterisk -rx 'manager show user discord'
```

設定を直した後はAsteriskを先に起動し、本リポジトリのディレクトリでブリッジも再起動してください。ブリッジはAMI応答とAudioSocket接続を確認し、失敗時は最大5回まで再試行します。

```console
docker compose restart discord-bridge
```

### AudioSocketが接続されない

* ブリッジログに `AudioSocket listening on 5000` があるか確認します。
* `asterisk-quickstart` のディレクトリで、Asterisk側のdialplanが読み込まれているか確認します。

```console
docker compose exec asterisk asterisk -rx 'dialplan show discord@default'
```

* TCPポート `5000` を他のプロセスが使用していないか確認します。
* `discord.conf` の接続先と、ブリッジを実行しているホストが一致しているか確認します。

### 片方向だけ音声が届く

* Discord→Asteriskが届かない場合は、Botがサーバー側でdeafenされていないか確認します。
* Asterisk→Discordが届かない場合は、BotのSpeak権限とAsterisk側のAudioSocket接続を確認します。
* `AMI originate accepted; waiting for AudioSocket` の後に接続されない場合は、AMI OriginateまたはAsteriskのdialplan設定を確認します。

### サーキットブレーカーが作動する

`Discord voice circuit breaker open` または `AMI circuit breaker open` が表示された場合、短周期の再接続や発呼を防ぐため自動復旧を5分間停止しています。直前に出力されたエラーとDiscord権限、AMI設定、dialplanを確認してください。原因が解消されれば次の復旧系列で自動的に再接続します。

## 開発時の検証

ロック済みの開発依存関係をインストールし、実行時処理を含むユニットテストと静的解析を実行します。

```console
uv sync --frozen --dev
uv run python -m unittest discover -s tests -v
uv run ruff check src tests
```

GitHub ActionsでもDockerイメージをビルドする前に同じ検証を実行します。

## 別ホストで動かす場合

Asteriskとブリッジを別ホストで動かす場合は、次の変更が必要です。

* `AudioSocket()` の接続先をブリッジホストのIPアドレスへ変更する
* `ASTERISK_AMI_HOST` をAsteriskホストのIPアドレスへ変更する
* `manager.conf` の `bindaddr`、`permit`、ホスト側ファイアウォールを送信元IPに限定して変更する
* TCP `5000` と `5038` の疎通を確認する

AudioSocketには認証機構がなく、AMIには通話を開始できる権限があります。どちらのポートもインターネットへ直接公開しないでください。

## プライバシー

このブリッジはDiscordボイスチャンネルの音声を電話会議へ転送します。利用前に、Discord側と電話側の参加者へ音声が相互転送されることを明示してください。

## 参考資料

* [Discord: Building your first Discord Bot](https://docs.discord.com/developers/quick-start/getting-started)
* [Discord: OAuth2 and Permissions](https://docs.discord.com/developers/platform/oauth2-and-permissions)
* [Discord: Permissions](https://docs.discord.com/developers/topics/permissions)
* [Discord: Voice Connections / DAVE protocol](https://docs.discord.com/developers/topics/voice-connections#end-to-end-encryption-dave-protocol)
* [Asterisk: AudioSocket()](https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Applications/AudioSocket/)
* [Asterisk: AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)
* [Asterisk: Manager Interface](https://docs.asterisk.org/Configuration/Interfaces/Asterisk-Manager-Interface-AMI/The-Asterisk-Manager-TCP-IP-API/)

## License

MIT
