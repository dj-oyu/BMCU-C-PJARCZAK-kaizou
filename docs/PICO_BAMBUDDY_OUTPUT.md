# Pico 2 WがBambuddyへ提供できる情報

Status: BMCU Link alpha.3 (`0x83`) の現行実装に基づく一覧

この文書は、`pico/bmcu_link.py` がBMCUのバイナリ通信を検証・デコードした後、
Bambuddyへ提供できる情報をまとめる。wire上の正規仕様は
[`BMCU_LINK_PROTOCOL_ALPHA3.md`](BMCU_LINK_PROTOCOL_ALPHA3.md)を参照する。

## 1. 現在の出力経路

| 経路 | 状態 | 内容 | 用途 |
| --- | --- | --- | --- |
| `GET http://<Pico-IP>/api/status` | 実装済み・read-only | 現在状態、最新full snapshot、直近イベント、診断値を1つのJSONで返す | Bambuddy試作・診断 |
| `publish(message)` callback | 型付き辞書まで実装済み、外部transportは未実装 | 受信フレームとPicoのlink/Wi-Fi状態変化 | 将来の認証付きoptional-device transport |
| USB JSONL | `DEBUG_USB=True`の時だけ | `publish(message)`を1行JSONで出力 | commissioning専用 |

HTTP serverは認証・TLSなし、同時に1接続だけを処理する暫定APIである。信頼できる
LAN外には公開しない。USB出力はUART処理を遅延させ得るためproduction transportには使わない。

## 2. `/api/status` JSON

```json
{
  "wifi": {"state": "online", "ip": "192.168.x.x"},
  "bmcu": {
    "link": "online",
    "tick_hz": 18000000,
    "status": {},
    "snapshot": [],
    "channels": [{}, {}, {}, {}],
    "events": [],
    "sensors": {},
    "decoder_crc_errors": 0,
    "decoder_frame_errors": 0
  }
}
```

未受信値は`null`、byte列は小文字hex文字列になる。

### 2.1 Pico・link情報

| JSON path | 型 | 意味 |
| --- | --- | --- |
| `wifi.state` | string | Pico Wi-Fi状態（`idle`、`connecting`、`online`など） |
| `wifi.ip` | string/null | Pico IPv4 address |
| `bmcu.link` | string | 有効なBMCU frameを6秒以内に受信していれば`online`、それ以外は`stale` |
| `bmcu.tick_hz` | integer/null | HELLOで通知されたhardware counter周波数。現状18 MHz |
| `bmcu.decoder_crc_errors` | integer | Pico decoderがCRC不一致で破棄したframe数 |
| `bmcu.decoder_frame_errors` | integer | Pico decoderが不正長で破棄したframe数 |

Pico decoder errorと、後述するBMCU management counterは別の観測値である。

### 2.2 BMCU集約状態 `bmcu.status`

| field | 型 | 意味 |
| --- | --- | --- |
| `hw_tick32` | u32 | wrapするBMCU hardware counter |
| `tx_drop` | u16 | management TX drop（飽和・切り詰め表示） |
| `rx_drop` | u16 | management RX drop（飽和・切り詰め表示） |
| `crc_error` | u16 | BMCU management RX CRC error |
| `frame_error` | u16 | BMCU management RX frame/length error |
| `current_slot` | u8 | 0-based channel。`255`はnone/unknown |
| `inserted_mask` | u8 | bit 0..3の挿入検知 |
| `online_mask` | u8 | bit 0..3の論理online状態 |
| `motion` | u8[4] | channelごとのprinter-facing AMS motion |
| `pull_pct` | u8[4] | channelごとのcached pull percentage |
| `pressure` | u16 | cached aggregate pressure |
| `led_mode` | u8 | management LED override mode |
| `control_error` | u8 | aggregate control-error flag |
| `motion_fault` | u8[4], optional | fault event受信後に生成。最初のeventまではfield自体がない |

indexは0-basedである。UIではchannel 0をSlot 1と表示してよいが、保存値は0-basedを維持する。

AMS motion enum: `0 idle`, `1 send-out`, `2 on-use`, `3 before-pull-back`,
`4 pull-back`, `5 before-on-use`, `6 stop-on-use`。

### 2.3 Channel telemetry `bmcu.channels[0..3]`

各要素は最新のcomplete full-status snapshotから得たobject、または未取得時の`null`である。

