// -*- mode: Bluespec; -*-
//
// The BMCU motion layers flattened into one machine per channel, sharing the
// merger and the downstream-strand mask. One event at a time: every action
// below is one printer command, one sensor edge, one timer expiry or one human
// gesture, which is the cooperative main loop's own guarantee.
//
// Source: the state map (docs/state_map, section 14) and the code it cites.
// Phases are the composite states of that section; the autoload internals
// (INS_CHECK .. AUTO_FAIL) collapse into one `autoload` step because nothing
// in them touches the merger or the strand past it.
//
// Two kinds of variable:
//   controller  what the firmware holds: phase, owner, inTail, down
//   physical    where the strand actually is: pos
// The invariants are about the second agreeing with the first, so pos is
// moved only by what physically moves filament, never by a firmware write.
//
// Modelling assumptions, each one a place where the model is less than the
// hardware:
//   A1  Only the forward drive of send moves a tip from the merger entrance
//       past it. Pressure control (before_on_use, on_use) on a strand whose
//       tip is not in the extruder is assumed not to push it through.
//   A2  A finished pull brings the tip back upstream of the merger. The pull
//       length (0.6 m) is chosen for this; a wrongly short setting breaks it.
//   A3  A inTail piece left past the merger leaves only by the printer or a
//       human (`remnantCleared`), never by a BMCU motion.
//   A4  Timers are nondeterministic: any timeout may fire whenever its state
//       is held.
module bmcu_merger {

  // ---------------------------------------------------------------- types

  type Phase =
    | EMPTY | PARKED | UNLOADING
    | SEND | SEND_HOLD | SEND_ABORT
    | PURGE | PRINT | PAUSE | ORPHAN
    | RUNOUT | TAIL | TAIL_STUCK
    | ASSIST | PULL | PULL_FROZEN | REDETECT | HELD | FAULT

  // Where the channel's strand is. TailDown is a cut-off inTail piece past the
  // merger with nothing of it left in the BMCU.
  type Pos = None | Up | Down | TailDown

  pure val CHANNELS: Set[int] = 0.to(2)
  pure val NO_CH: int = -1

  // ---------------------------------------------------------------- state

  var phase: int -> Phase
  var pos: int -> Pos
  var owner: int          // merger owner, NO_CH when free
  var inTail: bool          // merger in TAIL (only with an owner)
  var down: Set[int]      // downstream-strand mask (flash-backed)
  var reported: Set[int]  // channels whose last send entry raised DOWNSTREAM_OCCUPIED
  var lastAction: str

  // ---------------------------------------------------------------- helpers

  pure def isDownstream(p: Pos): bool = p == Down or p == TailDown

  // mark_sent: report others still marked, then set this channel's bit.
  action markSent(x: int): bool = all {
    down' = down.union(Set(x)),
    reported' = if (down.exclude(Set(x)).size() > 0) reported.union(Set(x))
                else reported.exclude(Set(x)),
  }

  action markReturned(x: int): bool = all {
    down' = down.exclude(Set(x)),
    reported' = reported,
  }

  action keepMask: bool = all { down' = down, reported' = reported }

