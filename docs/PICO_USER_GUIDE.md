# Pico BMCUブリッジ ユーザーガイド(2基運用対応)

対象: Raspberry Pi Pico W / Pico 2 W で1〜2基のBMCUを監視・管理する利用者。
開発者向けの詳細は [`PICO_BAMBUDDY_OUTPUT.md`](PICO_BAMBUDDY_OUTPUT.md) と
[`../pico/MULTI_BMCU_SPEC.md`](../pico/MULTI_BMCU_SPEC.md) を参照。

## 1. できること

- BMCU最大2基の状態監視(スロット状態、プル率、モーション、センサ、エラーカウンタ)
- ブラウザからのローカルWeb UI(1秒周期の自動更新、リンクごとの独立表示)
- アイドル時限定のBMCUソフトリセット(リンクごと、CSRF+明示確認つき)
- Bambuddyへのテレメトリ自動送信(Pico発のWebSocket push、全リンク多重化)

読み取り専用が原則で、モーター・スロット・フィラメント操作のAPIはありません。

## 2. 必要なもの

- Raspberry Pi Pico W(RP2040)または Pico 2 W(RP2350)
- MicroPython ファームウェア(Wi-Fi対応のW用イメージ)
- BMCUごとに3本の配線(TX/RX/GND)
- 2.4 GHz Wi-Fi(PicoとBMCU設置場所に届くこと)

BMCU側のファームウェアはBMCU Link alpha.3対応版(このリポジトリのもの)を
使います。2基とも同じバージョンで構いません。

## 3. 配線

各BMCUのH1ヘッダとPicoを接続します。**3.3 V TTL・115200 8E1・全二重UART**で、
RS-485ではありません。H1-3(3.3 V)は絶対に接続しないでください。PicoはUSB給電です。

| リンクID | Pico UART | Pico TX | Pico RX | BMCU側 |
| --- | --- | --- | --- | --- |
| `bmcu-a` | UART0 | GP0 → H1-2 (EXIT_RX) | GP1 ← H1-1 (EXIT_TX) | H1-4 GND共通 |
| `bmcu-b` | UART1 | GP4 → H1-2 (EXIT_RX) | GP5 ← H1-1 (EXIT_TX) | H1-4 GND共通 |

注意:

- 1本のUARTに2基のBMCUをぶら下げることはできません(バス共有・分配器は不可)。
- 各BMCUとPicoのGNDは必ず共通にします。
- Picoのハードウェア UARTは2基までです。3基目は別のPicoを追加してください。
- H2ヘッダはプリンタ/書き込み用です。このブリッジでは使いません。

## 4. セットアップ

1. **MicroPython書き込み**(BOOTSELボタンを押しながらUSB接続):

   ```powershell
   # Pico W (RP2040)
   .\pico\flash_micropython.ps1 -Board PicoW -BootDrive D -Uf2Path <UF2のパス>
   # Pico 2 W (RP2350)
   .\pico\flash_micropython.ps1 -Board Pico2W -BootDrive D
   ```

   スクリプトはBOOTSELボリューム名を確認するため、世代違いのイメージを
   誤書き込みする事故は起きません。

2. **secrets.py作成**: `pico/secrets_example.py` を `pico/secrets.py` にコピーし、
   `WIFI_SSID` / `WIFI_PASSWORD` / `MDNS_HOSTNAME`(例: `bmcu-monitor-a`、
   Picoごとに一意)を設定します。このファイルはGit管理外です。

3. **config.py作成(任意)**: 既定で `bmcu-a`(UART0)と `bmcu-b`(UART1)の
   2リンクが有効です。ピンやリンクIDを変えたい場合だけ
   `pico/config_example.py` を `config.py` にコピーして `BMCU_LINKS` を編集します。
   1基しか繋がない場合も設定変更は不要です(未接続リンクはSTALE表示になるだけ)。

4. **アプリ転送**:

   ```powershell
   .\pico\deploy.ps1 -Port COMx -SecretsPath .\pico\secrets.py
   ```

5. Picoをリセットし、`http://<MDNS_HOSTNAME>.local/`(またはDHCPのIP)を開きます。

### USBが使えない場合の代替デプロイ

USBシリアルが認識されない個体(下記トラブルシューティング参照)でも、
次の2経路でデプロイできます。

