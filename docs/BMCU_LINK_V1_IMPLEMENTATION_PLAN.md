# BMCU Link v1 実装計画

Status: Draft
Current phase: alpha.3 real-device validation
Target: stable wire version `0x01`

Software implementation status (2026-07-19):

- Phase 0 baseline corpus and capture tooling: implemented.
- Phase 1 management-link calibration liveness: implemented; hardware timing gates open.
- Phase 3 Pico resync/recovery: implemented and host-tested.
- Phase 4 dual hardware-UART contexts and scoped HTTP API: implemented and host-tested.
- See [software validation](BMCU_LINK_SOFTWARE_VALIDATION.md) for evidence and open gates.

関連文書:

- [BMCU Link Protocol alpha.3](BMCU_LINK_PROTOCOL_ALPHA3.md)
- [Pico 2 WがBambuddyへ提供できる情報](PICO_BAMBUDDY_OUTPUT.md)
- [BMCU Management Interface](BMCU_MANAGEMENT_INTERFACE.md)
- [Command Ownership](BMCU_COMMAND_OWNERSHIP.md)
- [Pico Command Surface](PICO_COMMAND_SURFACE.md)
- [UART Physical Specification](BMCU_UART_PHYSICAL_SPEC.md)
- [Printer USART1 DMA RX / CPU parser specification](PRINTER_RX_DMA_PARSER_SPEC.md)
- [Pico 2 W multi-BMCU bridge specification](../pico/MULTI_BMCU_SPEC.md)

## 1. 目的

alpha.3の実機統合済みprototypeを、BMCUのprinter/motor制御性能を落とさずに
Bambuddy optional deviceとして長時間運用できるstable v1へ進める。

v1で保証する範囲は次の通り。

1. BMCUの通常動作はPico/Bambuddyの接続状態に依存しない。
2. printer-facing AMS state、BMCU internal controller state、sensor、motor command、
   printer bus状態を再同期可能な形で観測できる。
3. management UARTの混雑・破損・切断がprinter busやmotor制御をblockingしない。
4. Bambuddyはsnapshotとeventから状態を再構成し、telemetry品質と機械異常を区別できる。
5. wire layout、enum、version negotiationをv1 ABIとして固定する。
6. v1時点のremote write操作はLED overrideと`REQUEST_SOFT_RESET`に限定する。

   当初この項は「LED overrideに限定し、機械操作は次期仕様へ送る」だった。soft resetは
   その線を越えるが、線を引いた目的——機械を動かす操作を勢いで足さないこと——は
   満たした上で越えている。idle限定、全channelのPWMゼロ要求、printer bus idle、
   flash write中でないこと、TTL、operation_idの重複拒否を通過した場合のみ受理する
   S2 recovery operationとして設計され、仕様は issue #3 に、拒否理由の一覧は
   `BMCU_MANAGEMENT_INTERFACE.md` §7.2 にある。

   線そのものは残す。次にremote writeを足すときは、この項を書き換えるのではなく、
   同じ水準の安全条件と拒否理由を設計してからここに追記すること。「前例がある」は
   理由にならない。
7. 1台のPico 2 Wへ最大2台のBMCUを独立UARTで接続し、状態、履歴、command routing、
   failureをlink単位で分離できる。

## 2. 現在地

alpha.3ではframe、CRC、sequence、HELLO、STATUS、EVENT、PING/PONG、
GET_FULL_STATUS、ACK、DMA TX、固定長binary recordが実装済みである。
4 channelのAMS motion、controller motion、motor PWM、encoder、sensor validity、
motion faultをPicoでdecodeし、read-only HTTP JSONとして確認できる。

実機では次を確認済み。

- printerからSlot 4のload指令を受信した。
- AMS stateとcontroller stateがload、on-use、stop、pull-backへ遷移した。
- pull-back中にmotor PWMとAS5600 angleの連続変化を観測した。
- calibration中にmanagement serviceが止まり、RX dropが増える現象を確認した。
- nonzero PWMでもencoder進捗がほぼない区間があり、commandと実動を分けて扱う必要を確認した。

