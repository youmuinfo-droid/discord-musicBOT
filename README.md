# NEXORA-musicBOT

**NEXORA-musicBOT** は、Discord サーバー向けの音楽再生、自動TTS、AIサポート、サーバー管理機能を備えたBotです。  
Python、discord.py、yt-dlp、FFmpeg、edge-tts を使用しています。

- Author: **yomu.(h1n4)**
- Prefix: `n!`
- Slash Command: `/`

## 主な機能

- YouTube、SoundCloud、TikTok、Spotify URL、その他対応URLからの音楽再生
- SoundCloud / YouTube の検索と候補選択
- Music Panel による再生状況・キューの表示
- キュー、ループ、シャッフル、音量、再生速度の制御
- `edge-tts` を使用したVC読み上げ
- テキストチャンネル単位の自動TTS
- VC入退室通知の読み上げ
- Groq API を使用したAIサポート
- サーバーごとの再生履歴、お気に入り、キュー保存・復元
- 睡眠タイマー、DJモード、リクエスト制限
- Botステータス、Ping、CPU、RAM、ストレージ空き容量の確認
- 削除されたメッセージのログ機能

## 必要環境

- Python 3.10 以上
- FFmpeg
- Discord Bot Token
- Groq API Key
- インターネット接続

## セットアップ

```bash
git clone [https://github.com/yomu-h1n4/NEXORA-musicBOT.git](https://github.com/yomu-h1n4/NEXORA-musicBOT.git)
cd NEXORA-musicBOT
pip install -r requirements.txt
python main.py
```

`main.py` 内または `.env` に、以下の情報を設定してください。

```env
DISCORD_TOKEN=あなたのDiscordBotToken
GROQ_API_KEY=あなたのGroqAPIKey
```

AIチャット機能を使用しない場合でも、音楽再生とTTSは利用できます。

## Discord Bot の設定

1. Discord Developer Portal でApplicationを作成する
2. BotページでTokenを取得する
3. Privileged Gateway Intents の Message Content Intent を有効にする
4. Botをサーバーに招待する
5. 以下の権限を付与する
   - ボイスチャンネルに接続
   - ボイスチャンネルで発言
   - メッセージを送信
   - Embedリンクを送信
   - 必要に応じてチャンネルの管理

## 音楽コマンド

| コマンド | 説明 |
|---|---|
| `n!p <URLまたは曲名>` | 音楽を再生またはキューに追加 |
| `n!p panel` | Music Panelを表示 |
| `/play <URLまたは曲名>` | 音楽を再生またはキューに追加 |
| `/source <service> <query>` | SoundCloudまたはYouTubeから検索して再生 |
| `/sources` | 対応している音源サービスを表示 |
| `/search <曲名>` | SoundCloudの検索候補を最大10件表示 |
| `n!q` | 現在のキューを表示 |
| `/queue` | 現在のキューを表示 |
| `n!np` | Music Panelを表示 |
| `/nowplaying` | Music Panelを表示 |
| `n!s` | 現在の曲をスキップ |
| `/skip` | 現在の曲をスキップ |
| `n!pause` | 再生を一時停止 |
| `/pause` | 再生を一時停止 |
| `n!r` | 再生を再開 |
| `/resume` | 再生を再開 |
| `n!v <0〜600>` | 音量を変更 |
| `/volume <0〜600>` | 音量を変更 |
| `n!speed <0.5〜2.0>` | 再生速度を変更 |
| `n!loop <off / track / queue>` | ループ設定を変更 |
| `/loop <OFF / 1曲 / キュー>` | ループ設定を変更 |
| `n!rm <番号>` | キューから曲を削除 |
| `/remove <番号>` | キューから曲を削除 |
| `n!clear` | キューを全消去 |
| `/clear` | キューを全消去 |
| `n!sh` | キューをシャッフル |
| `/shuffle` | キューをシャッフル |
| `n!j` | BotをVCに接続 |
| `/join` | BotをVCに接続 |
| `n!b` | BotをVCから退出 |
| `/leave` | BotをVCから退出 |
| `n!stop` | 再生とキューを停止 |
| `/stop` | 再生とキューを停止 |

## TTSコマンド

| コマンド | 説明 |
|---|---|
| `n!tts on / off` | サーバーのTTS機能をON / OFF |
| `/tts on / off` | サーバーのTTS機能をON / OFF |
| `n!y` | 現在のテキストチャンネルの自動TTSをON / OFF |
| `n!yp` | TTS音声を選択 |
| `n!say <テキスト>` | 指定したテキストをVCで読み上げ |
| `/say <テキスト>` | 指定したテキストをVCで読み上げ |
| `n!vcnotify on / off` | VC入退室通知を読み上げるか設定 |
| `/vcnotify on / off` | VC入退室通知を読み上げるか設定 |
| `n!o` | BotのVC入室読み上げをON / OFF |
| `n!ttsstatus` | TTSの現在設定を表示 |

## AIコマンド

| コマンド | 説明 |
|---|---|
| `@Bot <検索したい曲ワード>` | BotにメンションしてAIに質問する |

AI機能には `GROQ_API_KEY` が必要です。

## 管理コマンド

| コマンド | 説明 |
|---|---|
| `n!dj on / off` | DJモードをON / OFF |
| `/dj on / off` | DJモードをON / OFF |
| `n!djmode on / off` | DJモードをON / OFF |
| `n!requestlimit <1〜50>` | 60秒あたりのユーザーごとのリクエスト上限を設定 |
| `n!sleep <分数>` | 指定時間後に再生を停止してVCから退出 |
| `n!sleep off` | 睡眠タイマーを解除 |
| `n!status` | BotのPing、CPU、RAM、ストレージ、再生状況を表示 |
| `n!ping` | Botの応答速度を表示 |
| `n!synccommands` | Slash Commandを再同期 |
| `n!panelreset` | Music Panelを再作成 |
| `/voice <メッセージ>` | VC入室時の通知メッセージを変更 |

## 対応音源

- YouTube
- SoundCloud
- TikTok
- Spotify URL
- Bandcamp
- Mixcloud
- Vimeo
- Dailymotion
- yt-dlpが対応するその他のURL
- ローカルの音声・動画ファイル
- .kt3.ktc 対応
- Instagramには対応していません

Spotify URLは、公開メタデータを取得したうえでSoundCloudやYouTubeの公開音源を検索する方式です。

## 注意事項

- `.env`、Bot Token、API Key、CookieファイルはGitHubに公開しないでください
- `.gitignore` に機密ファイルを必ず含めてください
- YouTubeおよび各音源サービスの利用規約に従って使用してください
- 本Botは学習および個人利用を想定しています
- 音楽再生にはFFmpegが必要です

## ライセンス

MIT License

## 作者

- **yomu.(h1n4)**