> **注**: まず§4の通常手順を試し、駄目なときだけ以下に落ちてください。
> WebREPL経由はデプロイのたびにソフトリセットすることになり、DMAチャンネルが
> 解放されない問題(`pico/uart_dma_rx.py` §8)を踏みやすくなります。
> USBが列挙されない個体でも、接触不良が原因で後から復活する例があります。

- **WebREPL(OTA)**: デバイス側でWebREPLを有効化済みなら、
  `webrepl_cli.py -p <パスワード> <ファイル> <ホスト>:/<ファイル名>` で
  Wi-Fi経由の転送が可能です。パスワード等はローカル専用メモ
  (`pico/SECRETS_LOCAL.md`、Git管理外)を参照。
  Web UIは`.py`ではなくビルド成果物なので、モジュールとは別に送る必要があります。
  ディレクトリはWebREPLのRELP上で先に作成してください:

  ```python
  import os; os.mkdir('www')   # 既にあれば OSError になるので無視してよい
  ```

  ```powershell
  python webrepl_cli.py -p <パスワード> pico\www\index.html.gz <ホスト>:/www/index.html.gz
  ```

  転送し忘れると`/`が503を返し、本文に`tools/build_web_ui.py`を実行するよう出ます。
- **BOOTSEL直書き**: BOOTSELモードは通常のUSB故障の影響を受けません。
  PC上で `littlefs-python` を使いアプリ+`secrets.py` 入りのlittlefsイメージ
  (block 4096 / prog 256)を作成し、`picotool info -a <UF2>` で確認した
  組み込みドライブ先頭(Pico 2 W v1.28.0 では `0x10180000`)へ
  `picotool load -t bin fs.bin -o <アドレス>` で書き込みます。
  ファーム本体も同じセッションで `picotool load <UF2>` → `picotool reboot`。

## 5. Web UIの見かた

先頭にブリッジ共通の状態(Wi-Fi、Bambuddy接続、Pico稼働時間・空きヒープ・例外数)、
その下に**リンクごとのセクションが縦に並びます**。各セクションの内容:

- リンクIDと状態バッジ — `ONLINE` / `STALE`(6秒以上有効フレームなし)/
  `RESYNCING`(スナップショット取得中)/ `INCOMPATIBLE`(プロトコル不一致)
- 選択スロット、プル率、プル偏差(50%が中立)
- スロット1〜4のカード(フィラメント有無、オンライン、AMSモーション、
  コントローラ位相、モーターPWM、エンコーダ差分、センサ状態、モーション異常)
- 診断カウンタ(BMCU側TX/RXドロップ・CRC・フレームエラーと、Picoデコーダのエラー)
- 直近イベント(最新16件)
- ソフトリセットボタン

異なるBMCUの値が合成されることはありません。片方のリンクが故障・切断されても
もう片方の監視は継続します。

## 6. ソフトリセット

> **2026-08-03時点では実行できません。** コマンドはBMCU側
> (`KIND_REQUEST_SOFT_RESET`)にもPico側(`binary_control` の
> `CONTROL_SOFT_RESET`)にも実装済みですが、**それを呼び出す経路がありません**。
> かつて存在したローカルHTTPルート `POST /api/devices/<link-id>/soft-reset` は
> バイナリ transport 移行(`9e1f235`)で削除され、Bambuddy側にも
> soft resetコマンドとUIがまだありません。
>
> BMCUを再起動したい場合は電源の再投入を使ってください。RS-485ケーブルの
> 抜き差しではRAMが保持されるため、リセットにはなりません。
>
> 以下は呼び出し側が実装されたときの仕様です。安全条件そのものは
> BMCU/Pico双方で実装済みで、変わりません。

各リンクのセクションにある「Request <リンクID> soft reset」ボタンから実行します。
`RESET BMCU` と入力して確認すると、そのリンクのBMCUだけに要求が送られます。

安全条件(すべて満たさないと拒否されます):

- リンクがONLINEで、完全なフルステータスを取得済み
- 全4チャンネルの情報が揃っている
- 全チャンネルがアイドル(モーターPWM=0、モーション停止)

印刷中・フィラメント送出中は必ず拒否されます。BMCU側でも独自に再チェックされます。
実行後は `requested → scheduled → rebooted → completed` と進み、再起動後に
新しいスナップショットが揃うと完了です。

