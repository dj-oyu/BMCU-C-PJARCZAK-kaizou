// -*- mode: Bluespec; -*-
//
// Strand zones (docs/strand_zone_design.md, src/strand_zone_policy.h): the
// firmware's travel-based estimate of where each channel's filament is,
// checked against where it physically is.
//
// Physical side: each channel may hold a strand with a front position, still
// attached to its spool or not, and may have left a tail piece (remnant) in
// the shared tube. Positions are abstract units along the path, measured from
// the inner online switch:
//
//   -2 .. -1   in the BMCU, behind the switch
//    0         at the switch
//    PARK      merger entrance, where autoload parks
//    > J       past the merger junction: in the shared tube
//    HEAD      the nozzle
//
// Firmware side: the estimate, a transcription of strand_zone::update /
// passage_proof / restore. ci/quint_replay.py replays traces of this module
// against the real header, so the transcription is checked, not trusted.
//
// Assumptions (the design document's E1-E4):
//   E1  A measured move is the strand's real move (no slip on the wheel).
//   E2  HEAD is right: travel beyond it is filament consumed at the nozzle.
//   E3  The tube is a single bore: two strands cannot both be past J, and a
//       strand reaching HEAD has pushed any tail piece out ahead of it.
//   E4  The junction is at or beyond down_set: J == SET.
//   A5  Persistence is immediate (a reboot restores the last zones).
module strand_zone {

  // ---------------------------------------------------------------- geometry

  pure val PARK: int = 2
  pure val CLEAR: int = 3     // down_clear
  pure val SET: int = 4       // down_set
  pure val J: int = SET       // E4
  pure val HEAD: int = 7
  pure val BACK_END: int = -2 // further back and the strand is out
  pure val SLACK: int = 0     // measured travel is exact here (E1)

  pure val CHANNELS: Set[int] = 0.to(2)
  pure val NO_CH: int = -1

  // zones
  pure val NONE: int = 0
  pure val UPSTREAM: int = 1
  pure val DOWNSTREAM: int = 2
  pure val TAIL: int = 3
  pure def isDown(z: int): bool = z == DOWNSTREAM or z == TAIL

  // ---------------------------------------------------------------- estimate

  type Est = {
    front: int, lastDir: int, key: bool, known: bool,
    tailKnown: bool, remnant: bool, exact: bool, reachedHead: bool, zone: int,
  }

  pure val EST0: Est = {
    front: 0, lastDir: 0, key: false, known: false,
    tailKnown: false, remnant: false, exact: false, reachedHead: false, zone: NONE,
  }

  pure def clamp(v: int, lo: int, hi: int): int =
    if (v < lo) lo else if (v > hi) hi else v

  pure def deriveZone(e: Est): int =
    if (e.remnant) TAIL
    else if (e.tailKnown) (if (e.known and e.front > CLEAR) TAIL else UPSTREAM)
    else if (not(e.known)) (if (e.key) UPSTREAM else NONE)
    else if (e.zone == DOWNSTREAM) (if (e.front < CLEAR) UPSTREAM else DOWNSTREAM)
    else if (e.front > SET) DOWNSTREAM
    else UPSTREAM

