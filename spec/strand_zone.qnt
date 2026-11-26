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
//   A6  A zone record is lost (loseRecord) only with no tail piece anywhere.
//
// A second module at the end of this file, outlet_guard, specifies the
// open-outlet guard and its interaction with jam detection
// (docs/FEED_RESISTANCE_DESIGN.md); not implemented, not replayed. Select it
// with --main=outlet_guard.
module strand_zone {

  // ---------------------------------------------------------------- geometry

  pure val PARK: int = 2
  pure val CLEAR: int = 3     // down_clear
  pure val SET: int = 4       // down_set
  pure val J: int = SET       // E4
  pure val HEAD: int = 7
  pure val BACK_END: int = -2 // further back and the strand is out
  pure val KEY_BACK: int = -1 // the switch opens going back between 0 and -1
  pure val CONSUME: int = 1   // travel past HEAD, while consuming, that proves passage

  pure val CHANNELS: Set[int] = 0.to(2)
  pure val NO_CH: int = -1

  // zones
  pure val NONE: int = 0
  pure val UPSTREAM: int = 1
  pure val DOWNSTREAM: int = 2
  pure val TAIL: int = 3
  pure def isDown(z: int): bool = z == DOWNSTREAM or z == TAIL

  // ---------------------------------------------------------------- estimate

  // lastDir is the sign of the last move: the spec moves in whole units, far
  // larger than the header's dir_band, so the band never holds a direction
  // here (ci/zone_replay_adapter.cpp scales units so that it does not).
  type Est = {
    front: int, bootFront: int, lastDir: int, key: bool, keyValid: bool, known: bool,
    tailKnown: bool, remnant: bool, exact: bool, reachedHead: bool, passage: bool,
    consumed: int, claimed: bool, zone: int,
  }

  pure val EST0: Est = {
    front: 0, bootFront: 0, lastDir: 0, key: false, keyValid: true, known: false,
    tailKnown: false, remnant: false, exact: false, reachedHead: false, passage: false,
    consumed: 0, claimed: false, zone: NONE,
  }

  pure def clamp(v: int, lo: int, hi: int): int =
    if (v < lo) lo else if (v > hi) hi else v

  pure def deriveZone(e: Est): int =
    if (e.remnant) TAIL
    else if (e.tailKnown) (if (e.known and e.front > 0) TAIL else UPSTREAM)
    else if (not(e.known)) (if (e.key) UPSTREAM else NONE)
    else if (e.claimed) DOWNSTREAM
    else if (e.zone == DOWNSTREAM) (if (e.front < CLEAR) UPSTREAM else DOWNSTREAM)
    else if (e.front > SET) DOWNSTREAM
    else UPSTREAM

  // strand_zone::update
  // `consuming`: the printer feeds from this channel (it owns the merger).
  pure def upd(eIn: Est, delta: int, measured: bool, drivingFwd: bool, keyNow: bool,
               consuming: bool): Est = {
    // the first real switch reading after a restore: no edge
    val e0 =
      if (eIn.keyValid) eIn
      else if (keyNow and not(eIn.known))
        { ...eIn, keyValid: true, key: true, known: true, front: eIn.bootFront, exact: false }
      else { ...eIn, keyValid: true, key: keyNow }
    // travel
    val e1 =
      if (measured) {
        if (delta == 0) e0
        else {
          val moved = e0.front + delta
          // a tail piece is pushed on by what follows, never pulled back
          val f = if (e0.known and (not(e0.tailKnown) or delta > 0)) clamp(moved, -HEAD, HEAD)
                  else e0.front
          // travel past the head while consuming, from an exact front
          val gain = if (e0.known and delta > 0 and e0.exact and consuming and moved > HEAD)
                       moved - (if (e0.front > HEAD) e0.front else HEAD)
                     else 0
          val c2 = if (not(e0.known)) e0.consumed else if (delta < 0) 0 else e0.consumed + gain
          val arrives = gain > 0 and not(e0.reachedHead) and c2 >= CONSUME
          { ...e0,
            lastDir: if (delta > 0) 1 else -1,
            front: f,
            consumed: c2,
            reachedHead: if (not(e0.known)) e0.reachedHead
                         else if (delta < 0) false
                         else if (arrives) true
                         else e0.reachedHead,
            passage: e0.passage or arrives }
        }
      } else if (drivingFwd) {
        { ...e0, known: true, exact: false, reachedHead: false, passage: false, consumed: 0,
          lastDir: 1,
          front: if (not(e0.known) or e0.front < SET + (SET - CLEAR)) SET + (SET - CLEAR)
                 else e0.front }
      } else { ...e0, exact: false, reachedHead: false, passage: false, consumed: 0 }
    // switch edges
    val e2 =
      if (keyNow and not(e1.key)) {
        if (e1.tailKnown) {
          val r = { ...e1, remnant: e1.remnant or (e1.known and e1.front > 0),
                    tailKnown: false, reachedHead: false, passage: false, consumed: 0 }
          if (not(r.known) or r.front <= 0) { ...r, known: true, front: 0, exact: true }
          else { ...r, exact: false }
        }
        else if (not(e1.known) or e1.front <= 0)
          { ...e1, known: true, front: 0, exact: true, reachedHead: false, passage: false,
            consumed: 0 }
        else e1
      } else if (not(keyNow) and e1.key) {
        if (e1.lastDir > 0) { ...e1, tailKnown: true }
        else if (e1.lastDir < 0 and (not(e1.known) or e1.front <= 0))
          { ...e1, known: true, exact: true, front: KEY_BACK }
        else e1
      } else e1
    val e3 = { ...e2, key: keyNow }
    val e4 = if (e3.claimed and e3.lastDir < 0 and e3.known and e3.front < CLEAR)
               { ...e3, claimed: false }
             else e3
    { ...e4, zone: deriveZone(e4) }
  }