## 7. Bambuddy連携

接続は `BMB1` 永続バイナリTCPです。WebSocketではありません。`ws://` URLを
設定する箇所はもうないので、古い手順を見かけたら無視してください。

設定するのはホストとポートで、`pico/config.py` の `BMCU_BINARY_HOST` と
`BMCU_BINARY_PORT`(既定 8799)が起動時のブートストラップ値になります。
稼働後は `http://<hostname>.local/` の設定画面から差し替えられます
(内部的には `/api/transport` と `/api/device-key`)。

2基とも1本のTCPセッションに多重化され、`link_index` で区別されて送信されます。

> 2026-07時点で記録していたBambuddy側の「表示がブリッジ単位で混ざる」制限は
> **解消済み**です。サーバ側も状態を `(device_id, link_index)` で保持し、
> オンライン判定もリンクごとに独立して行うようになりました。

## 8. HTTP API(診断用・読み取り専用)

すべてBMB1メッセージの連結を返すバイナリ形式です。JSONではありません
(`/api/schema.json` だけは例外)。各レスポンスの構造は
`docs/bmcu_wire_layout.json` が正で、`/api/schema.json` はそこから生成されます。
**一度 `/api/schema.json` を取得すれば、他のエンドポイントはデバイスのソースを
読まずにデコードできます。**

| エンドポイント | 内容 |
| --- | --- |
| `GET /api/current.bin` | 全リンクの最新STATUS |
| `GET /api/history/status.bin` | `current.bin` の別名(同じハンドラ・同じ内容) |
| `GET /api/snapshot.bin` | リンクごとの詳細スナップショット |
| `GET /api/events.bin` | 永続イベントキュー(カーソル分割) |
| `GET /api/logs.bin` | Pico内部ログ(カーソル分割) |
| `GET /api/diagnostics.bin` | ブリッジ側カウンタ |
| `GET /api/capture.bin` | 破棄されたUARTランの記録 |
| `GET /api/schema.json` | 上記すべての自己記述スキーマ |

ソフトリセットのHTTPルートは存在しません。コマンド自体はBMCU・Pico双方に
実装済みですが、2026-08-03時点で呼び出す側が未実装です(§6も参照)。

HTTPは認証なしのため、LAN外に公開しないでください。

## 9. トラブルシューティング

| 症状 | 確認すること |
| --- | --- |
| リンクがずっとSTALE | TX/RX交差(Pico TX→H1-2、Pico RX←H1-1)、GND共通、BMCU電源 |
| CRC/フレームエラーが増える | 配線長・ノイズ、GND品質。片リンクだけなら配線側の問題 |
| INCOMPATIBLE表示 | BMCUファームウェアがalpha.3対応版か確認 |
| Web UIが開けない | `secrets.py` のWi-Fi設定、`/api/pico/logs` かUSBシリアルでログ確認 |
| ソフトリセットが拒否される | エラーメッセージ参照。アイドル条件(§6)を満たしているか |
| Bambuddyに届かない | `/settings` の接続状態表示、URL・トークン、サーバ起動 |
| USBシリアル(COM)が出ない | まず充電専用ケーブルを疑う(給電されるが認識されない)。BOOTSELモードで`RP2350`/`RPI-RP2`ドライブが見えるならデータ線は正常。**BOOTSELだけ動きMicroPythonでは全ビルドでCOMが出ない場合はUSB系の不良を疑う**(Wi-Fi・UARTは無事なことが多い)。§4の代替デプロイで運用可能。ただしハード不良と断ずる前に、コネクタの抜き差しと基板の清掃を試すこと(接触不良で列挙されず、後から復活する例がある) |
| 応答しないがpingは通る(TCPはacceptするがHTTPが返らない) | `main.py` が起動に失敗してREPLに落ちている。USBが使えるなら `mpremote connect COMx exec "import gc; print(gc.mem_free())"` で確認でき、空きヒープが400KB超なら本体は動いていない。`mpremote connect COMx reset` のハードリセットで復帰する。ソフトリセットでは復帰しないことがある |

片リンクの障害はそのリンクだけを止め、他のリンクやPico本体は動き続けます。
Picoの電源が落ちてもBMCU単体の動作には影響しません。