  // strand_zone::update
  pure def upd(e0: Est, delta: int, measured: bool, drivingFwd: bool, keyNow: bool): Est = {
    // travel
    val e1 =
      if (measured) {
        if (delta == 0) e0
        else {
          val f = if (e0.known) clamp(e0.front + delta, -HEAD, HEAD) else e0.front
          { ...e0,
            lastDir: if (delta > 0) 1 else -1,
            front: f,
            reachedHead: if (not(e0.known)) e0.reachedHead
                         else if (delta < 0) false
                         else if (e0.exact and f >= HEAD) true
                         else e0.reachedHead }
        }
      } else if (drivingFwd) {
        { ...e0, known: true, exact: false, reachedHead: false, lastDir: 1,
          front: if (e0.front <= SET) SET + 1 else e0.front }
      } else e0
    // switch edges
    val e2 =
      if (keyNow and not(e1.key)) {
        if (e1.tailKnown) {
          val r = { ...e1, remnant: e1.remnant or deriveZone(e1) == TAIL, tailKnown: false,
                    reachedHead: false }
          if (not(r.known) or r.front <= 0) { ...r, known: true, front: 0, exact: true }
          else { ...r, exact: false }
        }
        else if (not(e1.known) or e1.front <= 0)
          { ...e1, known: true, front: 0, exact: true, reachedHead: false }
        else e1
      } else if (not(keyNow) and e1.key) {
        if (e1.lastDir > 0) { ...e1, tailKnown: true }
        else if (e1.lastDir < 0 and (not(e1.known) or e1.front <= SLACK))
          { ...e1, known: true, exact: true, front: if (e1.front > 0) 0 else e1.front }
        else e1
      } else e1
    val e3 = { ...e2, key: keyNow }
    { ...e3, zone: deriveZone(e3) }
  }

  pure def atHead(e: Est): bool =
    e.reachedHead and e.known and not(e.tailKnown) and not(e.remnant) and e.front >= HEAD

  // strand_zone::passage_proof, applied to every channel but `ch`.
  pure def proofOne(e: Est): Est = {
    val e1 = { ...e, remnant: false, tailKnown: false, reachedHead: false,
               exact: if (e.known and e.front > SET) false else e.exact,
               front: if (e.known and e.front > SET) SET else e.front,
               zone: if (e.zone == DOWNSTREAM) UPSTREAM else e.zone }
    { ...e1, zone: deriveZone(e1) }
  }

  // strand_zone::restore
  pure def restoreOne(z0: int, keyNow: bool, owner: bool): Est = {
    val z = if (owner and z0 != TAIL) DOWNSTREAM else z0
    val e1 = { ...EST0, key: keyNow }
    val e2 =
      if (z == DOWNSTREAM) { ...e1, known: true, front: HEAD }
      else if (z == TAIL) { ...e1, remnant: true, known: keyNow, front: if (keyNow) SET else 0 }
      else { ...e1, known: keyNow, front: if (keyNow) SET else 0 }
    { ...e2, zone: deriveZone(e2) }
  }

  // One sample on channel x, then the passage proof if x is at the head.
  pure def sample(est: int -> Est, x: int, delta: int, measured: bool, drivingFwd: bool,
                  keyNow: bool): int -> Est = {
    val ex = upd(est.get(x), delta, measured, drivingFwd, keyNow)
    val withX = est.set(x, ex)
    if (atHead(ex)) CHANNELS.mapBy(c => if (c == x) ex else proofOne(withX.get(c)))
    else withX
  }

  // ---------------------------------------------------------------- state

  // physical
  var has: int -> bool       // the channel holds a strand
  var front: int -> int      // its front position (meaningful when has)
  var spool: int -> bool     // still attached to its spool
  var rem: int -> bool       // a tail piece of this channel is in the tube
  var asGood: int -> bool    // AS5600 healthy
  // firmware
  var est: int -> Est
  var stored: int -> int     // zones as last persisted (A5)
  var owner: int
  // trace metadata for the replay: what the firmware saw this step
  var lastAction: str
  var lastCh: int
  var lastDelta: int
  var lastMeasured: bool
  var lastDriving: bool

  pure def keyOf(h: bool, f: int): bool = h and f >= 0

  val physDown: int -> bool =
    CHANNELS.mapBy(c => (has.get(c) and front.get(c) > J) or rem.get(c))

  action meta(name: str, x: int, d: int, m: bool, drv: bool): bool = all {
    lastAction' = name, lastCh' = x, lastDelta' = d, lastMeasured' = m, lastDriving' = drv,
  }

  action persist(e: int -> Est): bool =
    stored' = CHANNELS.mapBy(c => e.get(c).zone)

  // ---------------------------------------------------------------- init

