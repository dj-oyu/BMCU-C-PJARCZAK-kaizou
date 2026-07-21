# BMCU Management Interface 論理仕様

Status: Draft / protocol v1 family  
Physical layer: [BMCU_UART_PHYSICAL_SPEC.md](BMCU_UART_PHYSICAL_SPEC.md)

## 1. 目的

H1の独立管理UARTを通じ、次をBambuddyから観測・管理できるようにする。

1. A1 miniから受信した通信
2. その通信に対するBMCUの解釈、状態変更、出力判断、返信
3. BMCUが持つセンサーの生値、解釈値、正常性
4. runout、jam、自動交換失敗を再構成できる時系列
5. 安全条件を満たす管理操作

A1 miniのバスをPicoへ電気的に中継する仕様ではない。BMCUが処理した結果を、
リアルタイム制御を妨げない固定長レコードとして独立UARTへ複製する。

## 2. 責務境界

| 要素 | 責務 |
| --- | --- |
| BMCU | プリンタ通信の受信・解釈・返信、周期制御、センサー取得、ローカル安全制約、管理レコード生成 |
| Pico 2 W | UART終端、CRC検証、受信時刻、短期バッファ、Bambuddyへの中継 |
| Bambuddy | デバイス管理、履歴、相関表示、通知、認証、運用ポリシー、プリンタAPIとの協調 |
| A1 mini | 印刷ジョブとプリンタ全体の最終状態 |

即時性と機械保護はBMCU、履歴を使う判断とUIはBambuddyが所有する。Picoは
判断主体にしない。Pico/Bambuddyの切断はBMCUの通常動作へ影響してはならない。

## 3. 必須の観測面

### 3.1 Printer transaction trace

1回のプリンタパケット処理に `transaction_id:u16` を付け、次の3段階を関連付ける。

```text
PRINTER_RX -> BMCU_DECISION -> PRINTER_TX
```

- `PRINTER_RX`: 分類、command/type、対象AMS/slot、主要引数、長さ、検証結果
- `BMCU_DECISION`: accepted/ignored/rejected、理由、適用したmotion/state、返信有無
- `PRINTER_TX`: 返信分類、長さ、返信に載せた主要状態、送信キュー結果

CRC不正など分類前に破棄したフレームは、パケットごとのraw転送ではなくエラー
カウンタとレート制限イベントで観測する。管理UARTがプリンタバスの全byteを
sniffする設計にはしない。

### 3.2 現行プリンタ要求の分類

`bambubus_package_type` と処理分岐を管理上の安定IDへ写像する。

| 要求 | wire識別 | 現行BMCU反応 | 必須観測 |
| --- | --- | --- | --- |
| motion short | short `command=0x03` | `set_motion`、状態応答生成 | slot、status/motion flag、適用後motion、pressure、返信結果 |
| motion long | short `command=0x04` | `set_motion`、詳細状態応答 | 同上、online/使用slot/距離を含む返信要約 |
| online detect | short `command=0x05` | 登録フェーズ更新、応答 | phase/subtype、登録前後、応答有無 |
| REQx6 | short `command=0x06` | 現状は分類のみ | `NO_HANDLER`を明示 |
| NFC detect | short `command=0x07` | 現状は分類のみ | `NO_HANDLER`を明示 |
| set filament | short `command=0x08` | filament情報更新、保存予約 | slot、変更field mask、保存予約結果 |
| heartbeat | short `command=0x20` | link deadline更新 | 毎回送らず、online/offline遷移とcounter |
| MC online | long `type=0x021A` | long response | address、結果、response queued |
| read filament | long `type=0x0211` | filament情報応答 | slot、response queued |
| set filament v2 | long `type=0x0218` | 情報更新、保存予約 | slot、変更field mask、結果 |
| version | long `type=0x0103` | version応答 | request/response correlation |
| serial number | long `type=0x0402` | serial応答 | 値そのものは通常ログで伏せ、応答結果のみ |
| unknown/ETC | その他 | 無処理 | command/type、length、`UNSUPPORTED` |

