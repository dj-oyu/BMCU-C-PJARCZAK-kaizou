# BMCU Command Ownership and Arbitration

Status: Draft  
Parent specification: [BMCU_MANAGEMENT_INTERFACE.md](BMCU_MANAGEMENT_INTERFACE.md)

## 1. 目的

プリンタコマンド、ユーザーコマンド、BMCU内部処理が同じモーター・フィラメント経路・
状態を同時に変更しないよう、操作の所有者、優先順位、競合処理、失効、復帰を定義する。

基本原則:

- 通常運転のオーナーはA1 miniである。
- ユーザーは初期段階では観測と期限付きの非動作操作だけを所有できる。
- Picoはtransportであり、コマンドオーナーではない。
- safetyは動作を止められるが、勝手に再開できない。
- 低優先度要求を暗黙に予約・遅延実行しない。競合時は明示的に拒否する。

## 2. Owner種別

| owner | 発行経路 | 所有できる操作 | 備考 |
| --- | --- | --- | --- |
| `PRINTER` | A1 mini bus | 通常のslot/motion/load/unload、状態照会、filament情報 | 通常運転の主オーナー |
| `USER` | Bambuddy→Pico→管理UART | S0/S1、将来明示許可されたS2/S3 | 利用者IDはBambuddy監査ログに保持 |
| `BMCU_LOCAL` | BMCU state machine | sensorに基づく補助動作、既存autoload/unload、保護処理 | root ownerを別途保持する |
| `SAFETY` | BMCU異常判定、将来の認可済み緊急要求 | 出力抑止、安全停止、lease取消 | 最優先。再開権限は持たない |
| `SYSTEM` | boot、timeout、recovery | 初期化、期限切れ解除、安全側復帰 | 新規の機械動作を開始しない |

PicoはBambuddyから受けたUSER要求を検証・転送するだけで、Bambuddy切断時に独自の
USER commandを生成しない。ローカルで即時保護が必要な判断はBMCUのSAFETYに置く。

## 3. Root ownerと実行owner

一つの操作に次の二つを記録する。

- `root_owner`: 操作を必要とした主体
- `executor`: 現在その工程を実行している主体

例: A1 miniのpull back要求を受け、BMCU内部state machineが距離を監視しながら
motorを動かす場合、`root_owner=PRINTER`、`executor=BMCU_LOCAL` である。
これにより「BMCUが勝手に動かした」のか「printer要求をBMCUが実行した」のかを
ログ上で区別できる。

## 4. Resource単位の所有権

デバイス全体を一つのlockにせず、次のresourceに分ける。

| resource | 通常owner | USER操作 |
| --- | --- | --- |
| `MOTION_CH0..3` | PRINTERまたはBMCU_LOCAL | S3有効時だけlease必須 |
| `FILAMENT_PATH` | PRINTER | slot変更/load/unloadは排他的 |
| `AUTO_SWAP` | PRINTERをrootとするBMCU_LOCAL | USER開始は別途許可が必要 |
| `FILAMENT_METADATA` | PRINTER | USER編集はprinter offline時のみ検討 |
| `LED_OVERRIDE` | USER | timeout付き。通常表示へ必ず復帰 |
| `DIAGNOSTICS` | shared read-only | S0として常時利用可能 |
| `SAFETY_LATCH` | SAFETY | USERはack可。解除・再開とは分離 |
| `DEVICE_RESET` | USER | S2として実装済み（`REQUEST_SOFT_RESET`, `BMCU_MANAGEMENT_INTERFACE.md` §7.2）。この文書のresource/leaseモデルは未実装だが、既にidle-only条件とoperation IDで排他されている |

FILAMENT_PATHを使う操作は対象MOTION_CHも同時に取得する。必要resourceを全部取得
できない場合は、部分的にstateを変更せず要求全体を拒否する。

## 5. 優先順位

```text
SAFETY > active operation owner > PRINTER > BMCU_LOCAL > USER(S0/S1)
```

- SAFETYは新規開始を禁止し、進行中operationを安全停止へ移行できる。
- 開始済みの機械動作は完了、失敗、取消までそのoperationが所有する。
- printer onlineまたはprinting中、motion pathの通常ownerはPRINTERである。
- BMCU_LOCALの補助動作は、起点となったroot ownerを引き継ぐ。
- USERのLEDとDIAGNOSTICSはmotion resourceと独立しているため並行できる。
- 低優先度要求はキューに残して後から勝手に実行せず、`BUSY`/`DENIED`を返す。

同じpriority同士では先着順ではなく、現在operationの継続を優先する。新しい要求で
進行中動作を上書きする場合は、actionごとに明示したcancel手順が必要である。

## 6. Printer command

### 6.1 受理条件

- wire CRCとlengthが正常
- target AMSが一致
- slot/channelが範囲内
- BMCU/AMSがonline
- 必要resourceが取得可能
- SAFETY latchが危険方向の操作を禁止していない
- 現在stateから許される遷移である

### 6.2 所有権付与

受理したprinter commandには、受信時の `transaction_id` とは別に内部
`operation_id` を付ける。同一要求に起因する `set_motion`、motor state machine、
sensor判定、printer replyを同じoperationへ関連付ける。

### 6.3 拒否と抑止

- target違い、slot不正、offline等を単なる無応答とせずdecision reasonへ記録する。
- SAFETY latch中の危険操作は、可能ならprinter replyにも異常状態を反映する。
- unknown/no-handler commandを成功扱いしない。
- printer heartbeat消失後はPRINTERに新しいmechanical resourceを取得させない。
- heartbeat消失時の進行中動作はaction別のsafe abort pointへ移行する。

管理ログには `transaction_id, operation_id, root_owner, executor, resource_mask,
decision, reason` を含める。

