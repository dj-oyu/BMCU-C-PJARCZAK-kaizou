# BMCU UART3 / Pico 2 W / Bambuddy 連携設計（Phase 1）

## 1. 目的と原則

UART3 に接続した Raspberry Pi Pico 2 W を、BMCU の制御に依存しない
**Bambuddy 用オプションデバイス**として公開する。

Phase 1 の目的は、BMCU の観測可能性を上げることと、往復通信の安全な
土台を作ることである。最初に許可する書込み操作は、印刷・モーター・
フィラメント状態に影響しない LED イルミネーションモードだけに限定する。

原則は次の通り。

- A1 mini との UART1 通信、モーター制御、ADC 処理は最優先であり、UART3
  の送受信・ログ処理が待機や再試行で妨げてはならない。
- BMCU はローカルで安全に動作し続ける。Pico、Wi-Fi、Bambuddy の停止や
  切断を、BMCU の制御失敗に結び付けない。
- Pico は BMCU の制御主体ではなく、UART とネットワークの境界である。
  Bambuddy が履歴、UI、通知、将来の運用ポリシーを所有する。
- UART3 のログは人間向け `printf` ではなく、固定長に近いバイナリイベントとする。

## 2. 全体構成

```text
A1 mini <-- 1.25 Mbps half-duplex --> BMCU <-- UART3 --> Pico 2 W --Wi-Fi--> Bambuddy
                                       |                 |                    ^
                                       |                 +-- diagnostic HTTP  |
                                       +-- BMCU Link     +-- outbound WebSocket+
```

- BMCU は UART1 で A1 mini の既存プロトコルを継続して処理する。
- BMCU Link は UART3 上のローカル専用プロトコルである。A1 mini のバスを
  中継・改変しない。
- Pico はネットワーク側で `bmcu-monitor` というオプションデバイスとして
  Bambuddy に登録される。Bambuddy が利用できないときも Pico は BMCU を
  直接制御しない。

## 3. 物理・UART 設定

確定したH1/H2のピン配置、書き込み配線、電源上の注意、実機検証記録は
[docs/BMCU_UART_PHYSICAL_SPEC.md](docs/BMCU_UART_PHYSICAL_SPEC.md)を正とする。

| 項目 | 初期値 |
| --- | --- |
| BMCU UART | USART3: TX=PB10, RX=PB11 |
| Pico UART | UART0: GP1=RX, GP0=TX（配線確定時に変更可） |
| 電気 | 3.3 V TTL、GND 共通。PicoをUSB給電する場合はH1の3.3 Vを接続しない |
| 通信 | 115200 bps, 8E1（オリジナルUSART3デバッグ設定を維持） |
| 配線 | BMCU TX→Pico RX、Pico TX→BMCU RX、GND→GND |

115200 bps でもイベントと低頻度の状態報告には十分である。将来、十分な
実機試験後に 460800/921600 bps へ上げられるが、Phase 1 の前提にしない。

## 4. BMCU Link プロトコル

### 4.1 フレーミング

UART の1フレームは同期ヘッダ `0xA5 0x5A` で開始し、続けて次のbodyを送る。

```text
A5 5A | version:u8 | kind:u8 | sequence:u16 | payload_length:u8 | payload | crc16:u16
```

- CRC は `version` から payload 末尾までを対象にし、同期ヘッダは含めない。
- body最大長は64 byte、payload最大長は57 byte。長さ不正・CRC不正は破棄し、次の同期ヘッダを探索する。
- wire長は `payload + 9 byte` で、旧COBS形式と同じオーバーヘッドである。
- `sequence` は送信元ごとの連番。応答は要求の `sequence` を返す。
- ACK を要求するコマンドだけが応答を持つ。イベントに対する ACK や再送はしない。
### 4.2 Phase 1 のメッセージ

| kind | 方向 | 内容 |
| --- | --- | --- |
| `0x01 HELLO` | BMCU→Pico | protocol alpha.3 (`0x83`)、機能ビット、ファームウェア版、`tick_hz` |
| `0x02 STATUS` | BMCU→Pico | 意味的状態変化時と明示要求時の集約状態 |
| `0x03 EVENT` | BMCU→Pico | 状態遷移、A1要求の分類、センサー異常など |
| `0x10 GET_STATUS` | Pico→BMCU | 即時の `STATUS` を要求 |
| `0x11 SET_LED_MODE` | Pico→BMCU | LED表示モードのみを変更 |
| `0x12 PING` | Pico→BMCU | token付き死活確認 |
| `0x72 PONG` | BMCU→Pico | tokenと`hw_tick32`を返す |
| `0x7F ACK` | BMCU→Pico | 成功、拒否、不正パラメータ、busy を返す |

`SET_LED_MODE` の payload は `mode:u8 | timeout_s:u16` とする。受理値は
既存の安全なLEDモードだけで、`timeout_s=0` は通常表示へ即時復帰とする。
LEDの上書きはタイムアウト後に必ず解除する。モーターPWM、AMS状態、
A1 mini バス、保存領域を触るコマンドは Phase 1 に加えない。

### 4.3 STATUS / EVENT の最小項目

`STATUS` は意味的状態変化時と明示要求時、`EVENT` は状態変化時だけ送る。
連続的なsensor値だけではSTATUSを発火させず、詳細値はsnapshotで取得する。

- `hw_tick32`、BMCU Link TX/RXのドロップ・CRCエラー数
- A1通信の直近パケット分類とエラー数
- 選択スロット、`loaded`、フィラメントモーション状態
- 各チャネルの `MC_PULL_pct`、ONLINEキー、AS5600正常性（量子化して送信）
- 自動引抜の arm/active/終了理由
- LED通常モードと一時上書き状態