「受信した」だけでは不十分である。対象AMS違い、offline、送信キューbusy、引数不正、
handlerなし等をdecision reasonとして必ず区別する。

### 3.3 センサー情報

各値は `value` だけでなく `validity` と `age` を持つ。派生値だけでなく、原因調査に
必要な範囲で量子化したraw値も公開する。

| 系統 | チャネルごとの必須項目 |
| --- | --- |
| Pull/pressure ADC | filtered raw、補正後値、percent、min/max/offset、polarity、valid |
| DM/online key | raw、解釈state、none threshold、挿入検知、loaded、fail latch |
| AS5600 | online、magnet state、raw angle、差分移動量、累積meters、good/fail streak |
| Motor control | requested motion、internal phase、PWM command、direction、timeout/jam/low latch |
| Auto load/unload | state、armed/active/blocked、try count、経過時間、終了理由 |
| AMS aggregate | selected slot、use flag、pressure、online、control error |

ADCの物理チャネル割当や単位が未確定な項目は推測せず、`raw`と`unit=UNSPECIFIED`
から始める。内部 `static` 値は管理モジュールから直接参照せず、Motion control側に
一貫したsnapshot APIを設けて一度だけコピーする。

## 4. 状態モデル

### 4.1 管理リンク

| 状態 | 条件 |
| --- | --- |
| `ABSENT` | Pico自体が未接続 |
| `DISCOVERING` | Pico接続済み、正常BMCUフレームを未受信 |
| `ONLINE` | 直近3秒以内に正常フレームを受信 |
| `STALE` | 3秒を超えて正常フレームなし |
| `INCOMPATIBLE` | 対応不能なversionまたは必須capability不足 |
| `FAULT` | 連続破損等で信頼不能 |

接続検出はHELLO/STATUSだけで行う。H1/H2の自動切替や稼働中の配線変更は行わない。

### 4.2 動作状態

- printer bus: `UNKNOWN / OFFLINE / IDLE / ACTIVE / DEGRADED`
- slot: `EMPTY / PRESENT / AVAILABLE / SELECTED / FAULT`
- filament path: `UNKNOWN / UNLOADED / LOADING / LOADED / UNLOADING / JAMMED`
- auto swap: `DISABLED / ARMED / DETECTING_END / RETRACTING / SELECTING / LOADING / VERIFYING / COMPLETE / FAILED`
- anomaly: `CLEAR / SUSPECT / CONFIRMED / LATCHED / ACKNOWLEDGED`

確定できない値は推測せず `UNKNOWN` と観測根拠を返す。

## 5. 安全クラス

| class | 内容 | 初期状態 |
| --- | --- | --- |
| `S0` | status、transaction trace、sensor snapshot | 許可 |
| `S1` | LED等、動作に影響しない期限付き操作 | 許可 |
| `S2` | 異常ラッチ確認等の管理状態変更 | 禁止 |
| `S3` | load/unload、slot選択、motor操作 | 禁止 |
| `S4` | ローカル駆動禁止、printer pause/stop連携 | 別設計 |

S3/S4は単純な1フレーム命令にしない。明示的enable、状態事前条件、operation ID、
reason、短いTTL、重複排除、結果イベント、通信断時の安全動作が必要である。
BMCUのローカル停止とBambuddyからA1 miniへのpause/stopは別々に確認する。

## 6. 共通ワイヤ形式

- 115200 bps、8E1
- 同期ヘッダ `0xA5 0x5A`
- body最大64 byte、payload最大57 byte
- little-endian
- CRC-16/CCITT-FALSE (`init=0xFFFF`, `poly=0x1021`)

```text
version:u8 | kind:u8 | sequence:u16 | payload_length:u8 | payload | crc16:u16
```

UART全体ではなく、**kindごとのpayloadを固定長**にする。commandは引数を持ってよいが、
kindごとに型・長さ・範囲を固定する。可変文字列、JSON、任意TLVはBMCUに入れない。

