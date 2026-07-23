# Bambuddy開発者向け: マルチリンク(複数BMCU/1ブリッジ)対応ノート

対象: Bambuddy(github.com/dj-oyu/bambuddy)のBMCU Link受信側を、1つのPico
ブリッジが複数BMCUを多重化する構成に対応させる開発者。

Pico側は実装済みで、`link_id` 付きenvelopeを1本のWebSocketで送信している。
wire契約の正は [`PICO_BAMBUDDY_ENVELOPE.md`](PICO_BAMBUDDY_ENVELOPE.md)、
Pico側設計は [`../pico/MULTI_BMCU_SPEC.md`](../pico/MULTI_BMCU_SPEC.md)。
本ノートは2026-07-21時点のBambuddyコード調査に基づく。行番号は目安。

## 1. Picoが送ってくるもの(前提)

- 1ブリッジ = 1 WebSocket = 1 `device_id`。その中に複数リンクが多重化される。
- リンクID既定は `bmcu-a` / `bmcu-b`(設定次第で任意文字列、1 Picoあたり最大2)。
- 全envelopeに `link` ブロックが入る:

  ```json
  "link": {
    "id": "bmcu-b",
    "state": "online",
    "pico_boot_session": 17,
    "bmcu_boot_session": 3,
    "transport_sequence": 4211,
    "bmcu_sequence": 902
  }
  ```

- HELLOは capability `multi_link` と、全リンクのセッション一覧
  (`link_sessions`)を広告する。
- `transport_sequence` と `bmcu_sequence` は**リンク内でのみ単調・一意**。
- STATUSのレート制限・合流(coalescing)もPico側でリンク単位。
- 外部identityは `<bridge_id>/<link_id>`。履歴・重複排除は
  `(device_id, link_id, pico_boot_session, bmcu_boot_session, sequence)` で
  キーすること(envelope仕様の要求)。

## 2. 現状評価: どこまでできているか

### 対応済み(そのままでよい層)

| 項目 | 場所 |
| --- | --- |
| dedupウィンドウが `(device_id, link_id)` キー | `backend/app/services/bmcu_link.py` `_dedup`(~99行)、キーは `(device_id, link.id, pico_boot_session, dedup_sequence)`(~152-160行) |
| replay watermark / `persisted_keys()` がリンク別 | 同 `_persisted_key`(~102行)、`persisted_keys()`(~222-237行) |
| ACK: rejected各要素とpersisted watermarkが `link_id` を運ぶ | `backend/app/api/routes/bmcu_link.py`(~174-185行)、schema ~98-114行 |
| イベント行に `link_id` カラムあり | `backend/app/models/bmcu_link_event.py`(~22行)、`_ingest_one`(~340行) |
| リンク数上限 | `MAX_LINKS_PER_DEVICE = 8` と `_link_admitted()`(~256-266行) |
| CONTROL MACに `link_id` を含む | `backend/app/services/bmcu_link_control.py`(~19-31行) |

つまり**転送・重複排除・再送・永続化の層はリンク単位で正しい**。
2リンク目が送信を始めてもデータが失われたり誤dedupされたりはしない。

### 未対応(2リンク目で壊れる層)

すべて「device_idのみでキーしている」ことが原因。

1. **STATUSの相互上書き** — `_last_status[device_id]`(services ~385行)と
   `BMCULinkDevice.last_status`(1ブリッジ1行)。bmcu-aとbmcu-bのSTATUSが
   交互に上書きし、UIのDeviceCardが2台分の値の間でちらつく。
2. **online/stale判定がブリッジ単位** — `_last_seen` / `_link_state`
   (~108-109行)とwatchdog(~537-553行)。bmcu-bが生きている限り、死んだ
   bmcu-aもonline表示のままでオフライン通知が出ない。`bmcu_boot_session` も
   device行にlast-writer-winsで保存される(~435-436行)。
3. **feed-stall監視が合成ストリームを見る** — `backend/app/main.py`
   (~4496-4502行)が `latest_statuses()[0]` を使うため、2台分が交互に
   混ざったstatus列で停滞判定してしまう。
4. **dropped_count集計の過小計上** — folding(~119-121, 395-408行)が
   device_idキーで、同一 `pico_boot_session` 下の2リンクの累積値を
   `max()` で畳むため片方が消える。