全イベントは数値IDで送る。ログは共通header＋種別別8 byte `union` payloadの16 byte固定レコードとし、文字列連結、フォーマット、JSON化、時刻換算は Pico/Bambuddy 側で行う。

## 5. BMCU 実装と性能要件

既存 `Debug_log` は送信完了まで待機するため、BMCU Link には使用しない。
別モジュール `bmcu_link.{h,cpp}` を置く。

### 5.1 送信経路

1. 制御ループの安全な観測点だけが、固定長イベントを SPSC TXリングへ書く。
2. リング容量は 8 slot、1フレームは最大66 byteとする。動的メモリ、`printf`、
   浮動小数点整形、ロックを使わない。
3. UART3 TX はDMAで送る。DMA送信中は次のフレームをリングに積むだけで、
   制御ループは送信完了を待たない。
4. リング満杯時は、低優先度の STATUS を捨てる。重要EVENTは優先枠を使い、
   それでも満杯なら捨てて `tx_drop_count` を次のSTATUSに載せる。

送信開始・DMA完了の処理は短いIRQ処理に限定する。A1 mini通信の UART1 IRQ、
ADC/DMA、モーター周期処理からログ生成・フレーム化を呼ばない。

### 5.2 受信経路

- USART3 RX IRQ は受信バイトを固定長RXリングへ格納するだけにする。
- 同期ヘッダ解析、CRC検証、コマンド実行判定はメインループ末尾で、1周につき
  最大1フレームだけ処理する。
- 不正フレームには応答せず捨てる。正規コマンドへのACKだけをTXリングへ積む。
- LEDモード変更は `RGB_update()` と競合しない共有状態へ代入し、次回の通常
  更新で反映する。フラッシュ書込み・delay・モーター操作は行わない。

### 5.3 測定可能な受入基準

- UART3を未接続、連続STATUS、異常フレーム連投の各条件で、UART1パケット
  エラーとモーター制御周期に有意な悪化がない。
- 送信リング満杯時も BMCU は停止・ブロックしない。
- Picoを再起動・抜線しても、A1 miniとの印刷・既存LED表示が継続する。
- `SET_LED_MODE` は100 ms以内にACKし、指定タイムアウトまたは再起動後に
  必ず通常表示へ戻る。

## 6. Pico 2 W の役割

PicoはUARTを読み、BMCU Linkフレームを検証してからネットワークに中継する。
BMCUが遅延なく動作できるよう、Wi-Fi接続・HTTP・保存処理が遅くてもUART受信を
止めない。Pico側にも固定長の受信キューと、Bambuddy未接続時の小さな循環イベント
バッファを置く。

Picoは独自の常駐ダッシュボードや通知主体にはしない。ネットワーク公開面は
「Bambuddyが発見して採用するオプションデバイス」のための最小限に留める。

### 6.1 Bambuddy オプションデバイス契約

Bambuddyの実際の拡張APIに合わせる薄いアダプタをRDK-X5側に実装する。現時点で
そのAPI仕様がこのリポジトリにないため、以下は実装すべき契約であり、URLや認証方式
はBambuddyの既存方式に合わせて確定する。

- 診断用発見: Picoは mDNS でホスト名を公開できるが、用途はcommissioningと
  browser診断に限定する。Bambuddyのproduction ingestはmDNSでPicoを発見して
  HTTP pollingする方式にしない。
- 識別: `device_id`、機種=`bmcu-monitor`、プロトコル版、BMCUファーム版、
  対応機能を返す。
- 接続: PicoがBambuddyへ認証済みの単一永続WebSocketをoutboundで開く。WebSocketが
  使えない場合だけbatched NDJSON POSTを使う。PicoはBMCUイベントを規定envelopeへ変換する。
- 制御: 現在のproduction scopeは観測とLED feedbackだけである。Bambuddy UIの `LED mode`
  操作を有効にするには、別途認証済みcommand contractが必要である。
- 所有: 履歴、グラフ、通知、認証、ユーザー権限、再接続はRDK-X5/Bambuddyが扱う。Picoは
  inbound Bambuddy接続を受け付けず、診断用read-only HTTPだけを公開する。

接続状態、ACK/replay、backpressure、および診断HTTPとの分離は
[`docs/PICO_BAMBUDDY_TRANSPORT.md`](docs/PICO_BAMBUDDY_TRANSPORT.md)を正とする。

PicoがBambuddyと接続できない時は、BMCUイベントを短期バッファに保持するだけで、
自動制御判断や状態広告を独自に行わない。

## 7. 段階導入

1. BMCU Linkのリング、同期ヘッダ/CRC、HELLO/STATUSと受信メトリクスを実装する。
2. PicoでUARTフレームを受信し、USBシリアルにデコード表示して配線と安定性を検証する。
3. `SET_LED_MODE` とACKを実装し、双方向通信の安全性を検証する。
4. PicoのmDNSとBambuddyオプションデバイス・アダプタを接続する。
5. スプール交換に関係するセンサー・状態遷移EVENTを追加し、失敗再現の時系列を収集する。
6. ログの根拠を得た後にだけ、停止・自動交換許可などの安全機能を別Phaseで設計する。

## 8. 未確定事項


- Bambuddyのオプションデバイス登録・認証・状態更新の実際のAPI。
- LEDの既存モード一覧と、通常表示を壊さない一時上書きの実装位置。
- PicoのWi-Fi認証情報の安全な初期プロビジョニング方式。