- alpha.3のheader/kindと全enum値をwire ABIとして固定する。値は追加のみとし、既存値を再採番しない。
- 応答は要求と同じsequenceを返す。
- 自発通知はBMCU側連番を使う。
- 互換追加はcapabilityと新kind、非互換変更だけversionを上げる。

## 7. Message registry

### 7.1 実装済み

| kind | 方向 | 長さ | payload |
| --- | --- | ---: | --- |
| `0x01 HELLO` | BMCU→Pico | 9 | `protocol:u8, capabilities:u16, fw_major:u8, fw_minor:u8, tick_hz:u32` |
| `0x02 STATUS` | BMCU→Pico | 27 | §7.2 |
| `0x10 GET_STATUS` | Pico→BMCU | 0 | 即時STATUS要求 |
| `0x11 SET_LED_MODE` | Pico→BMCU | 3 | `mode:u8, timeout_s:u16` |
| `0x12 PING` | Pico→BMCU | 4 | `token:u32` |
| `0x17 GET_FULL_STATUS` | Pico→BMCU | 2 | `section_mask:u8, channel_mask:u8` |
| `0x18 REQUEST_SOFT_RESET` | Pico→BMCU | 8 | `operation_id:u32, reason:u8, flags:u8, ttl_ms:u16` |
| `0x72 PONG` | BMCU→Pico | 8 | `token:u32, hw_tick32:u32` |
| `0x7F ACK` | BMCU→Pico | 2 | `request_kind:u8, result:u8` |

### 7.2 Soft reset safety contract

`REQUEST_SOFT_RESET` is implemented as an idle-only S2 recovery operation.
Version 1 has no force flag and accepts only a local CSRF-confirmed Pico request.
Bambuddy-originated control remains disabled until a separate authenticated
control scope is available.

BMCU is authoritative for motor/calibration/printer-bus quiescence and rechecks it
after `ACK_OK`. Pico additionally requires an online link, a complete Full Status,
all controller phases stopped, all AMS motions idle, and all PWM values zero.
Reset completion requires a new HELLO/boot session and a new complete snapshot.
Hardware validation must cover every refusal path and ACK-before-reset ordering.

### 7.3 STATUS v2（27 byte）

| offset | 型 | 名前 |
| ---: | --- | --- |
| 0 | u32 | hw_tick32（SysTick生カウンタ、wrapあり） |
| 4,6,8,10 | u16 x4 | tx_drop、rx_drop、crc_error、frame_error |
| 12 | u8 | current_slot (`0xFF`=none/unknown) |
| 13,14 | u8 | inserted_mask、online_mask |
| 15 | u8[4] | filament motion |
| 19 | u8[4] | pull_pct |
| 23 | u16 | pressure |
| 25,26 | u8 | led_mode、control_error |

v2のoffsetを固定し、詳細情報は新messageで追加する。`hw_tick32`の差分は`(new-old)&0xffffffff`で求め、`tick_hz`で秒へ換算する。壁時計と受信時刻はPico/Bambuddyが付与する。

### 7.4 必須追加message

| kind案 | 名前 | 方向 | 固定payload |
| --- | --- | --- | --- |
| `0x03` | `EVENT` | BMCU→Pico | `LogRecord`（共通header＋種別別union payload） |
| `0x04` | `PRINTER_TRANSACTION` | BMCU→Pico | command、owner、outcome、reason、request/response length |
| `0x05` | `SENSOR_RECORD` | BMCU→Pico | sensor、slot、validity、value format、value |
| `0x06` | `BUS_STATUS` | BMCU→Pico | online、last class、rx/valid/error/tx counters、last age |
| `0x07` | `SENSOR_GLOBAL` | BMCU→Pico | hw_tick32、selected slot、pressure、masks、aggregate flags |
| `0x08` | `SENSOR_CHANNEL` | BMCU→Pico | channel、sample id、raw/derived sensors、motion/PWM/latches |
| `0x09` | `ANOMALY_EVENT` | BMCU→Pico | event、severity、channel、hw_tick32、values、flags |
| `0x12` | `PING` | Pico→BMCU | `token:u32` |
| `0x15` | `GET_DEVICE_INFO` | Pico→BMCU | none |
| `0x16` | `GET_BUS_STATUS` | Pico→BMCU | none |
| `0x17` | `GET_FULL_STATUS` | Pico→BMCU | implemented above; this supersedes the earlier sensor-snapshot candidate |
| `0x72` | `PONG` | BMCU→Pico | `token:u32, hw_tick32:u32` |
| `0x7E` | `DEVICE_INFO` | BMCU→Pico | protocol range、capabilities、fw/hw/build id |