5. **UIがブリッジ単位** — DeviceCardは1ブリッジ1枚。リンクセレクタなし、
   イベントテーブルに `link_id` 列・フィルタなし
   (`GET /devices/{id}/events` も未対応、routes ~343-358行)、
   `bmcu_link_status` / `bmcu_link_anomaly` broadcastに `link_id` なし
   (services ~391, 411行、`frontend/src/hooks/useWebSocket.ts` ~426-437行)。
6. **HELLOの `multi_link` / `links[]` は未解釈** — opaque JSONとして保存され、
   `BMCULinkSettings.tsx`(~390-410行)がchipとして表示するだけ。

軽微: CONTROL sequence割当(`control_sequence` / `control_session_nonce`)が
device行に1つ(`bmcu_link_device.py` ~41-42行)。MACがlink_idを束縛するので
安全上の問題はないが、リンク間でカウンタを共有している。

## 3. 推奨する対応順序

### Phase 1: サービス層の状態キーを (device_id, link_id) 化

`BMCULinkService` の以下を `dict[str, ...]` → `dict[tuple[str, str], ...]` に:

- `_last_status`, `_last_status_mono`, `_last_status_broadcast`
- `_last_seen`, `_link_state`
- `_envelope_counts`, `_dropped_session`, `_folded`, `_baseline`

`link_id` は既に全envelopeでvalidation済み(`BMCULinkLink.id`、既定 `"default"`)
なので、ingest経路でキーに足すだけでよい。watchdogは (device, link) ごとに
評価し、オフライン通知にも `link_id` を含める。

注意: transport-level HELLO(`link.id == "transport"`、`bmcu_boot_session`なし)
はリンク状態を作らない特殊値として除外を維持すること(schema ~33-37行、
services ~435-436行)。

### Phase 2: 永続モデル

選択肢は2つ:

- **A(推奨)**: `bmcu_link_links` テーブルを新設(device_id FK + link_id +
  last_status + link_state + bmcu_boot_session + dropped_count、
  UNIQUE(device_id, link_id))。`BMCULinkDevice` はブリッジ属性
  (firmware、capabilities、last_seen全体)だけ持つ。
- B: `BMCULinkDevice` を (device_id, link_id) 行に分割。既存の
  device_id UNIQUE制約と外部参照の変更が大きく、非推奨。

マイグレーションでは既存devicesの現在値を `link_id="default"`(または
HELLOの `links[]` から判明する実ID)として移す。

### Phase 3: API/broadcast/フロント

- `bmcu_link_status` / `bmcu_link_anomaly` broadcastへ `link_id` を追加。
  受信側(`useWebSocket.ts`)は `device_id` + `link_id` でマッチ。
- `GET /devices/{id}/events` に `link_id` クエリフィルタを追加。
- DeviceCardをブリッジカード内のリンク別サブカード(またはリンク行)に分割。
  異なるリンクのスロット/圧力/センサ値を1つの表示に合成しないこと
  (Pico側specの受け入れ基準)。
- feed-stall監視(`main.py`)を (device, link) 単位の評価に変更。

### Phase 4(任意)

- HELLOの `links[]` を解釈してリンクの事前登録・命名に使う。
- CONTROL sequenceをリンク別カウンタにする(現状でも安全ではある)。

## 4. テスト観点

- 同一WebSocketで `bmcu-a` / `bmcu-b` のSTATUSを交互ingestし、
  それぞれの `last_status` が独立に保持されること。
- `bmcu-a` を沈黙させ `bmcu-b` を送信し続けた状態で、`bmcu-a` だけが
  stale/offlineになり通知が出ること。
- 同一 `pico_boot_session` 下で両リンクのdropped_countが独立に累積すること。
- リンク別 `bmcu_boot_session` の再起動検出が他リンクのbaselineを
  無効化しないこと。
- ACKのpersisted watermarkが従来どおりリンク別に返ること(回帰確認)。
- transport HELLO(`link.id="transport"`)がリンクとして登録されないこと。

## 5. 参照

- envelope契約: [`PICO_BAMBUDDY_ENVELOPE.md`](PICO_BAMBUDDY_ENVELOPE.md)
- 転送(ACK/replay/backpressure): [`PICO_BAMBUDDY_TRANSPORT.md`](PICO_BAMBUDDY_TRANSPORT.md)
- Pico側マルチリンク設計と受け入れ基準: [`../pico/MULTI_BMCU_SPEC.md`](../pico/MULTI_BMCU_SPEC.md)
- フィールド一覧・enum: [`PICO_BAMBUDDY_OUTPUT.md`](PICO_BAMBUDDY_OUTPUT.md)