  // strand_zone::take_passage: true once per arrival at the head.
  pure def passes(e: Est): bool =
    e.passage and e.reachedHead and e.known and not(e.tailKnown) and not(e.remnant) and
    e.front >= HEAD

  // strand_zone::passage_proof, applied to every channel but `ch`.
  pure def proofOne(e: Est): Est = {
    val inTube = e.known and e.front > SET
    val e1 = { ...e, remnant: false,
               reachedHead: false, passage: false, consumed: 0, claimed: false,
               exact: if (inTube) false else e.exact,
               front: if (inTube) SET else e.front,
               zone: if (e.zone == DOWNSTREAM) UPSTREAM else e.zone }
    { ...e1, zone: deriveZone(e1) }
  }

  // strand_zone::claim
  pure def claimOne(e: Est): Est =
    if (isDown(e.zone)) e
    else if (e.exact and e.known and not(e.tailKnown)) {
      val e1 = { ...e, claimed: true }
      { ...e1, zone: deriveZone(e1) }
    }
    else {
      val e1 = { ...e, known: true, front: HEAD, exact: false, reachedHead: false,
                 passage: false, consumed: 0, tailKnown: false }
      { ...e1, zone: deriveZone(e1) }
    }

  // strand_zone::restore: the switch is read at the first sample after it.
  pure def restoreOne(z0: int, valid: bool, owner: bool): Est = {
    val z = if (owner and z0 != TAIL) DOWNSTREAM else z0
    val e1 = { ...EST0, key: z != NONE, keyValid: false,
               bootFront: if (valid) SET else HEAD }
    val e2 =
      if (z == DOWNSTREAM) { ...e1, known: true, front: HEAD }
      else if (z == TAIL) { ...e1, remnant: true }
      else e1
    { ...e2, zone: deriveZone(e2) }
  }

  // One sample on channel x, then the passage proof if x just reached the head.
  pure def sample(est: int -> Est, x: int, delta: int, measured: bool, drivingFwd: bool,
                  keyNow: bool, consuming: bool): int -> Est = {
    val ex0 = upd(est.get(x), delta, measured, drivingFwd, keyNow, consuming)
    val ex = { ...ex0, passage: false }
    val withX = est.set(x, ex)
    if (passes(ex0)) CHANNELS.mapBy(c => if (c == x) ex else proofOne(withX.get(c)))
    else withX
  }

  // ---------------------------------------------------------------- state

  // physical
  var has: int -> bool       // the channel holds a strand
  var front: int -> int      // its front position (meaningful when has)
  var spool: int -> bool     // still attached to its spool
  var rem: int -> bool       // a tail piece of this channel is in the tube
  var stub: int -> bool      // a tail piece of this channel is in its own branch
  var asGood: int -> bool    // AS5600 healthy
  // firmware
  var est: int -> Est
  var stored: int -> int     // zones as last persisted (A5)
  var storedValid: bool      // the zone record survived (no page erase, not a first boot)
  var owner: int
  // trace metadata for the replay: what the firmware saw this step
  var lastAction: str
  var lastCh: int
  var lastDelta: int
  var lastMeasured: bool
  var lastDriving: bool
  var lastProof: bool        // this step's passage proof cleared another channel

  pure def keyOf(h: bool, f: int): bool = h and f >= 0

  val physDown: int -> bool =
    CHANNELS.mapBy(c => (has.get(c) and front.get(c) > J) or rem.get(c))

  action meta(name: str, x: int, d: int, m: bool, drv: bool): bool = all {
    lastAction' = name, lastCh' = x, lastDelta' = d, lastMeasured' = m, lastDriving' = drv,
  }

  // lastProof for actions that sample nothing.
  action noProof: bool = lastProof' = false

  // strand_zone::stored_zone: written ahead through the hysteresis band.
  pure def storedZone(e: Est): int =
    if (e.zone == UPSTREAM and not(e.tailKnown) and e.known and e.exact and e.front > CLEAR)
      DOWNSTREAM
    else e.zone

  action persist(e: int -> Est): bool = all {
    stored' = CHANNELS.mapBy(c => storedZone(e.get(c))),
    storedValid' = true,
  }