旧 `GET_SENSOR_SNAPSHOT` 案の要件は `GET_FULL_STATUS` のGLOBAL/CHANNEL固定長レコードへ統合済みである。
全チャネルを1つの巨大payloadへ詰めず、同一snapshot IDの固定長レコードへ分割する。

### 7.5 Binary log record

BMCUはログ文字列を生成しない。レコードは16 byte固定で、8 byteの共通headerと
8 byteの`union` payloadからなる。

```text
LogRecordHeader = hw_tick32:u32 | type:u8 | severity:u8 | source:u8 | payload_length:u8
LogRecord       = LogRecordHeader | union payload[8]
```

`type`はBOOT、PRINTER_LINK、PRINTER_TRANSACTION、STATE_CHANGE、SENSOR、
COMMAND_RESULT、SAFETY_DECISION、DIAGNOSTIC_COUNTERを数値enumで表す。payloadは
種別ごとの構造体として最小化し、意味のある先頭長を`payload_length`に入れる。
command owner、outcome、reason、ACK result、severity、source、sensor validityも
固定enumとする。ABIサイズはfirmwareの`static_assert`で検証する。

文字列名、JSON化、時刻整形、単位換算、メッセージ連結はPico/Bambuddy側のenum
registryで行う。BMCU側のhot pathでは構造体への整数代入とキューへの固定長コピーだけを行う。

### 7.6 Decision outcome/reason

outcomeは `ACCEPTED / APPLIED / REPLIED / IGNORED / REJECTED / FAILED`。
reasonは少なくとも `OK / TARGET_MISMATCH / SLOT_RANGE / AMS_OFFLINE /
INVALID_LENGTH / INVALID_CRC / UNSUPPORTED / NO_HANDLER / TX_BUSY / BAD_STATE /
SAFETY_INHIBIT / INTERNAL / NO_RESPONSE / NO_RESPONSE_EXPECTED` を持つ。`TX_BUSY`は先行応答がまだqueueに残る場合だけに用い、応答必須handlerが応答を生成しなかった場合は`FAILED / NO_RESPONSE`とする。handlerがonline detect登録済みなどの正常な無応答を明示した場合は`IGNORED / NO_RESPONSE_EXPECTED`として別counterへ記録する。非同期のDMA TE/timeoutはtransaction IDでTX完了まで相関できるまではcommand reasonへ帰属させず、PRINTER_TX_FAULT counterで報告する。

ACK resultの既存値 `0=OK, 1=BAD_VALUE, 2=UNSUPPORTED` は固定し、`BUSY、BAD_STATE、
DENIED、EXPIRED、DUPLICATE、INTERNAL` を後方互換で追加する。

## 8. 計測点と性能

Printer traceはISRで構築しない。既存parser/handlerの入口と出口で小さい観測構造体へ
値をコピーし、管理TXキューへ積む。printer raw packet全体の複製は禁止する。