| field | 型 | 意味 |
| --- | --- | --- |
| `channel` | u8 | 0-based channel |
| `ams_motion` | u8 | printer-facing AMS motion |
| `inserted` | bool | sampled insertion indication |
| `online` | bool | sampled logical online state |
| `pull_pct` | u8 | sampled pull percentage |
| `sensor_validity` | u8 | `0 unknown`, `1 valid`, `2 stale`, `3 offline`, `4 fault` |
| `flags` | u16 | forward compatibility用raw flags |
| `sensor_online` | bool | flags bit 2 |
| `sensor_good` | bool | flags bit 3 |
| `raw_angle` | u16 | cached AS5600/raw position。sensor rangeでwrapする |
| `position_delta` | i16 | cached signed position delta。累積距離ではない |
| `motor_pwm` | i16 | requested signed software motor command/PWM |
| `motion_fault` | u8 | `0 none`, `1 pull-back進捗なし`, `2 travel budget超過` |
| `controller_motion` | u8/null | sampled BMCU internal controller phase。valid bitなしなら`null` |

Controller motion enum: `0 send`, `1 redetect`, `2 pull`, `3 stop`,
`4 before-on-use`, `5 stop-on-use`, `6 pressure-control-on-use`,
`7 pressure-control-idle`, `8 before-pull-back`。

`motor_pwm`はfirmwareが要求したcommandであり、H-bridge電流やtorqueの証明ではない。
物理的な進捗は、PWM、controller phase、sensor validity、時間方向の`raw_angle`/
`position_delta`を組み合わせて判断する。`raw_angle`はwrapするためmodular差分が必要。
現行の`motion_fault=0`はsend-out進捗を保証しない。実装済みlatchは主にpull-back向けである。

### 2.4 Full snapshot `bmcu.snapshot`

最新の原子的に完成した`GET_FULL_STATUS` record set。full requestはGLOBAL 1、CHANNEL 4、
PRINTER_BUS 1、PRINTER_AUTH 1、PRINTER_RX 3、PRINTER_TX 2、COUNTERS 1の最大13 recordを返す。

| field | 型 | 意味 |
| --- | --- | --- |
| `snapshot_id` | u16 | record set共通identity |
| `record_index`, `record_count` | u8 | assembly位置と期待件数 |
| `record_type` | u8 | `1 global`, `2 channel`, `3 printer bus`, `4 counters`, `5 printer auth`, `6..8 printer RX`, `9..10 printer TX` |
| `hw_tick32` | u32 | snapshot全体で共通のcapture tick |
| `record_data` | hex string | 16-byte unionのraw値 |
| `channel_data` | object, CHANNELのみ | §2.3のfriendly decode |

PicoはCHANNELを`bmcu.channels`、PRINTER_AUTHを`bmcu.printer_auth`、3個のPRINTER_RX
recordを`bmcu.printer_rx`、2個のPRINTER_TX recordを`bmcu.printer_tx`へ、完全なrecord setを
受信した時だけ原子的に展開する。GLOBAL、PRINTER_BUS、COUNTERSをBambuddyが使う場合は
canonical wire offsetで`record_data`を読む。PRINTER_BUSにはprinter online、last RX
class/command/outcome、RX/TX counter、last valid RX ageがあり、COUNTERSにはu32幅の
management TX/RX drop、CRC/frame errorがある。

`bmcu.printer_tx`のfieldは`tx_started`, `tx_completed`, `tx_response_busy`,
`tx_response_missing`, `tx_invalid_length`, `tx_dma_error`, `tx_timeout`, `tx_event_suppressed`。前2つの差は現在進行中
または中断された送信を含み得る。物理DMA障害の判定には`tx_dma_error`/`tx_timeout`を使い、
`tx_response_busy`をDMA障害と解釈してはならない。counterはBMCU起動時からの飽和值である。

### 2.5 Event・sensor

`bmcu.events`はRAM上の直近16件ring。各eventは次を持つ。

| field | 型 | 意味 |
| --- | --- | --- |
| `hw_tick32` | u32 | BMCU event tick |
| `record_type` | u8 | binary record type |
| `severity` | u8 | `0 debug`, `1 info`, `2 notice`, `3 warning`, `4 error`, `5 critical` |
| `source` | u8 | `0 system`, `1 printer bus`, `2 motion`, `3 sensor`, `4 management`, `5 safety` |
| `payload_length` | u8 | 8-byte union中の有効長 |
| `payload` | hex string | lossless/future decode用raw union |
| `event_name` | string | Picoによるfriendly分類 |

Picoが現在展開するrecordは次の2つ。

- `state_change`: `field`, `slot`, `previous_value`, `value`を追加。fieldは
  `1 slot`, `2 inserted_mask`, `3 online_mask`, `4 motion`, `5 pressure`,
  `6 led_mode`, `7 control_error`, `8 motion_fault`。