  // est' and stored' after one sample of x, plus lastProof.
  action sampled(x: int, delta: int, measured: bool, drivingFwd: bool, keyNow: bool): bool = {
    val ex0 = upd(est.get(x), delta, measured, drivingFwd, keyNow, owner == x)
    val e = sample(est, x, delta, measured, drivingFwd, keyNow, owner == x)
    all { est' = e, persist(e),
          lastProof' = (passes(ex0) and
            CHANNELS.exists(c => c != x and isDown(est.get(c).zone) and not(isDown(e.get(c).zone)))) }
  }

  // ---------------------------------------------------------------- init

  action init = all {
    has' = CHANNELS.mapBy(c => false),
    front' = CHANNELS.mapBy(c => BACK_END),
    spool' = CHANNELS.mapBy(c => false),
    rem' = CHANNELS.mapBy(c => false),
    stub' = CHANNELS.mapBy(c => false),
    asGood' = CHANNELS.mapBy(c => true),
    est' = CHANNELS.mapBy(c => EST0),
    stored' = CHANNELS.mapBy(c => NONE),
    storedValid' = true,
    owner' = NO_CH,
    lastProof' = false,
    meta("init", NO_CH, 0, true, false),
  }

  // ---------------------------------------------------------------- physics

  // A new strand fed in by hand, behind the switch.
  action insert(x: int): bool = all {
    not(has.get(x)),
    has' = has.set(x, true),
    front' = front.set(x, BACK_END),
    spool' = spool.set(x, true),
    rem' = rem, stub' = stub, asGood' = asGood, est' = est, stored' = stored,
    storedValid' = storedValid, owner' = owner, noProof,
    meta("insert", x, 0, true, false),
  }

  // Forward by one unit, whatever drives it (send, hold_load, Stage 2, idle
  // feed, the extruder). At HEAD the front stays and the travel is consumed.
  // E3: nothing enters the tube while another strand is in it; a tail piece
  // is pushed ahead and leaves at the nozzle when this front gets there. A
  // piece left in this channel's branch is pushed ahead too: from the moment
  // the new front reaches the switch it is counted as in the tube (sooner
  // than it gets there, which only asks more of the estimate).
  action fwd(x: int): bool = all {
    has.get(x),
    val f2 = if (front.get(x) < HEAD) front.get(x) + 1 else HEAD
    val pushed = stub.get(x) and f2 >= 0
    all {
      f2 <= J or CHANNELS.forall(c => c == x or not(has.get(c) and front.get(c) > J)),
      front' = front.set(x, f2),
      rem' = if (f2 >= HEAD) CHANNELS.mapBy(c => false)
             else if (pushed) rem.set(x, true) else rem,
      stub' = if (pushed) stub.set(x, false) else stub,
      val m = asGood.get(x)
      sampled(x, 1, m, not(m), keyOf(true, f2)),
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
      sampled(x, -1, m, false, keyOf(not(out), f2)),
    },
    rem' = rem, stub' = stub, asGood' = asGood, owner' = owner,
    meta("back", x, -1, asGood.get(x), false),
  }

  // The spool ran out: the tail passes the switch on a forward move. What is
  // left becomes a tail piece in the tube if its front was past the junction,
  // or in this channel's branch if it was short of it.
  action runout(x: int): bool = all {
    has.get(x), spool.get(x), front.get(x) >= 0,
    has' = has.set(x, false),
    spool' = spool.set(x, false),
    rem' = rem.set(x, rem.get(x) or front.get(x) > J),
    stub' = stub.set(x, stub.get(x) or (front.get(x) > 0 and front.get(x) <= J)),
    front' = front.set(x, BACK_END),
    val m = asGood.get(x)
    sampled(x, 1, m, not(m), false),
    asGood' = asGood, owner' = owner,
    meta("runout", x, 1, asGood.get(x), not(asGood.get(x))),
  }

  // The printer purged a tail piece (or a human cleared the tube). Nothing
  // the BMCU can see.
  action consume(x: int): bool = all {
    rem.get(x),
    rem' = rem.set(x, false),
    has' = has, front' = front, spool' = spool, stub' = stub, asGood' = asGood,
    est' = est, stored' = stored, storedValid' = storedValid, owner' = owner, noProof,
    meta("consume", x, 0, true, false),
  }

  // A lever float the debounce let through: the switch reads empty for one
  // sample and back, with no travel. Two firmware samples.
  action float(x: int): bool = all {
    keyOf(has.get(x), front.get(x)),
    val e1 = sample(est, x, 0, true, false, false, owner == x)
    val e2 = sample(e1, x, 0, true, false, true, owner == x)
    all { est' = e2, persist(e2), lastProof' = false },
    has' = has, front' = front, spool' = spool, rem' = rem, stub' = stub, asGood' = asGood,
    owner' = owner,
    meta("float", x, 0, true, false),
  }

  action asFail(x: int): bool = all {
    asGood.get(x),
    asGood' = asGood.set(x, false),
    has' = has, front' = front, spool' = spool, rem' = rem, stub' = stub,
    est' = est, stored' = stored, storedValid' = storedValid, owner' = owner, noProof,
    meta("asFail", x, 0, true, false),
  }

  action asRecover(x: int): bool = all {
    not(asGood.get(x)),
    asGood' = asGood.set(x, true),
    has' = has, front' = front, spool' = spool, rem' = rem, stub' = stub,
    est' = est, stored' = stored, storedValid' = storedValid, owner' = owner, noProof,
    meta("asRecover", x, 0, true, false),
  }

  // ---------------------------------------------------------------- firmware

  // The merger was acquired by x (before_on_use / on_use): owner implies
  // downstream (Motion_control_zone_claimed).
  action claim(x: int): bool = all {
    owner == NO_CH or owner == x,
    owner' = x,
    val e = est.set(x, claimOne(est.get(x)))
    all { est' = e, persist(e) },
    has' = has, front' = front, spool' = spool, rem' = rem, stub' = stub, asGood' = asGood,
    noProof,
    meta("claim", x, 0, true, false),
  }

  action release: bool = all {
    owner != NO_CH,
    owner' = NO_CH,
    has' = has, front' = front, spool' = spool, rem' = rem, stub' = stub, asGood' = asGood,
    est' = est, stored' = stored, storedValid' = storedValid, noProof,
    meta("release", NO_CH, 0, true, false),
  }

  // RAM lost; zones come back from flash, and each channel's switch is read
  // once it has settled, before anything moves or is reported
  // (Motion_control_zone_restore, then zone_sample's first reading).
  action reboot: bool = all {
    val e = CHANNELS.mapBy(c => upd(restoreOne(stored.get(c), storedValid, owner == c),
                                    0, true, false, keyOf(has.get(c), front.get(c)), false))
    all { est' = e, persist(e) },
    has' = has, front' = front, spool' = spool, rem' = rem, stub' = stub, asGood' = asGood,
    owner' = owner, noProof,
    meta("reboot", NO_CH, 0, true, false),
  }

  // The zone record is gone: a ring-page erase took it, or this is the first
  // boot of a firmware that keeps one. Only a reboot notices. Assumption A6:
  // no tail piece is left anywhere when that happens -- with the switch empty
  // there is nothing at all to find it by.
  action loseRecord: bool = all {
    CHANNELS.forall(c => not(rem.get(c)) and not(stub.get(c))),
    stored' = CHANNELS.mapBy(c => NONE),
    storedValid' = false,
    has' = has, front' = front, spool' = spool, rem' = rem, stub' = stub, asGood' = asGood,
    est' = est, owner' = owner, noProof,
    meta("loseRecord", NO_CH, 0, true, false),
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
      all { dice < 1, loseRecord },
    }
  }

  // Same actions, weighted towards loads that reach the head and get
  // consumed (the passage proof), for the random simulator: `quint run
  // --step=stepFeed`. The default step rarely gets a strand there.
  action stepFeed = {
    nondet x = CHANNELS.oneOf()
    nondet dice = 0.to(19).oneOf()
    any {
      all { dice < 3, insert(x) },
      fwd(x), fwd(x), fwd(x), fwd(x), fwd(x), fwd(x),
      all { dice < 8, back(x) },
      all { dice < 2, runout(x) },
      all { dice < 3, consume(x) },
      all { dice < 2, float(x) },
      all { dice < 1, asFail(x) },
      all { dice < 6, asRecover(x) },
      all { dice < 12, claim(x) },
      all { dice < 6, release },
      all { dice < 1, reboot },
      all { dice < 1, loseRecord },
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
    not(lastProof)

  // Two channels downstream at once: a collision the report exists for.
  val witnessNoDoubleDown: bool =
    CHANNELS.forall(a => CHANNELS.forall(b =>
      (a != b) implies not(physDown.get(a) and physDown.get(b))))

  // ---------------------------------------------------------------- scenarios
  // Fixed paths through the passage proof, which random traces rarely reach.
  // `quint test` checks each keeps `safety`; ci/quint_replay.py replays their
  // ITF traces against the header like the random ones.

  // Channel 1 runs out with its tail piece in its own branch; channel 0 loads
  // to the head and is consumed. The proof fires, and must keep 1's piece.
  run proofKeepsBranchStubTest =
    init.then(insert(1)).then(fwd(1)).then(fwd(1)).then(fwd(1)).then(fwd(1))
      .then(runout(1))
      .then(insert(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0))
      .then(claim(0))
      .then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0))
      .expect(est.get(0).reachedHead and est.get(1).tailKnown and isDown(est.get(1).zone))
      .then(release).then(insert(1)).then(fwd(1)).then(fwd(1))
      .expect(est.get(1).remnant and safety)

  // Channel 1 leaves a tail piece in the tube; channel 0's passage pushes it
  // out at the nozzle.
  run proofClearsTubeRemnantTest =
    init.then(insert(1)).then(fwd(1)).then(fwd(1)).then(fwd(1)).then(fwd(1))
      .then(fwd(1)).then(fwd(1)).then(fwd(1)).then(runout(1))
      .then(insert(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0))
      .then(claim(0))
      .then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0))
      .expect(not(rem.get(1)) and est.get(0).reachedHead and safety)

  // Channel 1's sensor fails while it is pushed: the estimate guesses
  // downstream though the strand stays in its branch. Channel 0 passes and
  // clears the guess.
  run proofClearsUnseenGuessTest =
    init.then(insert(1)).then(fwd(1)).then(fwd(1)).then(fwd(1)).then(fwd(1))
      .then(asFail(1)).then(fwd(1))
      .expect(est.get(1).zone == DOWNSTREAM)
      .then(asRecover(1)).then(back(1))
      .then(insert(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0))
      .then(claim(0))
      .then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0))
      .expect(lastProof and not(isDown(est.get(1).zone)))

  // A tail piece left in the branch, a new strand fed in and pulled back
  // behind it, a reboot: the piece must still be counted when the new strand
  // reaches the switch and pushes it on. (Found by the CI simulation.)
  run stubSurvivesBackAndRebootTest =
    init.then(insert(2)).then(fwd(2)).then(fwd(2)).then(fwd(2)).then(fwd(2))
      .then(runout(2))
      .then(insert(2)).then(fwd(2)).then(back(2)).then(back(2))
      .then(reboot)
      .then(insert(2)).then(fwd(2)).then(fwd(2))
      .expect(isDown(est.get(2).zone) and safety)

  // The review's stale-head path: channel 0 reached the head, then its sensor
  // failed and it was pulled back unseen. Channel 1 loads into the tube; more
  // samples of channel 0 must not prove anything.
  run staleHeadProvesNothingTest =
    init.then(insert(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0))
      .then(claim(0))
      .then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0)).then(fwd(0))
      .then(asFail(0)).then(back(0)).then(back(0)).then(back(0)).then(back(0))
      .then(release)
      .then(insert(1)).then(fwd(1)).then(fwd(1)).then(fwd(1)).then(fwd(1))
      .then(fwd(1)).then(fwd(1)).then(fwd(1))
      .then(float(0)).then(back(0))
      .expect(isDown(est.get(1).zone) and safety)
}