  action init = all {
    has' = CHANNELS.mapBy(c => false),
    front' = CHANNELS.mapBy(c => BACK_END),
    spool' = CHANNELS.mapBy(c => false),
    rem' = CHANNELS.mapBy(c => false),
    asGood' = CHANNELS.mapBy(c => true),
    est' = CHANNELS.mapBy(c => EST0),
    stored' = CHANNELS.mapBy(c => NONE),
    owner' = NO_CH,
    meta("init", NO_CH, 0, true, false),
  }

  // ---------------------------------------------------------------- physics

  // A new strand fed in by hand, behind the switch.
  action insert(x: int): bool = all {
    not(has.get(x)),
    has' = has.set(x, true),
    front' = front.set(x, BACK_END),
    spool' = spool.set(x, true),
    rem' = rem, asGood' = asGood, est' = est, stored' = stored, owner' = owner,
    meta("insert", x, 0, true, false),
  }

  // Forward by one unit, whatever drives it (send, hold_load, Stage 2, idle
  // feed, the extruder). At HEAD the front stays and the travel is consumed.
  // E3: nothing enters the tube while another strand is in it; a tail piece
  // is pushed ahead and leaves at the nozzle when this front gets there.
  action fwd(x: int): bool = all {
    has.get(x),
    val f2 = if (front.get(x) < HEAD) front.get(x) + 1 else HEAD
    all {
      f2 <= J or CHANNELS.forall(c => c == x or not(has.get(c) and front.get(c) > J)),
      front' = front.set(x, f2),
      rem' = if (f2 >= HEAD) CHANNELS.mapBy(c => false) else rem,
      val m = asGood.get(x)
      val e = sample(est, x, 1, m, not(m), keyOf(true, f2))
      all { est' = e, persist(e) },
    },
    has' = has, spool' = spool, asGood' = asGood, owner' = owner,
    meta("fwd", x, 1, asGood.get(x), not(asGood.get(x))),
  }

  // Back by one unit (a retract, a human pulling). Past BACK_END the strand
  // is out of the channel.
  action back(x: int): bool = all {
    has.get(x),
    val f2 = front.get(x) - 1
    val out = f2 < BACK_END
    all {
      has' = has.set(x, not(out)),
      front' = front.set(x, if (out) BACK_END else f2),
      spool' = if (out) spool.set(x, false) else spool,
      val m = asGood.get(x)
      val e = sample(est, x, -1, m, false, keyOf(not(out), f2))
      all { est' = e, persist(e) },
    },
    rem' = rem, asGood' = asGood, owner' = owner,
    meta("back", x, -1, asGood.get(x), false),
  }

  // The spool ran out: the tail passes the switch on a forward move. What is
  // left becomes a tail piece in the tube if its front was past the junction.
  action runout(x: int): bool = all {
    has.get(x), spool.get(x), front.get(x) >= 0,
    has' = has.set(x, false),
    spool' = spool.set(x, false),
    rem' = rem.set(x, rem.get(x) or front.get(x) > J),
    front' = front.set(x, BACK_END),
    val m = asGood.get(x)
    val e = sample(est, x, 1, m, not(m), false)
    all { est' = e, persist(e) },
    asGood' = asGood, owner' = owner,
    meta("runout", x, 1, asGood.get(x), not(asGood.get(x))),
  }

  // The printer purged a tail piece (or a human cleared the tube). Nothing
  // the BMCU can see.
  action consume(x: int): bool = all {
    rem.get(x),
    rem' = rem.set(x, false),
    has' = has, front' = front, spool' = spool, asGood' = asGood,
    est' = est, stored' = stored, owner' = owner,
    meta("consume", x, 0, true, false),
  }

  // A lever float the debounce let through: the switch reads empty for one
  // sample and back, with no travel. Two firmware samples.
  action float(x: int): bool = all {
    keyOf(has.get(x), front.get(x)),
    val e1 = sample(est, x, 0, true, false, false)
    val e2 = sample(e1, x, 0, true, false, true)
    all { est' = e2, persist(e2) },
    has' = has, front' = front, spool' = spool, rem' = rem, asGood' = asGood, owner' = owner,
    meta("float", x, 0, true, false),
  }

  action asFail(x: int): bool = all {
    asGood.get(x),
    asGood' = asGood.set(x, false),
    has' = has, front' = front, spool' = spool, rem' = rem,
    est' = est, stored' = stored, owner' = owner,
    meta("asFail", x, 0, true, false),
  }

  action asRecover(x: int): bool = all {
    not(asGood.get(x)),
    asGood' = asGood.set(x, true),
    has' = has, front' = front, spool' = spool, rem' = rem,
    est' = est, stored' = stored, owner' = owner,
    meta("asRecover", x, 0, true, false),
  }

  // ---------------------------------------------------------------- firmware

  // The merger was acquired by x (before_on_use / on_use): owner implies
  // downstream (Motion_control_zone_claimed).
  action claim(x: int): bool = all {
    owner == NO_CH or owner == x,
    owner' = x,
    val ex = est.get(x)
    val e = if (isDown(ex.zone)) est else est.set(x, restoreOne(DOWNSTREAM, ex.key, true))
    all { est' = e, persist(e) },
    has' = has, front' = front, spool' = spool, rem' = rem, asGood' = asGood,
    meta("claim", x, 0, true, false),
  }

  action release: bool = all {
    owner != NO_CH,
    owner' = NO_CH,
    has' = has, front' = front, spool' = spool, rem' = rem, asGood' = asGood,
    est' = est, stored' = stored,
    meta("release", NO_CH, 0, true, false),
  }

  // RAM lost; zones come back from flash with the switch assumed to read
  // filament for any stored strand (Motion_control_zone_restore).
  action reboot: bool = all {
    val e = CHANNELS.mapBy(c => restoreOne(stored.get(c), stored.get(c) != NONE, owner == c))
    all { est' = e, persist(e) },
    has' = has, front' = front, spool' = spool, rem' = rem, asGood' = asGood, owner' = owner,
    meta("reboot", NO_CH, 0, true, false),
  }

  // ---------------------------------------------------------------- step

  // `dice` weights the random simulator only (see bmcu_merger.qnt).
  action step = {
    nondet x = CHANNELS.oneOf()
    nondet dice = 0.to(19).oneOf()
    any {
      all { dice < 3, insert(x) },
      fwd(x), fwd(x), back(x),
      all { dice < 2, runout(x) },
      all { dice < 3, consume(x) },
      all { dice < 2, float(x) },
      all { dice < 1, asFail(x) },
      all { dice < 4, asRecover(x) },
      all { dice < 3, claim(x) },
      all { dice < 3, release },
      all { dice < 2, reboot },
    }
  }

  // ---------------------------------------------------------------- invariants

  // Never under-reports: anything physically past the junction reads as
  // downstream.
  val sound: bool =
    CHANNELS.forall(c => physDown.get(c) implies isDown(est.get(c).zone))

  // Physics sanity (E3): at most one strand in the tube.
  val singleBore: bool =
    CHANNELS.forall(a => CHANNELS.forall(b =>
      (a != b and has.get(a) and has.get(b)) implies not(front.get(a) > J and front.get(b) > J)))

  val safety: bool = sound and singleBore

  // ---------------------------------------------------------------- witnesses
  // Expected to be VIOLATED: the estimate is allowed to over-report, and the
  // model must still reach these.

  // A spurious downstream (a hand-removed strand, an unseen-travel guess, a
  // float read as a tail).
  val witnessNeverSpurious: bool =
    CHANNELS.forall(c => isDown(est.get(c).zone) implies physDown.get(c))

  // A remnant cleared by another channel's passage.
  val witnessNoProofClear: bool =
    not(CHANNELS.exists(c => est.get(c).zone == UPSTREAM and lastAction == "fwd" and
      lastCh != c and atHead(est.get(lastCh)) and stored.get(c) == UPSTREAM))

  // Two channels downstream at once: a collision the report exists for.
  val witnessNoDoubleDown: bool =
    CHANNELS.forall(a => CHANNELS.forall(b =>
      (a != b) implies not(physDown.get(a) and physDown.get(b))))
}