- `sensor`: `sensor`, `slot`, `validity`, `value_format`, signed i32 `value`を追加。

その他は`event_name="record_<type>"`とraw payloadを保持する。record typeは
`1 boot`, `2 printer_link`, `3 printer_transaction`, `4 state_change`, `5 sensor`,
`6 command_result`, `7 safety_decision`, `8 diagnostic_counter`。

`bmcu.sensors`はnumeric sensor IDごとの最新sensor eventであり、完全なsensor inventoryではない。
`RECORD_SENSOR`を受信するまでは空である。

## 3. `publish(message)` stream

有効frameごとに型付き辞書をcallbackへ渡す。通常は`type`, `kind`, `sequence`を持つ。

| `type` | 追加情報 |
| --- | --- |
| `hello` | `protocol`, `capabilities`, `firmware:[major,minor]`, `tick_hz` |
| `status` | 集約状態を含む`data` |
| `event` | decoded eventを含む`data` |
| `pong` | echoされた`token`, `hw_tick32` |
| `ack` | `request_kind`, numeric `result` |
| `full_status_record` | snapshot metadata、raw `record_data`、任意の`channel_data`, `snapshot_complete` |
| `unknown_or_invalid` | raw `payload` |
| `protocol_error` | `reason`, rejected `frame` |
| `link_state` | Pico生成の`state=online|stale` |

`wifi.py`のWi-Fi状態変化も同じcallbackを通るが、BMCU frameではなくPico-local messageである。
production bridgeはBMCU payload外にdevice ID、receive timestamp、session/boot identity、
transport sequenceを追加する。

production envelope、Pico再起動/BMCU再起動の識別、切断中の再送とdrop通知、転送上限は
[`PICO_BAMBUDDY_ENVELOPE.md`](PICO_BAMBUDDY_ENVELOPE.md)を正とする。数値enumの名前は
[`bmcu_link_enum_registry.json`](bmcu_link_enum_registry.json)を使う。未知の値は数値のまま
保持する。

## 4. 時刻

BMCUはwall clockとuptimeを送らない。BambuddyはPico/server receive timeを保存し、
`hw_tick32`を次のように拡張する。

```text
delta_ticks = (new_tick - old_tick) & 0xffffffff
delta_s = delta_ticks / tick_hz
```

18 MHzでは約238.6秒でwrapする。長時間切断後はwrap回数を推測せず、古い拡張値を捨てて
full snapshotから新しいbaselineを確立する。

## 5. Bambuddy実装指針

1. Picoをstable device identityとprotocol/capabilitiesを持つoptional observerとして登録する。
2. complete full-status setだけをbaselineとして原子的にinstallし、後続STATUS/EVENTを順序適用する。
3. 未知enum値とraw payloadを保存する。append-only enumの追加でdeviceを拒否しない。
4. AMS logical motionとcontroller motionを別fieldで保存する。両者の不一致は診断情報になる。
5. 「commanded but not moving」はvalid sensorのもとで、nonzero PWMとmodular encoder進捗不足が
   一定時間継続した時だけderived alarmにする。
6. `stale`、sequence gap、incomplete snapshot、CRC error、drop増加はtelemetry品質低下として扱い、
   それだけでfilament faultとは判定しない。
7. remote motor/slot operationは無効にする。現行HTTP APIはread-onlyで、writeは内部の
   `set_led_mode()`だけが実装済みである。

## 6. 現行gap・互換性上の注意

- `0x83`はalpha.3でありstable v1ではない。
- `PRINTER_TRANSACTION (0x04)`と`SENSOR_RECORD (0x05)` kind/capabilityはABI予約済みだが、
  現在standalone messageとしてadvertise/emissionされない。一部の等価情報はEVENT/full statusにある。
- PicoはGLOBAL、PRINTER_BUS、COUNTERS unionをnamed JSONへまだ展開しない。
- 現行Picoはbring-up用にfull statusを1秒周期で要求している。canonical設計はconnect/reconnect/
  uncertainty時だけfull snapshot、その後はSTATUS/EVENT差分である。production統合前に周期pollを外す。
- HTTP clientの一部socket error pathは接続を必ずcloseしないため、新規requestが一時停止し得る。
  production transportにはbounded queue、reconnect、backpressureが必要。
- BMCU calibrationは現在blockingでmanagement UART serviceが止まり、RX dropが増え得る。
  Bambuddyはこのgapを表示し、BMCU rebootと即断しない。