// ==========================================================================
//
// Open outlet and jam, one channel (docs/FEED_RESISTANCE_DESIGN.md). NOT
// IMPLEMENTED in the firmware yet: this is the design, checked before it is
// written. A module of its own so that the strand_zone traces, which
// ci/quint_replay.py replays against the firmware header, are unchanged:
//
//   quint run spec/strand_zone.qnt --main=outlet_guard --invariant=safety
//   quint test spec/strand_zone.qnt --main=outlet_guard
//
// The physical fact: a PTFE tube can come off the filament hub. The BMCU
// then feeds into nothing, and bare filament comes out of the open end. The
// firmware cannot see the tube. What it can see is that forward travel meets
// no back-pressure. That is normal on exactly one stretch, the send path from
// rest to the extruder, whose length is known (AMS_RETRACT_LEN); anywhere
// else a few mm of push raises the buffer. So the guard counts forward travel
// since the last back-pressure (`free`), and when it exceeds the budget for
// the current mode it stops pushing, latched. Back-pressure coming back is
// the only release, besides a retract that winds the count back to zero.
// "Not taken by the extruder" and "tube off" are the same state to the
// firmware, and the guard does not try to tell them apart.
//
// Jam detection is the resistance-based design of the same document: a jam
// is forward drive that produces (almost) no travel, sustained. Its hazard
// here is the resume: when the guard has stopped the motor and the printer
// consumes the slack, the buffer is stretched at the moment back-pressure
// returns. A detector on the buffer reading (today's < 40 % for 300 ms)
// would read that as a jam; one on drive vs travel cannot, because nothing is
// driven while stopped and the first drive after the resume moves freely.
//
// Positions are abstract units along the path from the channel's rest
// position: 0 rest, HUB the filament hub (where the tube can come off), PATH
// the extruder. Travel past HUB with the outlet open goes into `spill`
// (filament outside the tube) instead of moving the front.
//
// Assumptions:
//   G1  Back-pressure is sensed only when the strand is coupled to something
//       downstream: the extruder holds the tip (front == PATH) and the outlet
//       is closed. Pushing a coupled strand raises the buffer; the extruder
//       pulling a coupled strand stretches it (`tension`). Either is contact.
//   G2  With the AS5600 good, `free` counts real forward travel. With it bad,
//       it counts time x commanded speed, which is at least the real travel
//       (it also counts while stuck): the guard trips early, never late.
//   G3  The printer consumes only a strand its extruder holds. With the
//       outlet open it first takes up the spill; once that is gone the tube
//       may be drawn back onto the hub (`reseat`, nondeterministic: it may
//       never happen).
//   G4  A retract unit removes spill first, then moves the front back.
//   G5  One firmware sample per action; a trip is evaluated on every sample
//       and on every mode change.
module outlet_guard {