現状はfeature-complete prototypeだが、再同期、異常系、長時間運用、ABI凍結が未完了である。

## 3. 基本方針

- printer UART protocolと既存printer response payloadは変更しない。
- management protocolだけを対象にし、printer hot pathとの共通化は観測用cacheまでに留める。
- sensor readをmanagement requestから同期実行しない。既存周期処理のcacheだけをsnapshotする。
- ISRではbyte搬送と既存必須処理だけを行い、log formattingやsnapshot構築を行わない。
- BMCUではdynamic allocation、文字列、JSON、wall clock、float formattingを使わない。
- DMAはbounded queueから送信し、printer/control busを占有するburstを作らない。
- 未知enumはappend-onlyで扱い、Pico/Bambuddyはnumeric値とraw payloadを保存する。
- safety判断はBMCU、履歴相関と通知はBambuddyが所有する。
- 各phaseは実装だけでなくexit criteriaを満たすまで完了としない。
- 複数BMCUを同一TTL busへ接続しない。BMCUごとに独立hardware UARTと独立stateを持つ。
- multi-BMCU対応によってBMCU firmware/wire protocolへbridge固有fieldを追加しない。

## 4. Version方針

| 状態 | wire version | 条件 |
| --- | ---: | --- |
| 現在 | `0x83` | alpha.3実機検証 |
| 互換修正 | `0x83` | layout、offset、既存enum値、意味を変更しない |
| 非互換修正 | `0x84`以降 | field layoutまたは既存fieldの意味を変更する |
| stable v1 | `0x01` | ABI freezeと全validation gate完了 |

「beta 1」はrelease maturityを示すtagとして使えるが、現行wire encodingにはbeta用の
version体系を新設しない。alpha byteからstable `0x01`へ昇格する。alpha peerとstable
peerは暗黙downgradeせず、version不一致を明示的に拒否する。

## 5. Phase 0 — Baseline固定と計測基盤

目的: 最適化・機能追加によるregressionを数値で比較できるようにする。

実装:

- 現行alpha.3のframe corpusと実機JSON sampleを保存する。
- BMCU build matrix、RAM、flash、hot functionのassembly sizeをCI artifact化する。
- printer RX ISR、parser/handler、motion update、management serviceの実行時間を計測できる
  development-only counterを追加する。
- TX/RX queue high-water mark、drop reason、snapshot retryを診断counterへ追加するか、
  wire変更を避ける場合はtest buildだけで収集する。
- protocol encoder/decoderのgolden vectorをC++とPythonで共有する。
- 現在の実機配線、firmware hash、build parameterをtest report templateへ記録する。

Exit criteria:

- clean buildと既存testが再現できる。
- alpha.3の全implemented kindにgolden vectorがある。
- RAM、flash、hot path、drop countの比較baselineが保存される。
- instrumentationをproduction buildから完全に除外できる。

## 6. Phase 1 — BMCU serviceの非blocking化

優先度: P0

目的: calibrationや長い内部処理中もmanagement UARTを飢餓状態にしない。

実装:

- `MC_PULL_calibration_boot()`をphase/state machineへ分解する。
- calibrationの待機loopをtick駆動にし、各stepの間で通常main loopへ戻る。
- direction detectionのmotor pulse、sensor sample、timeoutを明示phaseとして保持する。
- calibration中もRX ring drain、PING/PONG、critical EVENT、TX DMA completionをserviceする。
- full snapshot要求はcalibration中にcached値で返せる場合だけ返し、整合したsnapshotを作れない
  phaseでは`ACK_BUSY`を返す。
- management queue overflow時もmotor safety処理を優先し、drop counterだけを増やす。
- calibration phaseと結果をEVENTへ出す必要性を評価する。wire追加時はalpha.4候補とする。

Exit criteria:

- calibration中にPico linkが6秒staleへ遷移しない。
- calibration開始前後でmanagement RX dropが増えない。
- PINGにbounded responseがあり、実測上の最大latencyをtest reportに残す。
- Picoを切断・再接続してもcalibration結果とmotor direction maskが変化しない。
- printer/motion ISRのbaselineに有意なregressionがない。

