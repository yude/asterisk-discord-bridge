# asterisk-discord-bridge

Discord のボイスチャンネルを Asterisk の Conference room に相互ブリッジします

## 設定

* Asterisk は構築済みとします。
    * Conference room の構築までは https://github.com/tkytel/asterisk-quickstart を参照してください
    * Conference room に default context の 160 番でアクセスできる前提で開発しています。必要のある場合は main.py を修正してください。
* Discord bot 向け AudioSocket 送信口としての extension を作成します。
    * tkytel/asterisk-quickstart では config/extension/internal/discord.conf として以下の内容を記述すれば動作します。
        ```
        exten => discord,1,Answer()
            same => n,Set(UUID=${UUID()})
            same => n,AudioSocket(${UUID},127.0.0.1:5000)
            same => n,Hangup()
        ```
        * `127.0.0.1:5000` は asterisk-discord-bridge によって作成される socket の受け口を指定してください
* Asterisk の Conferece room に bot を参加させるため、Asterisk CLI をこのコンテナから操作できる必要があります
    * `/etc/asterisk/manager.conf` に以下の内容を記述してください
        * asterisk-quickstart では config/manager.conf に記述し compose.yaml で /etc/asterisk/manager.conf に volume mount してください
        * 内容
            ```
            [general]
            enabled = yes
            port = 5038
            bindaddr = 0.0.0.0

            [discord]
            secret = <AMI_PASSWORD; 適宜生成>
            read = all
            write = all
            ```
        * また、上記で設定した認証情報を asterisk-discord-bridge に環境変数経由で読み込ませてください
            * `.env.example` を参照してください

## License

MIT