  // ---------------------------------------------------------------- geometry

  pure val HUB: int = 1
  pure val PATH: int = 6          // AMS_RETRACT_LEN: rest -> extruder
  pure val MARGIN: int = 2        // send path tolerance
  pure val USE_FREE: int = 1      // on_use and every other forward motion
  pure val JAM_N: int = 2         // drive samples with no travel that latch a jam

  type Mode = IDLE | SEND | USE | PULL

  // The budget of free travel a forward mode allows. IDLE and PULL feed
  // nothing forward (an idle feed, a DM re-push or hold_load is USE here).
  pure def budget(m: Mode): int = match m {
    | SEND => PATH + MARGIN
    | USE => USE_FREE
    | IDLE => USE_FREE
    | PULL => USE_FREE
  }
  pure val MAX_BUDGET: int = PATH + MARGIN

  // ---------------------------------------------------------------- state

  // physical
  var front: int          // 0 .. PATH
  var spill: int          // filament pushed out of an open outlet
  var outletOpen: bool    // tube off the hub (or: tip not taken, same thing)
  var stuck: bool         // the strand is held upstream (spool clamped)
  var tension: bool       // the extruder pulled since the BMCU last fed
  var asGood: bool
  // firmware
  var mode: Mode          // the printer's command for this channel
  var free: int           // forward travel since the last back-pressure
  var tripped: bool       // open-outlet latch: forward drive stopped
  var jam: bool           // jam latch: forward drive stopped
  var jamEv: int          // consecutive drive samples with no travel
  var jamWasStuck: bool   // the strand was held when the jam latched
  var lastAction: str