## 7. Phase 2 — Motion telemetryと異常検知の完成

優先度: P0

目的: 「commandを出した」と「実際に移動した」を区別し、send/pull両方向の異常を検出する。

実装:

- signed direction maskを唯一の方向sourceにし、廃止済み`dir`相当の重複状態がないことを確認する。
- `raw_angle`のmodular deltaを一度だけ計算し、controller、fault判定、telemetryで共有する。
- send-out、pull-backそれぞれについて次を別々に定義する。
  - startup grace/kick
  - expected-direction minimum progress
  - no-progress time window
  - direction-independent travel budget
  - sensor invalid/offline時の扱い
  - latch解除条件
- thresholdは推測値で固定せず、正常load/unload、空転、jam、手動外力の実測分布から決める。
- fault enumは「no progress」「wrong direction」「travel budget」「sensor unavailable」を区別する
  必要性を評価する。既存enumへのappendは互換、意味変更はalpha.4とする。
- PWM、encoder progress、controller phaseを同一snapshot tickで観測できることを確認する。
- BMCU local safety stopとprinterへのerror reportingを分離する。management faultだけでprinter
  protocol responseを勝手に変更しない。

Exit criteria:

- 正常send/pullでfalse positiveがない。
- motor disconnect、shaft stall、gear slip、sensor offline、逆方向外力を識別または明示的に
  「識別不能」と報告できる。
- send側stallがbounded時間内にlatchされ、motorが安全状態へ遷移する。
- fault発生、停止、解除をfull statusとEVENTの両方から再構成できる。
- 24 V実機試験で4 channelすべての方向とthresholdを確認する。

## 8. Phase 3 — Pico同期・復旧処理

優先度: P0

目的: 1秒full-status pollingを廃止し、snapshot + incremental eventの設計へ合わせる。

実装:

- full status要求をHELLO、初回接続、再接続、sequence gap、incomplete snapshot、
  explicit diagnosis時だけにする。
- snapshotにtimeout、duplicate index検出、missing index検出、bounded retry/backoffを追加する。
- complete record setだけをatomic baselineとしてinstallする。
- BMCU unsolicited sequenceとrequest/response sequenceを別に追跡する。
- `hw_tick32`をPico上でwrap extensionし、disconnectで曖昧になった時はepochを破棄する。
- UART decoder bufferを固定上限化し、syncなしnoiseが連続してもmemory growthしないようにする。
- HTTP socketのsend/recv `OSError`で必ずclientをcloseし、次のrequestを受け付ける。
- Wi-Fi/HTTP workのbudgetを設け、UART pollを常に優先する。
- link stateに`online/stale/resyncing/incompatible`を導入する必要性をBambuddy contractと合わせて決める。

Exit criteria:

- steady stateで周期full snapshotを送らない。
- frame loss、duplicate、CRC corruption、Pico reboot、BMCU rebootから自動復旧する。
- 10分以上のUART noise injectionでmemory使用量が増え続けない。
- dead HTTP client後に手動resetなしで次のclientを処理できる。
- reconnect後に古いsnapshotと新しいEVENTが混在しない。

## 9. Phase 4 — Pico multi-BMCU bridge

優先度: P1

目的: 1台のPico 2 Wで2本の独立BMCU H1 management linkを扱い、Bambuddyには
それぞれを独立optional deviceとして提供する。

物理構成:

- `bmcu-a`: UART0、GP0 TX / GP1 RX
- `bmcu-b`: UART1、GP4 TX / GP5 RX
- 各linkは115200 8E1、3.3 V TTL、BMCU H1との共通GNDを持つ。
- PicoはUSB給電とし、BMCU H1-3電源railは接続しない。
- TX/RXをBMCU間で結線せず、passive splitterとsoftware UARTを使用しない。

実装:

- 現在のsingleton `monitor`、timer、web stateをlink context配列へ置き換える。
- 各linkが独立した`BMCUMonitor`、FrameDecoder、sequence space、snapshot assembly、
  status baseline、event ring、sensor cache、PING timer、liveness/error counterを持つ。