  // A send on `x` preempts whoever owns the merger. A TAIL owner drops to
  // EMPTY (its inTail piece stays where it is); a PRINT owner becomes ORPHAN.
  pure def preemptedPhase(c: int, x: int, ph: Phase, own: int): Phase =
    if (c == x) ph
    else if (c == own and ph == TAIL) EMPTY
    else if (ph == PRINT or ph == PAUSE or ph == PURGE) ORPHAN
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
    down' = Set(),
    reported' = Set(),
    lastAction' = "init",
  }

  // ---------------------------------------------------------------- printer

  // send_out(x). From an empty channel only the AMS-side value changes: the
  // key reads empty, so motor_motion_switch never enters send.
  action sendOut(x: int): bool = all {
    phase.get(x).in(Set(PARKED, HELD, FAULT, ORPHAN)),
    phase' = CHANNELS.mapBy(c =>
      if (c == x) SEND else preemptedPhase(c, x, phase.get(c), owner)),
    pos' = pos.set(x, Down),
    owner' = NO_CH,
    inTail' = false,
    markSent(x),
    lastAction' = "sendOut",
  }

  action sendHold(x: int): bool = all {
    phase.get(x) == SEND,
    phase' = phase.set(x, SEND_HOLD),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "sendHold",
  }

  action sendAbort(x: int): bool = all {
    phase.get(x) == SEND,
    phase' = phase.set(x, SEND_ABORT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "sendAbort",
  }

  // The printer handler accepts before_on_use / on_use only while the merger
  // is free or already this channel's (allow_any), and stop_on_use /
  // before_pull_back only from the owner (allow_stop); anything else is
  // dropped before it touches motion or the merger, which is a stutter here.
  pure def allowAny(own: int, x: int): bool = own == NO_CH or own == x
  pure def allowStop(own: int, x: int): bool = own == x

  // before_on_use(x): takes the merger.
  action beforeOnUse(x: int): bool = all {
    allowAny(owner, x),
    phase.get(x).in(Set(SEND, SEND_HOLD, SEND_ABORT, PARKED, HELD, FAULT, TAIL)),
    phase' = phase.set(x, PURGE),
    pos' = pos,
    owner' = x,
    inTail' = false,
    keepMask,
    lastAction' = "beforeOnUse",
  }

  action onUse(x: int): bool = all {
    allowAny(owner, x),
    phase.get(x).in(Set(PURGE, PAUSE, SEND_HOLD, TAIL)),
    phase' = phase.set(x, PRINT),
    pos' = pos,
    owner' = x,
    inTail' = false,
    keepMask,
    lastAction' = "onUse",
  }

  action stopOnUse(x: int): bool = all {
    allowStop(owner, x),
    phase.get(x).in(Set(PURGE, PRINT)),
    phase' = phase.set(x, PAUSE),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "stopOnUse",
  }

  // before_pull_back(x): releases the merger at command time, before the
  // strand has moved. In TAIL nothing moves at all.
  action beforePullBack(x: int): bool = all {
    allowStop(owner, x),
    phase.get(x).in(Set(PURGE, PRINT, PAUSE, TAIL)),
    phase' = phase.set(x, if (phase.get(x) == TAIL) TAIL_STUCK else ASSIST),
    pos' = pos,
    owner' = if (owner == x) NO_CH else owner,
    inTail' = if (owner == x) false else inTail,
    keepMask,
    lastAction' = "beforePullBack",
  }

  action pullBack(x: int): bool = all {
    phase.get(x).in(Set(ASSIST, PRINT, FAULT, TAIL)),
    phase' = phase.set(x, if (phase.get(x) == TAIL) TAIL_STUCK else PULL),
    pos' = pos,
    owner' = if (owner == x) NO_CH else owner,
    inTail' = if (owner == x) false else inTail,
    keepMask,
    lastAction' = "pullBack",
  }

  // Global idle: refused in TAIL, ends TAIL_STUCK.
  action idleAll: bool = all {
    phase' = CHANNELS.mapBy(c => if (phase.get(c) == TAIL_STUCK) EMPTY else phase.get(c)),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "idleAll",
  }

  // ---------------------------------------------------------------- motion

  // The pull reached its target, or the key read empty: both go to redetect,
  // and the tip is back upstream of the merger (A2).
  action pullDone(x: int): bool = all {
    phase.get(x) == PULL,
    phase' = phase.set(x, REDETECT),
    pos' = pos.set(x, Up),
    owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "pullDone",
  }

  // Redetect found the key: the retract finished. Clear site 1.
  action redetectFound(x: int): bool = all {
    phase.get(x) == REDETECT,
    phase' = phase.set(x, HELD),
    pos' = pos, owner' = owner, inTail' = inTail,
    markReturned(x),
    lastAction' = "redetectFound",
  }

  action redetectTimeout(x: int): bool = all {
    phase.get(x) == REDETECT,
    phase' = phase.set(x, FAULT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "redetectTimeout",
  }

  // No progress, travel budget, or a bus loss that outlasted 3 s.
  action pullFault(x: int): bool = all {
    phase.get(x).in(Set(PULL, PULL_FROZEN)),
    phase' = phase.set(x, FAULT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "pullFault",
  }

  action busLost(x: int): bool = all {
    phase.get(x) == PULL,
    phase' = phase.set(x, PULL_FROZEN),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "busLost",
  }

  action busBack(x: int): bool = all {
    phase.get(x) == PULL_FROZEN,
    phase' = phase.set(x, PULL),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "busBack",
  }

  // ---------------------------------------------------------------- runout

  // Only the extruder consumes filament, and it can only do so with the tip in
  // the head (A1), so a runout needs the strand past the merger.
  action keyEmptyWhilePrinting(x: int): bool = all {
    phase.get(x) == PRINT,
    pos.get(x) == Down,
    phase' = phase.set(x, RUNOUT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "keyEmptyWhilePrinting",
  }

  action keyBack(x: int): bool = all {
    phase.get(x) == RUNOUT,
    phase' = phase.set(x, PRINT),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "keyBack",
  }

  // The inTail has cleared the BMCU: what is left is past the merger only.
  action toTail(x: int): bool = all {
    phase.get(x) == RUNOUT,
    owner == x,
    phase' = phase.set(x, TAIL),
    pos' = pos.set(x, TailDown),
    owner' = owner,
    inTail' = true,
    keepMask,
    lastAction' = "toTail",
  }

  // ---------------------------------------------------------------- loader

  action autoload(x: int): bool = all {
    phase.get(x) == EMPTY,
    pos.get(x) == None,
    phase' = phase.set(x, PARKED),
    pos' = pos.set(x, Up),
    owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "autoload",
  }

  action autoUnloadStart(x: int): bool = all {
    phase.get(x).in(Set(PARKED, HELD)),
    phase' = phase.set(x, UNLOADING),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "autoUnloadStart",
  }

  // The key read empty for the window: pulled out of the loader. Clear site 2.
  action autoUnloadEmpty(x: int): bool = all {
    phase.get(x) == UNLOADING,
    phase' = phase.set(x, EMPTY),
    pos' = pos.set(x, None),
    owner' = owner, inTail' = inTail,
    markReturned(x),
    lastAction' = "autoUnloadEmpty",
  }

  action autoUnloadGiveUp(x: int): bool = all {
    phase.get(x) == UNLOADING,
    phase' = phase.set(x, PARKED),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "autoUnloadGiveUp",
  }

  // A human pulls a parked strand out by hand.
  action handRemoved(x: int): bool = all {
    phase.get(x).in(Set(PARKED, HELD)),
    phase' = phase.set(x, EMPTY),
    pos' = pos.set(x, None),
    owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "handRemoved",
  }

  // A3: the printer or a human clears a inTail piece the BMCU no longer sees.
  action remnantCleared(x: int): bool = all {
    pos.get(x) == TailDown,
    phase.get(x) == EMPTY,
    phase' = phase,
    pos' = pos.set(x, None),
    owner' = owner, inTail' = inTail, keepMask,
    lastAction' = "remnantCleared",
  }

  // ---------------------------------------------------------------- reboot

  // RAM is lost; owner, inTail and down come back from flash. The restored
  // owner resumes in PRINT or TAIL; everyone else is rebuilt from the key.
  action reboot: bool = all {
    phase' = CHANNELS.mapBy(c =>
      if (c == owner) (if (inTail) TAIL else PRINT)
      else if (pos.get(c) == Up or pos.get(c) == Down) PARKED
      else EMPTY),
    pos' = pos, owner' = owner, inTail' = inTail, keepMask,
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
      keyEmptyWhilePrinting(x), keyBack(x), toTail(x),
      all { dice < 4, autoload(x) },
      all { dice < 2, autoUnloadStart(x) }, autoUnloadEmpty(x),
      all { dice < 4, autoUnloadGiveUp(x) },
      all { dice < 1, handRemoved(x) }, remnantCleared(x),
    }
  }

  // ---------------------------------------------------------------- invariants

  // The bit never under-reports: a strand that is physically past the merger
  // always has its bit set. A wrongly-cleared bit is the failure the bit
  // exists to avoid; a wrongly-set one (spurious report) is allowed.
  val downstreamSound: bool =
    CHANNELS.forall(c => isDownstream(pos.get(c)) implies down.contains(c))

  // Two strands past the merger at once is always reported by one of them.
  val collisionReported: bool =
    CHANNELS.forall(a => CHANNELS.forall(b =>
      (a != b and isDownstream(pos.get(a)) and isDownstream(pos.get(b)))
        implies (reported.contains(a) or reported.contains(b))))

  // TAIL only with an owner, and that owner is the channel in TAIL.
  val tailHasOwner: bool =
    inTail implies (owner != NO_CH and phase.get(owner) == TAIL)

  // The merger owner is a channel that is using the tube.
  val ownerIsUsing: bool =
    owner == NO_CH or phase.get(owner).in(Set(PURGE, PRINT, PAUSE, RUNOUT, TAIL))

  // PARKED/HELD/EMPTY never hide a strand past the merger without the bit --
  // the ORPHAN-vs-PARKED question the bit was added to answer. Implied by
  // downstreamSound, stated on its own because it is the reason for it.
  val parkedIsDistinguishable: bool =
    CHANNELS.forall(c =>
      (phase.get(c).in(Set(PARKED, HELD, EMPTY)) and isDownstream(pos.get(c)))
        implies down.contains(c))

  val safety: bool = all {
    downstreamSound, collisionReported, tailHasOwner, ownerIsUsing,
    parkedIsDistinguishable,
  }

  // ---------------------------------------------------------------- witnesses
  //
  // Expected to be VIOLATED. Each is a known behaviour the firmware does not
  // prevent (the printer is master); the check proves the model can reach it,
  // so a model that silently stopped reaching it would be caught.

  // Race 1: two strands past the merger at once (send on Y while X is still
  // pulling back, or never retracted).
  val witnessNoCollision: bool =
    not(CHANNELS.exists(a => CHANNELS.exists(b =>
      a != b and isDownstream(pos.get(a)) and isDownstream(pos.get(b)))))

  // ORPHAN after reboot reads PARKED in every controller variable but down.
  val witnessNoParkedOrphan: bool =
    not(CHANNELS.exists(c => phase.get(c) == PARKED and pos.get(c) == Down))
}