  pure def coupledOf(f: int, open: bool): bool = f == PATH and not(open)
  val coupled: bool = coupledOf(front, outletOpen)

  action keepPhys: bool = all {
    front' = front, spill' = spill, outletOpen' = outletOpen, stuck' = stuck,
    tension' = tension, asGood' = asGood,
  }

  // ---------------------------------------------------------------- init

  action init = all {
    front' = 0, spill' = 0, outletOpen' = false, stuck' = false, tension' = false,
    asGood' = true,
    mode' = IDLE, free' = 0, tripped' = false, jam' = false, jamEv' = 0,
    jamWasStuck' = false, lastAction' = "init",
  }

  // ---------------------------------------------------------------- printer

  // send_out: clears the jam latch unconditionally (346783f). The guard
  // latch stays: a repeated send_out must not buy another budget.
  action cmdSend: bool = all {
    mode' = SEND,
    jam' = false, jamEv' = 0,
    tripped' = (tripped or free > budget(SEND)),
    free' = free, jamWasStuck' = false,
    keepPhys,
    lastAction' = "cmdSend",
  }

  // before_on_use / on_use: the budget drops to USE_FREE at once. A load that
  // was never taken (no contact since the send) trips here. Not while a jam
  // holds: nothing travels then, and the jam is the better explanation; the
  // count is judged again once the printer's recovery clears the jam.
  action cmdUse: bool = all {
    mode' = USE,
    tripped' = (tripped or (not(jam) and free > budget(USE))),
    free' = free, jam' = jam, jamEv' = jamEv, jamWasStuck' = jamWasStuck,
    keepPhys,
    lastAction' = "cmdUse",
  }

  // before_pull_back / pull_back: clears the jam latch (346783f).
  action cmdPull: bool = all {
    mode' = PULL,
    jam' = false, jamEv' = 0,
    tripped' = tripped, free' = free, jamWasStuck' = false,
    keepPhys,
    lastAction' = "cmdPull",
  }

  action cmdIdle: bool = all {
    mode' = IDLE,
    tripped' = tripped, free' = free, jam' = jam, jamEv' = jamEv,
    jamWasStuck' = jamWasStuck,
    keepPhys,
    lastAction' = "cmdIdle",
  }

  // ---------------------------------------------------------------- firmware

  // Forward drive is allowed only in SEND / USE, never while a latch holds.
  val driveEnabled: bool = (mode == SEND or mode == USE) and not(tripped) and not(jam)

  // One forward drive sample.
  action drive: bool = all {
    driveEnabled,
    if (stuck) all {
      // no travel; the encoder sees it, time x speed does not (G2)
      front' = front, spill' = spill, tension' = tension,
      val f2 = if (asGood) free else free + 1
      val ev = if (asGood) jamEv + 1 else 0
      all {
        free' = f2,
        jamEv' = ev,
        jam' = (ev >= JAM_N),
        jamWasStuck' = if (ev >= JAM_N) stuck else jamWasStuck,
        tripped' = (f2 > budget(mode)),
      },
      lastAction' = if (asGood and jamEv + 1 >= JAM_N) "jamLatch" else "driveStuck",
    }
    else if (coupledOf(front, outletOpen)) all {
      // pushing a coupled strand: the buffer rises, contact
      front' = front, spill' = spill, tension' = false,
      free' = 0, jamEv' = 0, jam' = false, jamWasStuck' = jamWasStuck, tripped' = false,
      lastAction' = "drivePressed",
    }
    else all {
      // free travel: along the path, or out of the open outlet
      val toSpill = outletOpen and front >= HUB
      all {
        front' = if (toSpill) front else front + 1,
        spill' = if (toSpill) spill + 1 else spill,
      },
      tension' = false,
      free' = free + 1, jamEv' = 0, jam' = false, jamWasStuck' = jamWasStuck,
      tripped' = (free + 1 > budget(mode)),
      lastAction' = if (free + 1 > budget(mode)) "trip" else "driveFree",
    },
    outletOpen' = outletOpen, stuck' = stuck, asGood' = asGood,
    mode' = mode,
  }