- `BMCU_LINKS`設定からstable `link_id`、UART ID、TX/RX pinを構築する。
- bridge identityとlink identityを分け、外部identityを
  `<bridge_id>/<link_id>`とする。
- Bambuddy dedup keyを`bridge_id, link_id, pico_boot_session, bmcu_boot_session, sequence`とする。
- Pico main loopは各UARTへbounded service budgetを与える。一方のnoiseや大量frameで
  他方のUART、Wi-Fi、HTTPをstarveさせない。
- commandは必ず明示的なlinkへrouteする。LED commandを含めbroadcast writeを禁止する。
- HTTPを次のlink-scoped endpointへ移行する。
  - `GET /api/devices`
  - `GET /api/devices/<link-id>/status`
  - `GET /api/devices/<link-id>/events`
- UIはdevice selectorを持ち、slot、pressure、sensorをBMCU間で合算・混在させない。
- 直接接続上限はPico 2 Wのhardware UART数に合わせて2台とする。3台以上はUART expanderを
  別途supportするかPico bridgeを追加し、v1 direct bridgeの必須範囲には含めない。

Exit criteria:

- 2台の実BMCUを同時接続し、それぞれのsnapshot、EVENT、PING/PONGを独立して維持できる。
- 一方の切断、CRC burst、stalled snapshot、再起動が他方のstatus、timing、sequence、
  command routingを変化させない。
- 全HTTP responseとBambuddy envelopeに`bridge_id`と`link_id`が含まれる。
- UI操作とLED commandは暗黙のglobal slot/deviceを参照せず、選択linkだけを対象にする。
- 2 link同時load/unload telemetry中もdecoder dropとunbounded memory growthがない。
- Pico power loss・reset時も両方のBMCUがlocal printer/motor制御を継続する。

## 10. Phase 5 — Bambuddy向けschemaの確定

優先度: P1

目的: PicoをBambuddyのoptional deviceとして安定して扱えるapplication contractを作る。

実装:

- GLOBAL、CHANNEL、PRINTER_BUS、COUNTERSをすべてnamed objectへdecodeする。
- event unionのBOOT、PRINTER_LINK、PRINTER_TRANSACTION、COMMAND_RESULT、
  SAFETY_DECISION、DIAGNOSTIC_COUNTERをfriendly decodeする。
- raw numeric enum、raw payload、unknown fieldを必ず保持する。
- [`PICO_BAMBUDDY_ENVELOPE.md`](PICO_BAMBUDDY_ENVELOPE.md)を実装の正とし、
  `device_id`、`link.id`、`received_at_us`、`pico_boot_session`、
  `bmcu_boot_session`、sequence、firmware、protocol、capability、modeを全envelopeへ入れる。
- Pico boot時に予測不能な`pico_boot_session`を生成し、valid BMCU HELLOごとに
  `bmcu_boot_session`を増やす。Bambuddy dedup keyは5要素すべてを使う。
- monotonic時刻をUART frame decode時にcaptureする。wall clockは任意の参考値だけとし、
  Bambuddyの到着時刻を正とする。
- ~~PicoからBambuddyへの認証済みoutbound WebSocketを実装する。WebSocket不可時だけ
  batched NDJSON POSTを使い、inbound Pico APIをproduction transportに使わない。~~

  **廃止。** production transportは`BMB1`永続binary TCPに置き換わった。JSON
  WebSocketもHTTPS NDJSON fallbackも実装から削除済みで（`pico/bambuddy_ws.py`、
  `bambuddy_https.py`、`bambuddy_session.py`はいずれも存在しない）、現行の実装は
  `pico/bambuddy_binary_tcp.py`。framing、session確立、ACK、replayの正は
  [`BMCU_BINARY_TRANSPORT_V1.md`](BMCU_BINARY_TRANSPORT_V1.md)で、この計画書は
  それを再掲しない。「inbound Pico APIをproduction transportに使わない」方針だけは
  変わっていない。
- 固定長・固定上限の30秒FIFOを追加する。切断中はoldest-firstで再送し、Bambuddy ACKを
  受けるまで破棄しない。overflow時は`transport_drop`と累積`dropped_count`を送る。
