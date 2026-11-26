// -*- mode: Bluespec; -*-
//
// The BMCU motion layers flattened into one machine per channel, sharing the
// merger and the downstream-strand mask. One event at a time: every action
// below is one printer command, one sensor edge, one timer expiry or one human
// gesture, which is the cooperative main loop's own guarantee.
//
// Source: the state map (section 14 of the BMCU state-map document) and the
// code it cites. Phases are the composite states of that section; the autoload
// internals (INS_CHECK .. AUTO_FAIL) collapse into one `autoload` step because
// nothing in them touches the merger or the strand past it. LOW_STOP is left
// out for the same reason; JAM is kept because it shortens the next pull.
//
// Two kinds of variable:
//   controller  what the firmware holds: phase, owner, inTail, jammed and
//               pullClears (whether the running pull is full length)
//   physical    where the strand actually is: pos
// The invariants are about the second agreeing with the first, so pos is
// moved only by what physically moves filament, never by a firmware write.
//
// Modelling assumptions, each one a place where the model is less than the
// hardware:
//   A1  Forward drive that can take a tip past the merger: send, and the
//       hold_load push of before_on_use on a parked strand (nondeterministic:
//       it may or may not get there). Idle feeds and the DM Stage-2 re-push on
//       a parked channel are assumed to stay short of the merger -- see the
//       design review; they are not modelled.
//   A2  A full-length pull (AMS_RETRACT_LEN) brings the tip back upstream of
//       the merger; the build setting is chosen for this. The jam-shortened
//       100 mm pull does not.
//   A3  A tail piece left past the merger leaves only by the printer or a
//       human (`remnantCleared`), never by a BMCU motion.
//   A4  Timers are nondeterministic: any timeout may fire whenever its state
//       is held.
//   A5  Persistence is immediate: a reboot restores exactly what RAM held.
//       The firmware writes flash a few ms later, so a power cut in that
//       window can lose the newest change.
module bmcu_merger {

  // ---------------------------------------------------------------- types

  type Phase =
    | EMPTY | PARKED | UNLOADING
    | SEND | SEND_HOLD | SEND_ABORT
    | PURGE | PRINT | PAUSE | JAM | ORPHAN
    | RUNOUT | TAIL | TAIL_STUCK
    | ASSIST | PULL | PULL_FROZEN | REDETECT | HELD | FAULT

  // Where the channel's strand is. TailDown is a cut-off tail piece past the
  // merger with nothing of it left in the BMCU.
  type Pos = None | Up | Down | TailDown

  pure val CHANNELS: Set[int] = 0.to(2)
  pure val NO_CH: int = -1

  // Phases whose AMS-side motion is on_use / before_on_use / stop_on_use.
  pure val USING: Set[Phase] = Set(PURGE, PRINT, PAUSE, JAM, RUNOUT, TAIL)

  // ---------------------------------------------------------------- state

  var phase: int -> Phase
  var pos: int -> Pos
  var owner: int            // merger owner, NO_CH when free
  var inTail: bool          // merger in TAIL (only with an owner)
  var jammed: Set[int]      // g_on_use_jam_latch (RAM)
  var pullClears: Set[int]  // the running pull is full length (not jam-shortened)
  var lastAction: str

  // ---------------------------------------------------------------- helpers

  pure def isDownstream(p: Pos): bool = p == Down or p == TailDown

  // Where each strand is relative to the merger is the business of
  // strand_zone.qnt; this module keeps the merger and the phases. These
  // stand where the first version of this spec updated a downstream mask.
  pure def markSent(x: int): bool = true
  pure def markReturned(x: int): bool = true
  pure val keepMask: bool = true

  action keepLatches: bool = all { jammed' = jammed, pullClears' = pullClears }

  // Whatever another channel was doing on the AMS side is dropped when this
  // channel's command is accepted (bambu_bus_ams.cpp: the previous
  // now_filament_num is set to idle). A strand that was being sent or used is
  // left wherever it is: ORPHAN. A pull keeps going -- its sequencer does not
  // look at the AMS motion. A TAIL owner preempted by a send drops to EMPTY.
  pure def deselected(c: int, x: int, ph: Phase, own: int): Phase =
    if (c == x) ph
    else if (c == own and ph == TAIL) EMPTY
    else if (ph.in(Set(SEND, SEND_HOLD, SEND_ABORT, PURGE, PRINT, PAUSE, JAM, RUNOUT, ASSIST))) ORPHAN
    else ph

  // ---------------------------------------------------------------- init