  // A sample with the motor not pushing: the extruder pulling a coupled
  // strand is contact too. That is the resume: the latch clears and the jam
  // detector starts from nothing.
  action sense: bool = all {
    coupled, tension,
    free' = 0, tripped' = false, jamEv' = 0,
    jam' = jam, jamWasStuck' = jamWasStuck,
    keepPhys, mode' = mode,
    lastAction' = if (tripped) "resume" else "contact",
  }

  // One retract unit (G4). Winding the count back to zero clears the latch:
  // the strand is where the count started, and the next send has its budget.
  action retract: bool = all {
    mode == PULL,
    front > 0 or spill > 0,
    spill' = if (spill > 0) spill - 1 else spill,
    front' = if (spill > 0) front else front - 1,
    tension' = false,
    val f2 = if (free > 0) free - 1 else 0
    all { free' = f2, tripped' = (tripped and f2 > 0) },
    jam' = jam, jamEv' = jamEv, jamWasStuck' = jamWasStuck,
    outletOpen' = outletOpen, stuck' = stuck, asGood' = asGood, mode' = mode,
    lastAction' = "retract",
  }

  // ---------------------------------------------------------------- physics

  // The tube comes off the hub.
  action detach: bool = all {
    not(outletOpen),
    outletOpen' = true,
    front' = front, spill' = spill, stuck' = stuck, tension' = tension, asGood' = asGood,
    mode' = mode, free' = free, tripped' = tripped, jam' = jam, jamEv' = jamEv,
    jamWasStuck' = jamWasStuck,
    lastAction' = "detach",
  }

  // The printer extrudes one unit (G3).
  action consume: bool = all {
    mode == USE, front == PATH,
    if (outletOpen) all {
      spill > 0,
      spill' = spill - 1, outletOpen' = true, tension' = tension,
      lastAction' = "consumeSpill",
    } else all {
      spill' = spill, outletOpen' = false, tension' = true,
      lastAction' = "consume",
    },
    front' = front, stuck' = stuck, asGood' = asGood,
    mode' = mode, free' = free, tripped' = tripped, jam' = jam, jamEv' = jamEv,
    jamWasStuck' = jamWasStuck,
  }

  // With the spill taken up, consumption draws the tube back onto the hub;
  // the extruder is now pulling against the BMCU (G3).
  action reseat: bool = all {
    outletOpen, spill == 0, mode == USE, front == PATH,
    outletOpen' = false, tension' = true,
    front' = front, spill' = spill, stuck' = stuck, asGood' = asGood,
    mode' = mode, free' = free, tripped' = tripped, jam' = jam, jamEv' = jamEv,
    jamWasStuck' = jamWasStuck,
    lastAction' = "reseat",
  }

  // A human puts the tube back (the send-path case: nothing is consumed).
  action reattach: bool = all {
    outletOpen, spill == 0,
    outletOpen' = false,
    front' = front, spill' = spill, stuck' = stuck, tension' = tension, asGood' = asGood,
    mode' = mode, free' = free, tripped' = tripped, jam' = jam, jamEv' = jamEv,
    jamWasStuck' = jamWasStuck,
    lastAction' = "reattach",
  }

  action stick: bool = all {
    not(stuck), stuck' = true,
    front' = front, spill' = spill, outletOpen' = outletOpen, tension' = tension,
    asGood' = asGood,
    mode' = mode, free' = free, tripped' = tripped, jam' = jam, jamEv' = jamEv,
    jamWasStuck' = jamWasStuck,
    lastAction' = "stick",
  }

  action unstick: bool = all {
    stuck, stuck' = false,
    front' = front, spill' = spill, outletOpen' = outletOpen, tension' = tension,
    asGood' = asGood,
    mode' = mode, free' = free, tripped' = tripped, jam' = jam, jamEv' = jamEv,
    jamWasStuck' = jamWasStuck,
    lastAction' = "unstick",
  }

  action asToggle: bool = all {
    asGood' = not(asGood),
    front' = front, spill' = spill, outletOpen' = outletOpen, stuck' = stuck,
    tension' = tension,
    mode' = mode, free' = free, tripped' = tripped, jam' = jam, jamEv' = jamEv,
    jamWasStuck' = jamWasStuck,
    lastAction' = "asToggle",
  }

  // ---------------------------------------------------------------- step