- steady stateのSTATUSを意味的変化時および最大1 Hz heartbeatに抑え、通常2 msg/s/link、
  20 msg/s・5秒以内のburst上限を実装する。飽和時はnon-critical STATUSを先に落とし、
  ERROR/CRITICAL EVENTを可能な限り保持する。
- 同一bridge上の各linkをBambuddyへ別々のoptional deviceとして登録する。
- current stateとappend-only event streamを分ける。
- alarmをsource faultとderived faultに分ける。
  - source: BMCUがlatchしたmotion/sensor/control fault
  - derived: BambuddyがPWMとencoder時系列から判定したno-progress疑い
  - quality: stale、gap、CRC/drop増加、incomplete snapshot
- HTTP `/api/status`は診断用として残すか、production buildで無効にする。
- remote motor/load/unload APIはv1 scope外とし、追加しない。

Exit criteria:

- Bambuddy再起動後、1回のsnapshotと再送queueでcurrent stateを復元できる。
- unknown enumを含むmessageをlosslessに保存できる。
- Pico/BMCU reboot、Bambuddy reconnect、retryで同一eventを重複通知しない。
- 切断中のqueue overflowが`transport_drop`として観測でき、「無イベント」と区別できる。
- 10分のBambuddy切断中もUART service、printer/motor timing、Pico RAMがboundedである。
- telemetry quality低下をfilament jamと誤分類しない。
- secretsをrepository、log、diagnostic JSONへ出さない。
- 別linkの同一sequence、slot、sensor IDがhistoryやalarm上で衝突しない。

## 11. Phase 6 — Performance tuningとresource gate

優先度: P1

目的: management機能のcostを測定し、BMCU hot pathへの影響を固定上限内に収める。

確認対象:

- printer RX ISRとparser/handler
- motion state updateとencoder delta計算
- state cache callback
- event queue enqueue
- full snapshot capture
- DMA descriptor/queue service
- owner/kind/trait dispatch

実装・検査:

- compiler outputを確認し、owner/kind dispatchがbounded branchまたはjump tableになることを確認する。
- sign extension、64-bit helper、division/modulo、float helperの残存をsymbol/assemblyで検査する。
- repeated volatile/global loadをlocal registerへ集約する。
- encoder delta、mask、online/inserted状態を1回だけ計算してcacheを共有する。
- printer response bufferとmanagement bufferを混同・共通化しない。
- memcpy削減はalignmentとcode sizeを含めて実測し、小さい固定copyを無条件に手展開しない。
- DMA burstとqueue depthを測定し、critical event用capacityを常に残す。
- link無効buildでmanagement codeがhot pathから除去可能であることを確認する。

Resource gate:

- management機能追加後もRAM使用率75%未満、flash使用率90%未満を目安とする。
- printer/motion hot functionのworst-case実行時間をbaselineから5%以上悪化させる変更は、
  根拠と実機測定なしにmergeしない。
- management queue満杯時もprinter responseとmotor timingにdrop/blockingを発生させない。
- full snapshot送信中もcritical EVENTをenqueueできる。
- Picoは2 UARTを同時serviceしても各linkのRX bufferとsnapshot assemblyを固定上限内に保つ。
- 一方のlinkが最大入力率でも他方のPING、snapshot、EVENTにunbounded latencyを発生させない。

数値gateはPhase 0の測定結果で見直し、根拠をtest reportへ記録する。

## 12. Phase 7 — Validation matrix

優先度: P0 for v1 promotion

### 12.1 Protocol test

- 全kindのgolden vector
- payload length境界: 0、最大57、58以上
- CRC error、syncずれ、途中切断、連続noise
- unknown kind、unknown enum、reserved field
- sequence wrap、hardware tick wrap
- duplicate/missing/out-of-order snapshot record
- alpha/stable version拒否

### 12.2 Hardware test

4 channelそれぞれについて実施する。

- filament insert/remove
- autoload on/off
- load、on-use pressure control、unload、50 cm retract
- normal motor、stall、motor disconnect、gear slip
- AS5600 normal、offline、stuck、wrap crossing
- printer power cycle、Pico power cycle、BMCU reset
- Pico UART disconnect/reconnect
- calibration中のPING、snapshot、event
- 24 V motor powerとUSB Pico powerの独立投入順序