  action init = all {
    nondet start = CHANNELS.setOfMaps(Set("empty", "parked")).oneOf()
    all {
      phase' = CHANNELS.mapBy(c => if (start.get(c) == "parked") PARKED else EMPTY),
      pos' = CHANNELS.mapBy(c => if (start.get(c) == "parked") Up else None),
    },
    owner' = NO_CH,
    inTail' = false,
    jammed' = Set(),
    pullClears' = Set(),
    lastAction' = "init",
  }

  // ---------------------------------------------------------------- printer

  // The printer handler accepts before_on_use / on_use only while the merger
  // is free or already this channel's (allow_any), and stop_on_use /
  // before_pull_back only from the owner (allow_stop); anything else is
  // dropped before it touches motion or the merger, which is a stutter here.
  pure def allowAny(own: int, x: int): bool = own == NO_CH or own == x
  pure def allowStop(own: int, x: int): bool = own == x

  // send_out(x): preempts whoever holds the merger, the channel itself
  // included. From an empty channel only the AMS-side value changes: the key
  // reads empty, so motor_motion_switch never enters send.
  action sendOut(x: int): bool = all {
    phase.get(x).in(Set(PARKED, HELD, FAULT, ORPHAN, JAM, PRINT, PAUSE, PURGE)),
    phase' = CHANNELS.mapBy(c =>
      if (c == x) SEND else deselected(c, x, phase.get(c), owner)),
    pos' = pos.set(x, if (pos.get(x) == Up) Down else pos.get(x)),
    owner' = NO_CH,
    inTail' = false,
    markSent(x),
    // send_out clears the jam latch (buffer above 85 %).
    jammed' = jammed.exclude(Set(x)),
    pullClears' = pullClears,
    lastAction' = "sendOut",
  }

  action sendHold(x: int): bool = all {
    phase.get(x) == SEND,
    phase' = phase.set(x, SEND_HOLD),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "sendHold",
  }

  action sendAbort(x: int): bool = all {
    phase.get(x) == SEND,
    phase' = phase.set(x, SEND_ABORT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "sendAbort",
  }

  // before_on_use(x): takes the merger, and with it the bit (owner implies
  // downstream). From a parked strand hold_load may push it past the merger
  // without any send (A1).
  action beforeOnUse(x: int): bool = all {
    allowAny(owner, x),
    phase.get(x).in(Set(SEND, SEND_HOLD, SEND_ABORT, PARKED, HELD, FAULT, TAIL, ORPHAN)),
    nondet pushed = Set(true, false).oneOf()
    pos' = pos.set(x,
      if (pos.get(x) == Up and pushed and phase.get(x).in(Set(PARKED, HELD))) Down
      else pos.get(x)),
    phase' = CHANNELS.mapBy(c =>
      if (c == x) PURGE else deselected(c, x, phase.get(c), owner)),
    owner' = x,
    inTail' = false,
    markSent(x),
    keepLatches,
    lastAction' = "beforeOnUse",
  }

  action onUse(x: int): bool = all {
    allowAny(owner, x),
    phase.get(x).in(Set(PURGE, PAUSE, SEND_HOLD, TAIL, ORPHAN)),
    phase' = CHANNELS.mapBy(c =>
      if (c == x) PRINT else deselected(c, x, phase.get(c), owner)),
    pos' = pos,
    owner' = x,
    inTail' = false,
    markSent(x),
    keepLatches,
    lastAction' = "onUse",
  }

  action stopOnUse(x: int): bool = all {
    allowStop(owner, x),
    phase.get(x).in(Set(PURGE, PRINT)),
    phase' = phase.set(x, PAUSE),
    pos' = pos, owner' = owner, inTail' = inTail,
    markSent(x),
    keepLatches,
    lastAction' = "stopOnUse",
  }

  // before_pull_back(x): releases the merger at command time, before the
  // strand has moved. In TAIL nothing moves at all.
  action beforePullBack(x: int): bool = all {
    allowStop(owner, x),
    phase.get(x).in(Set(PURGE, PRINT, PAUSE, JAM, TAIL)),
    phase' = phase.set(x, if (phase.get(x) == TAIL) TAIL_STUCK else ASSIST),
    pos' = pos,
    owner' = NO_CH,
    inTail' = false,
    keepMask, keepLatches,
    lastAction' = "beforePullBack",
  }

  // 0xFF 03/00 on the selected channel. A jam-latched channel pulls 100 mm
  // only, which leaves the tip in the tube (A2), so that pull may not clear.
  action pullBack(x: int): bool = all {
    phase.get(x).in(Set(ASSIST, PURGE, PRINT, PAUSE, JAM, TAIL)),
    val short = jammed.contains(x)
    all {
      phase' = phase.set(x, if (phase.get(x) == TAIL) TAIL_STUCK else PULL),
      pullClears' = if (phase.get(x) == TAIL) pullClears
                    else if (short) pullClears.exclude(Set(x))
                    else pullClears.union(Set(x)),
      lastAction' = if (short) "jamPullBack" else "pullBack",
    },
    pos' = pos,
    owner' = if (owner == x) NO_CH else owner,
    inTail' = if (owner == x) false else inTail,
    keepMask,
    jammed' = jammed,
  }

  // Global idle: refused while the selected channel is using, ends TAIL_STUCK.
  action idleAll: bool = all {
    phase' = CHANNELS.mapBy(c => if (phase.get(c) == TAIL_STUCK) EMPTY else phase.get(c)),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "idleAll",
  }

  // ---------------------------------------------------------------- motion

  // The pull reached its target: on to redetect. A full pull has the tip back
  // upstream of the merger; a jam pull does not (A2).
  action pullDone(x: int): bool = all {
    phase.get(x) == PULL,
    phase' = phase.set(x, REDETECT),
    pos' = pos.set(x, if (pullClears.contains(x)) Up else pos.get(x)),
    owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "pullDone",
  }

  // Redetect found the key. Clear site 1, gated on the pull being evidence.
  action redetectFound(x: int): bool = all {
    phase.get(x) == REDETECT,
    phase' = phase.set(x, HELD),
    pos' = pos, owner' = owner, inTail' = inTail,
    if (pullClears.contains(x)) markReturned(x) else keepMask,
    jammed' = jammed,
    pullClears' = pullClears.exclude(Set(x)),
    lastAction' = if (pullClears.contains(x)) "redetectFound" else "redetectFoundShort",
  }

  action redetectTimeout(x: int): bool = all {
    phase.get(x) == REDETECT,
    phase' = phase.set(x, FAULT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "redetectTimeout",
  }

  // No progress, travel budget, or a bus loss that outlasted 3 s.
  action pullFault(x: int): bool = all {
    phase.get(x).in(Set(PULL, PULL_FROZEN)),
    phase' = phase.set(x, FAULT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "pullFault",
  }

  action busLost(x: int): bool = all {
    phase.get(x) == PULL,
    phase' = phase.set(x, PULL_FROZEN),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "busLost",
  }

  action busBack(x: int): bool = all {
    phase.get(x) == PULL_FROZEN,
    phase' = phase.set(x, PULL),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "busBack",
  }

  // ---------------------------------------------------------------- printing

  // Buffer below 40 % for 300 ms: the jam latch.
  action jam(x: int): bool = all {
    phase.get(x) == PRINT,
    pos.get(x) == Down,
    phase' = phase.set(x, JAM),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    jammed' = jammed.union(Set(x)),
    pullClears' = pullClears,
    lastAction' = "jam",
  }

  // Only the extruder consumes filament, and it can only do so with the tip in
  // the head, so a runout needs the strand past the merger.
  action keyEmptyWhilePrinting(x: int): bool = all {
    phase.get(x) == PRINT,
    pos.get(x) == Down,
    phase' = phase.set(x, RUNOUT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "keyEmptyWhilePrinting",
  }

  action keyBack(x: int): bool = all {
    phase.get(x) == RUNOUT,
    phase' = phase.set(x, PRINT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "keyBack",
  }

  // The tail has cleared the BMCU: what is left is past the merger only.
  action toTail(x: int): bool = all {
    phase.get(x) == RUNOUT,
    owner == x,
    phase' = phase.set(x, TAIL),
    pos' = pos.set(x, TailDown),
    owner' = owner,
    inTail' = true,
    keepMask, keepLatches,
    lastAction' = "toTail",
  }

  // ---------------------------------------------------------------- loader

  action autoload(x: int): bool = all {
    phase.get(x) == EMPTY,
    pos.get(x) == None,
    phase' = phase.set(x, PARKED),
    pos' = pos.set(x, Up),
    owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "autoload",
  }

  action autoUnloadStart(x: int): bool = all {
    phase.get(x).in(Set(PARKED, HELD)),
    phase' = phase.set(x, UNLOADING),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "autoUnloadStart",
  }

  // The key read empty for the window: pulled out of the loader. Clear site 2.
  action autoUnloadEmpty(x: int): bool = all {
    phase.get(x) == UNLOADING,
    phase' = phase.set(x, EMPTY),
    pos' = pos.set(x, None),
    owner' = owner, inTail' = inTail,
    markReturned(x),
    keepLatches,
    lastAction' = "autoUnloadEmpty",
  }

  action autoUnloadGiveUp(x: int): bool = all {
    phase.get(x) == UNLOADING,
    phase' = phase.set(x, PARKED),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "autoUnloadGiveUp",
  }

  // A human pulls a parked strand out by hand. The bit is not cleared: the
  // firmware cannot tell this from the extruder consuming an orphan.
  action handRemoved(x: int): bool = all {
    phase.get(x).in(Set(PARKED, HELD)),
    phase' = phase.set(x, EMPTY),
    pos' = pos.set(x, None),
    owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "handRemoved",
  }

  // A3: the printer or a human clears a tail piece the BMCU no longer sees.
  action remnantCleared(x: int): bool = all {
    pos.get(x) == TailDown,
    phase.get(x) == EMPTY,
    phase' = phase,
    pos' = pos.set(x, None),
    owner' = owner, inTail' = inTail, keepMask, keepLatches,
    lastAction' = "remnantCleared",
  }

  // ---------------------------------------------------------------- reboot

  // RAM is lost; owner and tail come back from flash (A5). The restored
  // owner resumes in PRINT or TAIL; everyone else is rebuilt from the key.
  action reboot: bool = all {
    phase' = CHANNELS.mapBy(c =>
      if (c == owner) (if (inTail) TAIL else PRINT)
      else if (pos.get(c) == Up or pos.get(c) == Down) PARKED
      else EMPTY),
    pos' = pos, owner' = owner, inTail' = inTail,
    jammed' = Set(),
    pullClears' = Set(),
    lastAction' = "reboot",
  }

  // ---------------------------------------------------------------- step

  // `dice` only weights the random simulator, which picks evenly among the
  // enabled branches: always-enabled events (reboot, idle, a hand pulling a
  // strand out, the unload gesture) would otherwise fill every trace and the
  // long unload sequences would never be reached. It restricts nothing for
  // `quint verify`, where every value of dice is explored.
  action step = {
    nondet x = CHANNELS.oneOf()
    nondet dice = 0.to(19).oneOf()
    any {
      all { dice == 0, reboot },
      all { dice < 2, idleAll },
      sendOut(x), sendHold(x), sendAbort(x),
      beforeOnUse(x), onUse(x), stopOnUse(x),
      beforePullBack(x), pullBack(x),
      pullDone(x), redetectFound(x), redetectTimeout(x),
      pullFault(x), busLost(x), busBack(x),
      all { dice < 4, jam(x) },
      keyEmptyWhilePrinting(x), keyBack(x), toTail(x),
      all { dice < 4, autoload(x) },
      all { dice < 2, autoUnloadStart(x) }, autoUnloadEmpty(x),
      all { dice < 4, autoUnloadGiveUp(x) },
      all { dice < 1, handRemoved(x) }, remnantCleared(x),
    }
  }

  // ---------------------------------------------------------------- invariants

  // TAIL only with an owner, and that owner is the channel in TAIL.
  val tailHasOwner: bool =
    inTail implies (owner != NO_CH and phase.get(owner) == TAIL)

  // The merger owner is a channel that is using the tube.
  val ownerIsUsing: bool =
    owner == NO_CH or phase.get(owner).in(USING)

  // At most one channel is in a using phase: the merger mutex holds.
  val singleUser: bool =
    CHANNELS.filter(c => phase.get(c).in(USING)).size() <= 1

  val safety: bool = all {
    tailHasOwner, ownerIsUsing, singleUser,
  }

  // ---------------------------------------------------------------- witnesses
  //
  // Expected to be VIOLATED. Each is a known behaviour the firmware does not
  // prevent (the printer is master) or a known limit of the bit; the check
  // proves the model can reach it, so a model that silently stopped reaching
  // it would be caught.

  // Race 1: two strands past the merger at once (send on Y while X is still
  // pulling back, or never retracted).
  val witnessNoCollision: bool =
    not(CHANNELS.exists(a => CHANNELS.exists(b =>
      a != b and isDownstream(pos.get(a)) and isDownstream(pos.get(b)))))

  // ORPHAN after reboot reads PARKED in every merger/phase variable: only the
  // strand zones (strand_zone.qnt) can tell it apart.
  val witnessNoParkedOrphan: bool =
    not(CHANNELS.exists(c => phase.get(c) == PARKED and pos.get(c) == Down))
}