  action step = {
    nondet dice = 0.to(19).oneOf()
    any {
      all { dice < 3, cmdSend }, all { dice < 4, cmdUse },
      all { dice < 2, cmdPull }, all { dice < 1, cmdIdle },
      drive, drive, sense, retract,
      all { dice < 2, detach }, consume, reseat,
      all { dice < 2, reattach },
      all { dice < 1, stick }, all { dice < 3, unstick },
      all { dice < 1, asToggle },
    }
  }

  // ---------------------------------------------------------------- invariants

  // Free feed never grows past the budget: unlatched in a forward mode, the
  // count is within that mode's budget; latched, nothing feeds, so it is
  // never more than one sample past the largest budget.
  val freeBounded: bool = all {
    (not(tripped) and not(jam) and (mode == SEND or mode == USE)) implies free <= budget(mode),
    free <= MAX_BUDGET + 1,
  }

  // The count never under-reads what came out of the open outlet (G2), so
  // the spill is bounded by the same budget.
  val spillCounted: bool = spill <= free

  // The two latches are disjoint: a jam means no travel, an open outlet means
  // travel with nothing against it. Neither can latch while the other holds
  // the motor.
  val latchesDisjoint: bool = not(jam and tripped)

  // A jam never latches without a held strand, in particular not at the
  // resume, where the buffer is stretched by the printer's pull.
  val jamSound: bool = jam implies jamWasStuck

  // The printer's send_out / pull releases a jam (346783f), whatever the
  // buffer reads.
  val jamReleasedByCommand: bool =
    (lastAction == "cmdSend" or lastAction == "cmdPull") implies not(jam)

  // Contact always clears the guard: resume needs no human.
  val resumeOnContact: bool =
    (lastAction == "resume" or lastAction == "contact" or lastAction == "drivePressed")
      implies (not(tripped) and free == 0)

  val safety: bool = all {
    freeBounded, spillCounted, latchesDisjoint, jamSound, jamReleasedByCommand,
    resumeOnContact,
  }

  // ---------------------------------------------------------------- witnesses
  // Expected to be VIOLATED.

  // The guard trips.
  val witnessNoTrip: bool = not(tripped)

  // It resumes on contact, with no human step.
  val witnessNoResume: bool = lastAction != "resume"

  // At the resume the buffer is stretched: a detector on the buffer reading
  // would latch a jam here, on a strand that is not held.
  val witnessNoStretchedResume: bool =
    not(lastAction == "resume" and tension and not(stuck))

  // A real jam still latches.
  val witnessNoJam: bool = not(jam)

  // On the send path the spill reaches past the on_use budget: the price of
  // the one stretch where free travel is normal.
  val witnessSmallSpill: bool = spill <= USE_FREE + 1

  // ---------------------------------------------------------------- scenarios

  // The tube comes off mid-print: the guard stops within the on_use budget,
  // the printer eats the slack, the tube reseats and feeding resumes with no
  // jam.
  run detachMidPrintTest =
    init.then(cmdSend)
      .then(drive).then(drive).then(drive).then(drive).then(drive).then(drive)
      .then(drive)
      .expect(lastAction == "drivePressed" and free == 0)
      .then(cmdUse).then(detach)
      .then(drive).then(drive)
      .expect(tripped and spill == 2 and lastAction == "trip")
      .expect(not(driveEnabled))
      .then(consume).then(consume).then(reseat).then(sense)
      .expect(lastAction == "resume" and not(tripped) and not(jam) and tension)
      .then(drive)
      .expect(lastAction == "drivePressed" and not(jam) and safety)

  // A load the extruder never takes (or a tube off before the send): the
  // spill stops at the send budget, a retract winds it back, and after the
  // tube is put back the next send has its full budget.
  run notTakenTest =
    init.then(detach).then(cmdSend)
      .then(drive).then(drive).then(drive).then(drive).then(drive).then(drive)
      .then(drive).then(drive).then(drive)
      .expect(tripped and spill == PATH + MARGIN and free == PATH + MARGIN + 1)
      .then(cmdSend).expect(tripped and not(driveEnabled))
      .then(cmdPull)
      .then(retract).then(retract).then(retract).then(retract).then(retract)
      .then(retract).then(retract).then(retract).then(retract)
      .expect(not(tripped) and free == 0 and spill == 0 and front == 0)
      .then(reattach).then(cmdSend)
      .then(drive).then(drive).then(drive).then(drive).then(drive).then(drive)
      .then(drive)
      .expect(lastAction == "drivePressed" and safety)

  // A held spool: the jam latches with no travel, the guard does not; the
  // printer's send_out clears it.
  run jamIsNotOpenTest =
    init.then(cmdSend)
      .then(drive).then(drive).then(drive).then(drive).then(drive).then(drive)
      .then(drive).then(cmdUse).then(stick).then(drive).then(drive)
      .expect(jam and not(tripped) and free == 0)
      .then(unstick).then(cmdSend)
      .expect(not(jam) and safety)
}