## 7. USER command

### 7.1 S0/S1

- S0の読取りは所有権不要。snapshotの一貫性だけを保証する。
- S1は独立resourceなら許可する。
- LED overrideは必ずtimeoutを持ち、期限切れ・再起動・明示解除で通常表示へ戻る。
- S0/S1要求がprinter packetやmotor周期を待たせてはならない。

### 7.2 S2以上

次をすべて満たす場合だけ受理する。

- firmware capabilityあり
- device configurationで明示enable済み
- user roleがBambuddy側で認可済み
- precondition成立
- resource lease取得成功
- operation IDが新規または既知のretry
- TTLが残っている

初期仕様ではprinter online/printing中のUSER S3を
`DENIED_PRINTER_OWNS_RESOURCE` とする。USER commandをprinter packetや内部stateの
偽装注入として実装してはならず、専用の検証済みoperation APIを経由させる。

### 7.3 USER stop

UIの「停止」は一つの成功値にまとめない。

1. BMCU local inhibit request/result
2. BambuddyからA1 miniへのpause/stop request/result
3. 実際のprinter job state確認

三つを別々に表示・記録する。BMCU側が止まってもprinter全体が停止したとは限らず、
printer pause成功だけでもBMCU motorが直ちに安全状態とは限らない。

## 8. Operationとlease

S2以上のUSER commandと、追跡対象のprinter mechanical commandをoperationとして扱う。

```text
operation_id:u32 | root_owner:u8 | executor:u8 | resource_mask:u16 |
action:u8 | reason:u8 | ttl_100ms:u16 | precondition_flags:u16
```

- USER operation IDはBambuddyがboot session内で一意に生成する。
- printer operation IDはBMCUがtransactionから生成する。
- BMCUは最近のoperation IDと最終結果を固定長cacheに保持する。
- 同じoperationのretryで機械動作を二重実行しない。
- lease期限後に新しい工程を開始しない。
- 動作途中の期限切れは即座の逆転動作を意味せず、action別safe abort pointへ移る。
- communication loss、printer競合、sensor fault、SAFETY latchでleaseを失効できる。
- 失効後の再開には新operationが必要で、自動resumeは禁止する。

Printer commandは既存wire protocolにTTLを持たないため、printer heartbeatとcommand
stateから内部leaseを生成する。USER leaseと同じwire構造をprinterへ要求しない。

## 9. Ownership state machine

```text
FREE -> RESERVED -> RUNNING -> COMPLETING -> RELEASED
                  |       |
                  +-> ABORTING -> LATCHED
```

| state | 意味 |
| --- | --- |
| `FREE` | ownerなし |
| `RESERVED` | precondition検証済み、まだ出力変更なし |
| `RUNNING` | operation実行中 |
| `COMPLETING` | 出力停止・sensor結果確認中 |
| `RELEASED` | 結果記録済み、resource解放 |
| `ABORTING` | safe abortを実行中 |
| `LATCHED` | safety/user intervention待ち |

resource取得と最初のstate変更の間に失敗した場合は `FREE` へ戻す。`RUNNING`以降の
失敗はaction固有のabortを経由し、理由なしに直接FREEへ戻さない。

## 10. SAFETY latchと復帰

停止権限と再開権限を分離する。

- SAFETYは他ownerのleaseを取消し、出力を安全側へ制限できる。
- USER ackは「利用者が確認した」の記録であり、latch解除ではない。
- 解除にはsensor正常化、最小安定時間、printer state、明示reset policyを要求する。
- BMCU local inhibitが解除されてもprinter jobを自動resumeしない。
- 停止理由、停止時sensor snapshot、取消operation、解除条件をeventに残す。

## 11. 調停結果

| result/reason | 意味 |
| --- | --- |
| `OK` | 受理または完了。phaseを別途示す |
| `OWNER_CONFLICT` | 別ownerのoperation実行中 |
| `PRINTER_OWNS_RESOURCE` | printerが対象resourceを所有 |
| `LEASE_REQUIRED` | 必要なleaseなし |
| `LEASE_EXPIRED` | TTL切れ |
| `PRECONDITION_FAILED` | printer/sensor/state条件不成立 |
| `SAFETY_LATCHED` | safety latchにより禁止 |
| `CAPABILITY_DISABLED` | build/configで機能無効 |
| `DUPLICATE_OPERATION` | 同じoperationを処理済み |
| `BAD_STATE` | state遷移が許可されない |
| `BUSY` | 一時的にresource取得不能 |

既存ACKの小さいresult codeは互換性のため維持し、詳細reasonはcommand result eventで
返す。ACK `OK` は受理を意味し、機械動作の完了はoperation eventで通知する。

## 12. 観測messageへの追加項目

`BMCU_DECISION`とoperation eventには最低限次を含める。

```text
transaction_id:u16 | operation_id:u32 | root_owner:u8 | executor:u8 |
resource_mask:u16 | phase:u8 | result:u8 | reason:u8
```

64 byte上限に収まらない場合も文字列化せず、printer transaction recordとoperation
recordを分け、IDで結合する。Bambuddyはownerごとに色分けし、競合・取消・safety
overrideを時系列で表示する。

## 13. 受入条件

- printer要求からBMCU内部補助動作までroot ownerがPRINTERとして追跡できる。
- USER LED操作中もprinter motion ownershipが変化しない。
- printer online中の未許可USER S3が確実に拒否される。
- 同じUSER operation retryでmotor actionが二重実行されない。
- SAFETY発生時に進行中owner、取消resource、停止結果が記録される。
- latch ackだけでmotorやprinter jobが再開しない。
- printer heartbeat消失時に新しいPRINTER operationを開始しない。
- owner競合でmain loop、printer reply、motor周期をblockしない。