- RX IRQ: 管理byteをringへ置くだけ
- state producerは値が意味的に変化した時、global dirty maskへreason bitをcallbackする
- management serviceはdirty=0ならsnapshotを構築せず、1回のvolatile readでfast pathへ戻る
- dirty時だけ既存global stateからSTATUS snapshotを1回構築し、成功時にreasonを消費する
- enqueue失敗時はreasonをglobal dirty maskへ戻し、状態通知を失わない
- management TX: DMA、動的メモリなし、送信待ちなし
- HELLOは起動時に1回だけ送信する
- STATUSは意味的状態変化時と明示要求時だけ送信する
- PING/PONGはPico起点の死活監視とし、推奨2秒周期、6秒無応答でSTALEとする
- sensor snapshotは要求時または期限付き低頻度とする
- heartbeatは毎回event化せずcounterとlink transitionを送る
- trace burst時は低優先STATUSを先にdropする
- trace自体が多すぎる場合はsample modeを使うが、motion commandと異常は省略しない
- 反復障害は飽和counterを正本とし、同一EVENTの連続生成でmanagement linkを圧迫しない
- printer TXのTE/timeoutは共通復旧経路でDMA停止、flag clear、DE受信復帰を行い、full snapshot counterへ記録する
- queue drop数とtransaction/event sequence gapを必ず公開する

現行TX queueは8 slotである。容量拡大より先にpriority/reserved slotと実測を行う。
周期送信による診断LEDは通常LEDを覆うため、開発ビルド限定へ分離する。

## 9. 異常検知

`observation -> suspect -> confirmed -> action -> outcome` を全て記録する。一度の閾値
超過だけで停止せず、時間窓、ヒステリシス、複数信号を使う。

例: motor command/PWMあり + AS5600移動なし + pull/pressure上昇をjam候補とする。
runoutはprinter motion要求、online key、pull sensor、AS5600移動、selected slotを
同じ時系列で評価する。自動交換失敗は要求、BMCU decision、sensor snapshot、返信を
transaction IDとsample IDで結び、どの層で期待から外れたか判定する。

## 10. Pico/Bambuddy契約

Picoは正常frameを型付きenvelopeへ変換する。BMCUの`hw_tick32`とPico単調時刻を両方
保持し、HELLOの`tick_hz`で経過時間を換算する。正規のenvelope、再送、および転送
契約は [`PICO_BAMBUDDY_ENVELOPE.md`](PICO_BAMBUDDY_ENVELOPE.md) とする。

Bambuddyは `(device_id, pico_boot_session, bmcu_boot_session, sequence)` で重複排除し、
transaction IDでRX/decision/TXを1つの操作として表示する。UARTはローカル信頼境界、
認証・認可・監査はPico/Bambuddyのネットワーク境界で行う。

## 11. 実装順序

1. **完了**: H1全二重、同期ヘッダ/CRC、DMA TX、RX IRQ、HELLO/STATUS、LED command。
2. printer transaction observerとBUS_STATUSを追加する。
3. side-effectなしのsensor snapshot APIとGLOBAL/CHANNEL messageを追加する。
4. motion、runout、jam、auto unload/swapの状態遷移eventを追加する。
5. Pico gatewayとBambuddy optional device adapterを実装する。
6. 実機ログから検知規則・閾値・誤検知率を評価する。
7. hazard reviewとfault injection後にS2、S3/S4を個別に有効化する。

## 12. 受入条件

- printer request、BMCU decision、printer replyを同一transactionとして追跡できる。
- 全4チャネルの必須sensor値、validity、age、内部解釈を取得できる。
- 対象違い・unsupported・handlerなし・TX busyを「無反応」と区別できる。
- Pico未接続、再起動、管理RX連投でもprinter bus/motor周期が悪化しない。
- frame欠落、BMCU再起動、Pico再起動をBambuddyが区別できる。
- S3/S4無効時は管理frameでmotor状態が変わらない。
- 異常停止有効化前に正常時の誤検知率と異常時の検出時間を記録する。

## 13. 未決定事項

- 各追加messageの最終byte layoutとcapability bit
- ADC物理チャネルの名称・単位・公開するraw量子化
- internal motion/auto-load/unload stateの安定enumへの写像
- Bambuddy forkでの認証方式とoptional-device ingest URL
- runout/jam/swap failureの閾値と時間窓
- S4でBMCUが禁止できる出力範囲とA1 mini pause確認方法