### 12.3 Endurance test

- 連続8時間のidle monitoring
- 長時間print相当のload/on-use/unload sequence
- 10,000 frame以上のprotocol soak
- repeated Wi-Fi disconnect/reconnect
- repeated HTTP/Bambuddy reconnect
- counter saturation/wrap
- queue pressure下でのcritical event保持

### 12.4 Regression test

- original printer communication response byte列
- motor direction、送り量、引き戻し量
- calibration結果
- build matrix全parameter組み合わせ
- management linkなし構成
- filament RGB、autoload、AMS ID、retract lengthの代表構成

### 12.5 Multi-BMCU isolation test

- 2 BMCU同時boot、snapshot、steady-state EVENT
- 同一sequence値と同一slot indexのlink間分離
- 一方だけのUART disconnect、GND不良相当、CRC/noise burst、frame flood
- 一方だけのBMCU reset、calibration、snapshot timeout
- 2 link同時のload/on-use/unload telemetry
- link selectorとLED commandのtarget検証
- bridge reboot後のstable identityと新boot session
- Bambuddy再接続時の2 device個別baseline
- 8時間のdual-link idle monitoring
- 一方のfaultが他方のalarm、history、UIへ混入しないこと

## 13. Phase 8 — ABI freezeとv1昇格

優先度: P0

手順:

1. 実機試験から必要なwire変更を確定する。
2. 変更があればalpha.4以降で最低1回のvalidation cycleを実施する。
3. kind、payload length、offset、enum、capability、reserved fieldをfreezeする。
4. canonical protocol documentとC++/Python decoderを同一commitで更新する。
5. stable version `0x01`へ変更し、alpha peerとの相互拒否testを追加する。
6. firmware build artifact、SHA256、build parameter、MicroPython versionをreleaseへ添付する。
7. Bambuddy側compatibility matrixとrollback手順を公開する。
8. v1 tag後は既存enumを再採番せず、互換追加は新kind/capabilityで行う。

v1 promotion gate:

- Phase 0から7のexit criteriaをすべて満たす。
- open P0 defectがない。
- wire ABIに未確定fieldがない。
- 4 channel実機validationが完了している。
- 2台の実BMCUを使ったmulti-link isolation/endurance testが完了している。
- endurance test中にprinter/motion regression、unbounded memory growth、silent state divergenceがない。
- pure firmware、Pico、Bambuddyの各rollback artifactがある。

## 14. 推奨実装順序

| 順番 | 作業 | 理由 |
| ---: | --- | --- |
| 1 | Phase 0 baseline | 後続変更の性能・動作比較に必要 |
| 2 | calibration非blocking化 | 現在確認済みのtelemetry欠落原因 |
| 3 | send/pull fault model | 安全性とprotocol fieldの意味を先に確定する必要がある |
| 4 | Pico snapshot/reconnect修正 | 毎秒pollを廃止しproduction flowへ移行 |
| 5 | multi-BMCU context分離 | singletonをなくし、schema確定前にidentityとroutingを固定 |
| 6 | full decodeとBambuddy schema | BMCU fieldとlink identityが安定してからcontractを固定 |
| 7 | performance/assembly tuning | 2 linkを含む全機能が揃った状態で全体costを最適化 |
| 8 | single/dual-link full validation | ABI freeze前の最終実機検証 |
| 9 | v1 freeze/release | validation済みlayoutだけをstable化 |

## 15. Scope外

次はv1 observer protocolのscope外とする。

- remote motor test、load、unload、slot selection
- printer stubのmechanical command拡張
- Bambuddyによるprinter pause/stopの自動実行
- firmware update over BMCU Link
- raw printer UART packetの無制限mirror
- Pico単独の機械安全判断
- passive UART splitterまたは複数BMCUのTTL bus共有
- software UART、および3台以上を直接接続するbridge

これらはv1 telemetryを使った運用実績を得た後、owner、lease、TTL、precondition、
local timeout、audit eventを持つ別versionで設計する。
