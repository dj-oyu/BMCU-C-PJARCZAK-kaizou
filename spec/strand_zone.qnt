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
          // unseen forward travel can have reached the head
          front: HEAD }
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
// Open outlet and jam, one channel (docs/FEED_RESISTANCE_DESIGN.md §2). A
// transcription of src/outlet_guard_policy.h (4b552c9) and of how
// Motion_control.cpp uses it, checked against a physical model. A module of
// its own so that the strand_zone traces, which ci/quint_replay.py replays
// against the firmware header, are unchanged; nothing replays this one yet.
//
//   quint test spec/strand_zone.qnt --main=outlet_guard
//   quint run  spec/strand_zone.qnt --main=outlet_guard --invariant=safety
//
// The physical fact: a PTFE tube can come off the filament hub, and the BMCU
// then feeds into nothing: bare filament comes out of the gap. The buffer
// cannot see it -- printing reads the same 50-52 % as free travel (AMS-B ch3,
// 2026-11-27: PWM ~600, 1.1 mm/s, pull 51 %). Travel can: a printing
// extruder takes at most CONS per sample (the consumption ceiling, 12 mm/s),
// so the firmware counts travel in excess of that (`excess`): forward travel
// adds, outside send_out the ceiling drains it, never below zero. send_out
// is the one stretch where free travel is normal; there nothing drains, and
// the budget is the switch-to-head distance plus margin.
//
// Back-pressure resets the count: the buffer pressed (>= 55 %), or pulled
// (< 47 %) while the motor is not pushing. Leaving send_out for anything but
// a retract resets count and latch: the printer moves on to before_on_use
// only once its own sensor has the filament, and a send that ends any other
// way is the printer's to retry, with a fresh budget. A retract keeps the
// count and winds it back; reaching zero clears the latch. Latched, every
// push is braked, and the count is not drained (only a retract may undo it).
// No encoder, no verdict: with the AS5600 bad the count is zero.
//
// Jam detection here is the implemented one, on the buffer: pulled for
// JAM_N samples in on_use latches a jam. It is held off while the guard is
// latched, and after a resume until the buffer is no longer pulled or for
// DISARM_N samples: the resume is a pulled buffer, and not a jam.
//
// Gear slip: forward gear travel while the buffer is pulled or pressed is
// counted on its own (`slipCnt`), drained at CONS outside send_out; past
// SLIP_BUD it latches a slip (`slipped`), which stops the push and is not
// resumed by tension. The motor coasts while it waits: travel with the motor
// unpowered is the strand's own (G2), and past REC_N of it the slip clears
// (`recover`). After MAX_RES recoveries per printer command, or WAIT_N
// samples without one, it is escalated: braked, and the on_use jam raised. A
// command into another mode ends it. A pulled buffer is a coupled strand, so
// it zeroes the open-outlet count.
//
// Abstract units: one sample is one step of time. A push moves the strand up
// to FEED per sample, the extruder takes at most CONS per sample (`credit`).
// Positions run from the key switch (0) to the extruder (PATH); HUB is where
// the tube can come off. The buffer has BUF of stroke (`buf` == BUF is
// pressed) and stretches up to PULL_MAX (`pulled` > 0 is both "pulled" for
// the resume and "low" for the jam detector: the 47 % and 40 % thresholds
// are one here).
//
// Assumptions:
//   G1  Back-pressure exists only on a coupled strand: the extruder holds the
//       tip (front == PATH) and the outlet is closed.
//   G2  The AS5600, when good, measures the drive gear's travel: the
//       strand's, unless the gear slips on a held strand (`slips`), when it
//       reads FEED per push and the strand does not move. Unpowered, the
//       gear turns only with the strand: a coasting motor is back-driven by
//       an extruder pulling a free strand (the motor's spin-down, settle_ms,
//       is not modelled).
//   G3  The printer consumes only a strand its extruder holds. With the
//       outlet open it first takes up the spill; once that is gone the tube
//       may be drawn back onto the hub (`reseat`, nondeterministic).
//   G4  A retract releases the extruder's pull and removes spill first.
//   G5  In on_use the pressure controller pushes whenever the buffer is not
//       pressed and nothing holds the motor; it never idles on a pulled
//       buffer (`idleSample` is not enabled there).
module outlet_guard {

  // ---------------------------------------------------------------- geometry

  pure val HUB: int = 1
  pure val PATH: int = 6          // BMCU_KEY_TO_HEAD_MM (720 mm)
  pure val MARGIN: int = 2        // BMCU_OUTLET_SEND_MARGIN_MM (150 mm)
  pure val USE_BUD: int = 4       // BMCU_OUTLET_USE_MM (40 mm): stroke + margin
  pure val SLIP_BUD: int = 4      // BMCU_OUTLET_SLIP_MM (40 mm)
  pure val BUF: int = 2           // buffer stroke
  pure val PULL_MAX: int = 2
  pure val FEED: int = 2          // push travel per sample
  pure val CONS: int = 1          // BMCU_OUTLET_CONSUME_MM_S per sample
  pure val JAM_N: int = 2         // BMCU_ON_USE_JAM_HOLD_MS in samples
  pure val DISARM_N: int = 3      // the 3 s jam disarm after a resume, in samples
  pure val REC_N: int = 2         // BMCU_SLIP_RECOVER_MM: unpowered travel that clears a slip
  pure val WAIT_N: int = 8        // BMCU_SLIP_WAIT_MS in samples
  pure val MAX_RES: int = 2       // BMCU_SLIP_RESUMES

  // The guard's modes are the motor's (outlet_sample): send, pull, and every
  // other motion that can feed forward (on_use, before_on_use, idle feed).
  type Mode = SEND | USE | PULL

  pure def budget(m: Mode): int = if (m == SEND) PATH + MARGIN else USE_BUD
  pure val MAX_BUDGET: int = PATH + MARGIN

  // Most filament that one count can let out of an open outlet before the
  // trip: travel outpaces the drain by FEED - CONS per sample.
  pure val SPILL_CAP: int = (MAX_BUDGET + FEED) * FEED / (FEED - CONS)

  pure def min(a: int, b: int): int = if (a < b) a else b

  // Most gear travel one slip can grind into a held strand within one
  // printer command. Three stretches at most: while the buffer still reads
  // pressed (a slip count, short of its trip), while it reads at rest (the
  // open-outlet count, longest in send_out with no drain), and once it is
  // pulled and the latch resumes (a slip count to its trip). A slip count
  // outpaces its drain by FEED - CONS per sample.
  pure val SLIP_RUN: int = FEED * (SLIP_BUD / (FEED - CONS) + 1)
  pure val GRIND_CAP: int = FEED * (MAX_BUDGET / FEED + 1) + 2 * SLIP_RUN

  // Grinding is counted net of the consumption ceiling outside send_out:
  // gear travel no faster than the extruder could take it, with the buffer
  // where it is, is what normal feeding looks like to these sensors (a gear
  // slipping at under CONS/FEED duty is not detectable, and grinds at most
  // at the consumption rate).
  pure def grindNet(g: int, m: Mode): int =
    if (m == SEND) g else if (g > CONS) g - CONS else 0

  // ---------------------------------------------------------------- firmware

  type Fw = {
    excess: int, tripped: bool, judged: bool, fromSend: bool,
    slipCnt: int, slipped: bool, resumes: int, slipAge: int, recover: int,
    jam: bool, jamEv: int, disarm: int, jamWasStuck: bool,
  }

  pure val FW0: Fw = {
    excess: 0, tripped: false, judged: true, fromSend: false,
    slipCnt: 0, slipped: false, resumes: 0, slipAge: 0, recover: 0,
    jam: false, jamEv: 0, disarm: -1, jamWasStuck: false,
  }

  // outlet_guard::slip_escalated: a slip that will not clear by itself.
  pure def escalated(fw: Fw): bool =
    fw.slipped and (fw.resumes >= MAX_RES or fw.slipAge >= WAIT_N)

  // A command into a different mode ends a slip (outlet_guard::sample on a
  // mode change).
  pure def endSlip(fw: Fw, m0: Mode, m1: Mode): Fw =
    if (m0 != m1) { ...fw, slipped: false, slipCnt: 0, resumes: 0, slipAge: 0, recover: 0 } else fw

  // outlet_guard::sample, then the on_use jam check of
  // pressure_ctrl_on_use. `pressed` / `pulledNow` are the buffer at the
  // sample, `fwd` the travel since the last one.
  pure def fwSample(fw: Fw, m: Mode, measured: bool, fwd: int, pushing: bool,
                    pressed: bool, pulledNow: bool, stuck: bool): Fw = {
    // back-pressure: pressed, or pulled (a coupled strand)
    val bp0 =
      if (pressed or pulledNow)
        { ...fw, excess: 0,
          tripped: fw.tripped and pulledNow and pushing,
          disarm: if (fw.tripped and pulledNow and not(pushing)) 0 else fw.disarm }
      else fw
    val deflected = pulledNow or pressed
    val bp = if (deflected) bp0 else { ...bp0, slipCnt: 0 }
    val drainOf = if (m != SEND) CONS else 0
    // the counts
    val t =
      if (not(measured)) { ...bp, excess: 0, slipCnt: 0,
                           slipAge: if (bp.slipped) bp.slipAge + 1 else bp.slipAge }
      else if (bp.slipped) {
        // latched: unpowered forward travel is the strand moving
        val r = if (not(pushing) and fwd > 0 and not(escalated(bp))) bp.recover + fwd else bp.recover
        if (r > REC_N)
          { ...bp, slipped: false, resumes: bp.resumes + 1, slipCnt: 0, recover: 0, excess: 0,
                   slipAge: 0, disarm: 0 }
        else { ...bp, recover: r, slipAge: bp.slipAge + 1 }
      }
      else if (deflected) {
        val s1 = bp.slipCnt + (if (fwd > 0) fwd else 0) - drainOf
        val s2 = if (s1 < 0) 0 else s1
        val trip = not(bp.tripped) and s2 > SLIP_BUD
        { ...bp, slipCnt: s2, slipped: trip,
          slipAge: if (trip) 0 else bp.slipAge, recover: if (trip) 0 else bp.recover }
      }
      else {
        val e1 = bp.excess + fwd
        val e2 = if (not(bp.tripped)) e1 - drainOf else e1
        val e3 = if (e2 < 0) 0 else e2
        { ...bp, excess: e3,
          tripped: if (bp.tripped) not(fwd < 0 and e3 == 0)
                   else not(bp.slipped) and e3 > budget(m) }
      }
    // jam_disarmed: ends once the buffer is not pulled, or after DISARM_N
    val dActive = t.disarm >= 0 and pulledNow and t.disarm < DISARM_N
    val held = t.tripped or t.slipped or dActive
    val ev = if (m != USE or held or not(pulledNow) or t.jam) 0 else t.jamEv + 1
    // an escalated slip in on_use raises the jam (outlet_sample)
    val latches = ev >= JAM_N or (m == USE and escalated(t) and not(t.jam))
    { ...t,
      judged: true,
      disarm: if (dActive) t.disarm + 1 else -1,
      jamEv: if (latches) 0 else ev,
      jam: t.jam or latches,
      jamWasStuck: if (latches) (stuck or escalated(t)) else t.jamWasStuck }
  }

  // ---------------------------------------------------------------- state

  // physical
  var front: int
  var spill: int          // filament out of an open outlet
  var outletOpen: bool
  var stuck: bool         // the strand is held upstream (spool clamped)
  var slips: bool         // held, and the gear slips on it instead of stalling
  var buf: int            // buffer compression, BUF = pressed
  var pulled: int         // buffer stretch, > 0 = pulled
  var credit: int         // what the extruder may still take this sample
  var asGood: bool
  // firmware
  var mode: Mode
  var fw: Fw
  // ghosts, for the invariants
  var attemptSpill: int   // spill since the count last started over
  var faultFed: bool      // since then, travel went uncoupled outside send, or out of the gap,
                          // or the gear slipped
  var grind: int          // gear travel ground into a held strand since the last command,
                          // measured (no encoder, no verdict: an unmeasured sample restarts it)
  var lastAction: str

  pure def coupledOf(f: int, open: bool): bool = f == PATH and not(open)

  // The count is back at zero with no latch (contact, a fresh send, an
  // unwind, a drain to zero, or no encoder): the ghosts start over with it.
  pure def restarted(fw2: Fw): bool = fw2.excess == 0 and not(fw2.tripped)

  action keepPhys: bool = all {
    front' = front, spill' = spill, outletOpen' = outletOpen, stuck' = stuck,
    slips' = slips, buf' = buf, pulled' = pulled, credit' = credit, asGood' = asGood,
  }

  action keepGhosts: bool = all {
    attemptSpill' = attemptSpill, faultFed' = faultFed, grind' = grind,
  }

  // ---------------------------------------------------------------- init

  action init = all {
    front' = 0, spill' = 0, outletOpen' = false, stuck' = false, slips' = false,
    buf' = 0, pulled' = 0,
    credit' = CONS, asGood' = true,
    mode' = USE, fw' = FW0,
    attemptSpill' = 0, faultFed' = false, grind' = 0,
    lastAction' = "init",
  }

  // ---------------------------------------------------------------- printer

  // send_out: clears the jam latch unconditionally (346783f). The count and
  // the guard latch stay: a repeated send_out buys no new budget.
  action cmdSend: bool = all {
    mode' = SEND,
    fw' = { ...endSlip(fw, mode, SEND), jam: false, jamEv: 0, jamWasStuck: false,
            fromSend: true, judged: fw.judged and mode == SEND },
    keepPhys, attemptSpill' = attemptSpill, faultFed' = faultFed, grind' = 0,
    lastAction' = "cmdSend",
  }

  // before_on_use / on_use / idle. Leaving send_out this way, directly or
  // through retracts, starts the count over, latch included: the printer has
  // the filament, or will retry. A send's count carried through a retract
  // into on_use tripped a printing strand (noFalseTrip).
  action cmdUse: bool = all {
    mode' = USE,
    val leave = mode == SEND or (mode == PULL and fw.fromSend)
    all {
      fw' = if (leave) { ...endSlip(fw, mode, USE), excess: 0, tripped: false, judged: true, fromSend: false }
            else { ...endSlip(fw, mode, USE), judged: fw.judged and mode == USE },
      attemptSpill' = if (leave) 0 else attemptSpill,
      faultFed' = if (leave) false else faultFed,
    },
    grind' = 0,
    keepPhys,
    lastAction' = "cmdUse",
  }

  // before_pull_back / pull_back: clears the jam latch (346783f). The count
  // is kept, so that winding it back is what clears the guard.
  action cmdPull: bool = all {
    mode' = PULL,
    fw' = { ...endSlip(fw, mode, PULL), jam: false, jamEv: 0, jamWasStuck: false,
            fromSend: fw.fromSend and mode != USE, judged: fw.judged and mode == PULL },
    keepPhys, attemptSpill' = attemptSpill, faultFed' = faultFed, grind' = 0,
    lastAction' = "cmdPull",
  }

  // ---------------------------------------------------------------- firmware

  // Motion_control_set_PWM brakes every push while latched, whoever asks.
  val driveEnabled: bool =
    (mode == SEND or mode == USE) and not(fw.tripped) and not(fw.slipped) and not(fw.jam)

  // One sample with the motor pushing.
  action drive: bool = all {
    driveEnabled,
    val coupled = coupledOf(front, outletOpen)
    val toSpill = outletOpen and front >= HUB
    // travel
    val take = if (stuck or not(coupled)) 0 else min(FEED, pulled)
    val fill = if (stuck or not(coupled)) 0 else min(FEED - take, BUF - buf)
    val t =
      if (stuck) 0
      else if (toSpill) FEED
      else if (outletOpen) 1                      // up to the gap
      else if (front < PATH) min(FEED, PATH - front)
      else take + fill
    val f2 = if (stuck or toSpill or coupled) front else front + t
    val s2 = if (toSpill and not(stuck)) spill + t else spill
    val b2 = buf + fill
    val p2 = pulled - take
    // what the AS5600 reads: the gear, which turns on a slipping strand
    val slipping = stuck and slips
    val meas = if (slipping) FEED else t
    val fw2 = fwSample(fw, mode, asGood, meas, true, b2 == BUF, p2 > 0, stuck)
    val uncoupledFeed = slipping or (t > 0 and (toSpill or (mode != SEND and not(coupled))))
    all {
      front' = f2, spill' = s2, buf' = b2, pulled' = p2,
      fw' = fw2,
      attemptSpill' = if (restarted(fw2)) 0 else attemptSpill + (s2 - spill),
      faultFed' = if (restarted(fw2)) false else faultFed or uncoupledFeed,
      grind' = if (not(asGood)) 0 else grindNet(grind + (if (slipping) FEED else 0), mode),
      lastAction' =
        if (fw2.slipped and not(fw.slipped)) "slip"
        else if (fw2.tripped and not(fw.tripped)) "trip"
        else if (fw2.jam and not(fw.jam)) "jamLatch"
        else if (b2 == BUF) "drivePressed"
        else "drive",
    },
    credit' = CONS,
    outletOpen' = outletOpen, stuck' = stuck, slips' = slips, asGood' = asGood, mode' = mode,
  }

  // One sample with the motor not pushing: latched, jammed, a pressed buffer
  // held, or a mode that does not push (G5). The extruder pulling a coupled
  // strand here is the resume.
  action idleSample: bool = all {
    not(driveEnabled) or buf == BUF,
    not(mode == PULL),                    // PULL samples are retracts
    // A latched slip coasts: the extruder draws a free strand through (G2).
    val drawn = fw.slipped and not(escalated(fw)) and not(stuck) and mode == USE and
                coupledOf(front, outletOpen) and pulled > 0
    val fw2 = fwSample(fw, mode, asGood, if (drawn) CONS else 0, false, buf == BUF, pulled > 0, stuck)
    val reset = restarted(fw2)
    all {
      fw' = fw2,
      attemptSpill' = if (reset) 0 else attemptSpill,
      faultFed' = if (reset) false else faultFed,
      grind' = grindNet(grind, mode),
      lastAction' = if (fw.tripped and not(fw2.tripped)) "resume"
                    else if (fw.slipped and not(fw2.slipped)) "recover"
                    else if (fw2.jam and not(fw.jam)) "jamLatch"
                    else "idle",
    },
    credit' = CONS,
    front' = front, spill' = spill, outletOpen' = outletOpen, stuck' = stuck, slips' = slips,
    buf' = buf, pulled' = pulled, asGood' = asGood, mode' = mode,
  }

  // One retract sample (G4): spill first, then the front.
  action retract: bool = all {
    mode == PULL,
    front > 0 or spill > 0,
    val fw2 = fwSample(fw, mode, asGood, -1, false, false, false, stuck)
    all {
      fw' = fw2,
      attemptSpill' = if (restarted(fw2)) 0
                      else if (spill > 0 and attemptSpill > 0) attemptSpill - 1
                      else attemptSpill,
      faultFed' = if (restarted(fw2)) false else faultFed,
      grind' = grind,
      lastAction' = if (fw.tripped and not(fw2.tripped)) "unwound" else "retract",
    },
    spill' = if (spill > 0) spill - 1 else spill,
    front' = if (spill > 0) front else front - 1,
    buf' = 0, pulled' = 0, credit' = CONS,
    outletOpen' = outletOpen, stuck' = stuck, slips' = slips, asGood' = asGood, mode' = mode,
  }

  // ---------------------------------------------------------------- physics

  action detach: bool = all {
    not(outletOpen),
    outletOpen' = true,
    // the strand is no longer held at the far end: the buffer relaxes
    front' = front, spill' = spill, stuck' = stuck, slips' = slips, buf' = 0, pulled' = 0,
    credit' = credit, asGood' = asGood,
    mode' = mode, fw' = fw, attemptSpill' = attemptSpill, faultFed' = faultFed,
    grind' = 0,                 // a second fault: its own bound,
    lastAction' = "detach",
  }

  // The extruder takes one unit (G3), at most CONS per sample.
  action consume: bool = all {
    mode == USE, front == PATH, credit > 0,
    credit' = credit - 1,
    if (outletOpen) all {
      spill > 0,
      spill' = spill - 1, buf' = buf, pulled' = pulled,
      lastAction' = "consumeSpill",
    } else all {
      spill' = spill,
      buf' = if (buf > 0) buf - 1 else buf,
      pulled' = if (buf > 0) pulled else min(pulled + 1, PULL_MAX),
      lastAction' = "consume",
    },
    front' = front, outletOpen' = outletOpen, stuck' = stuck, slips' = slips, asGood' = asGood,
    mode' = mode, fw' = fw, keepGhosts,
  }

  // With the spill taken up, consumption draws the tube back onto the hub;
  // the extruder now pulls against the braked BMCU.
  action reseat: bool = all {
    outletOpen, spill == 0, mode == USE, front == PATH, credit > 0,
    outletOpen' = false, pulled' = 1, credit' = credit - 1,
    front' = front, spill' = spill, stuck' = stuck, slips' = slips, buf' = buf, asGood' = asGood,
    mode' = mode, fw' = fw, keepGhosts,
    lastAction' = "reseat",
  }

  // A human puts the tube back (the send-path case: nothing is consumed).
  action reattach: bool = all {
    outletOpen, spill == 0,
    outletOpen' = false,
    front' = front, spill' = spill, stuck' = stuck, slips' = slips, buf' = buf, pulled' = pulled,
    credit' = credit, asGood' = asGood,
    mode' = mode, fw' = fw, keepGhosts,
    lastAction' = "reattach",
  }

  action stick: bool = all {
    not(stuck), stuck' = true, slips' = false,
    front' = front, spill' = spill, outletOpen' = outletOpen, buf' = buf, pulled' = pulled,
    credit' = credit, asGood' = asGood,
    mode' = mode, fw' = fw, keepGhosts,
    lastAction' = "stick",
  }

  // Held, and the gear slips on it rather than stalling (grinding).
  action stickSlip: bool = all {
    not(stuck), stuck' = true, slips' = true,
    front' = front, spill' = spill, outletOpen' = outletOpen, buf' = buf, pulled' = pulled,
    credit' = credit, asGood' = asGood,
    mode' = mode, fw' = fw, attemptSpill' = attemptSpill, faultFed' = faultFed, grind' = 0,
    lastAction' = "stickSlip",
  }

  action unstick: bool = all {
    stuck, stuck' = false, slips' = false,
    front' = front, spill' = spill, outletOpen' = outletOpen, buf' = buf, pulled' = pulled,
    credit' = credit, asGood' = asGood,
    mode' = mode, fw' = fw, keepGhosts,
    lastAction' = "unstick",
  }

  action asToggle: bool = all {
    asGood' = not(asGood),
    front' = front, spill' = spill, outletOpen' = outletOpen, stuck' = stuck, slips' = slips,
    buf' = buf, pulled' = pulled, credit' = credit,
    mode' = mode, fw' = fw, keepGhosts,
    lastAction' = "asToggle",
  }

  // ---------------------------------------------------------------- step

  action step = {
    nondet dice = 0.to(19).oneOf()
    any {
      all { dice < 3, cmdSend }, all { dice < 3, cmdUse }, all { dice < 2, cmdPull },
      drive, drive, idleSample, retract, consume, consume,
      all { dice < 2, detach }, reseat, all { dice < 2, reattach },
      all { dice < 1, stick }, all { dice < 1, stickSlip }, all { dice < 3, unstick },
      all { dice < 1, asToggle },
    }
  }

  // ---------------------------------------------------------------- invariants

  // Free feed never grows past the budget. Unlatched, once judged in this
  // mode, the count is within the mode's budget; latched nothing pushes, so
  // it never gets more than one sample past the largest budget.
  val freeBounded: bool = all {
    (fw.judged and not(fw.tripped) and asGood) implies fw.excess <= budget(mode),
    fw.excess <= MAX_BUDGET + FEED,
  }

  // What came out of the gap since the count last started over is bounded:
  // the trip comes within SPILL_CAP.
  val spillBounded: bool = attemptSpill <= SPILL_CAP

  // The guard trips only on a real fault: since the count last started
  // over, travel went uncoupled outside send_out, or out of the gap. Normal
  // printing near the consumption ceiling, or a send that reaches the
  // extruder before the buffer presses, never trips.
  val noFalseTrip: bool = fw.tripped implies faultFed

  // The latch brakes every push.
  val latchBrakes: bool = (fw.tripped or fw.slipped) implies not(driveEnabled)

  // A slip is caught: the gear grinds into a held strand at most GRIND_CAP
  // per fault (the slip, or a detach on top of it) and printer command.
  val grindBounded: bool = grind <= GRIND_CAP

  // A slip is never taken for an open outlet that tension may resume.
  val slipNotOpen: bool = fw.slipped implies not(fw.tripped)

  // A slip clears only on a strand that moves: never while it is held.
  val recoverOnFree: bool = lastAction == "recover" implies not(stuck)

  // Recoveries are bounded per printer command.
  val resumesBounded: bool = fw.resumes <= MAX_RES

  // An escalated slip in on_use is raised to the printer once sampled.
  val escalationRaised: bool =
    (mode == USE and fw.judged and escalated(fw)) implies fw.jam

  // A jam latches only on a held strand: in particular not at the resume,
  // where the buffer is pulled by the printer.
  val jamSound: bool = fw.jam implies fw.jamWasStuck

  // The printer's send_out / pull release a jam (346783f).
  val jamReleasedByCommand: bool =
    (lastAction == "cmdSend" or lastAction == "cmdPull") implies not(fw.jam)

  val safety: bool = all {
    freeBounded, spillBounded, noFalseTrip, latchBrakes, jamSound, jamReleasedByCommand,
    grindBounded, slipNotOpen, recoverOnFree, resumesBounded, escalationRaised,
  }

  // ---------------------------------------------------------------- witnesses
  // Expected to be VIOLATED.

  val witnessNoTrip: bool = not(fw.tripped)
  val witnessNoResume: bool = lastAction != "resume"
  val witnessNoUnwind: bool = lastAction != "unwound"
  // At the resume the buffer is pulled: what the jam detector is held off for.
  val witnessNoPulledResume: bool = not(lastAction == "resume" and pulled > 0 and not(stuck))
  val witnessNoJam: bool = not(fw.jam)
  // On the send path the spill reaches past the on_use budget.
  val witnessSmallSpill: bool = attemptSpill <= USE_BUD
  // No encoder, no verdict: the spill then grows unbounded.
  val witnessNoBlindSpill: bool = spill <= SPILL_CAP
  // The slip verdict is reached.
  val witnessNoSlip: bool = not(fw.slipped)
  // A slip clears by itself once the hold lets go.
  val witnessNoRecover: bool = lastAction != "recover"
  // A slip that will not clear is raised.
  val witnessNoEscalation: bool = not(escalated(fw) and fw.jam)

  // ---------------------------------------------------------------- scenarios

  // A send that reaches the extruder, and the printer moving on to
  // before_on_use before the buffer presses (send_out p50 -> bef_on_use p53):
  // no trip. Then printing at the consumption ceiling: no trip.
  run arrivalBeforePressTest =
    init.then(cmdSend)
      .then(drive).then(drive).then(drive)
      .expect(front == PATH and fw.excess == PATH and not(fw.tripped))
      .then(cmdUse)
      .expect(fw.excess == 0 and not(fw.tripped))
      .then(consume).then(drive).then(consume).then(drive).then(consume).then(drive)
      .then(consume).then(drive)
      .expect(not(fw.tripped) and safety)

  // The tube comes off mid-print: the guard stops within its budget, the
  // printer eats the slack, the tube reseats, the resume is not a jam.
  run detachMidPrintTest =
    init.then(cmdSend).then(drive).then(drive).then(drive).then(drive)
      .expect(lastAction == "drivePressed" and fw.excess == 0 and fw.slipCnt == FEED)
      .then(cmdUse).then(detach)
      .then(drive).then(drive).then(drive).then(drive).then(drive)
      .expect(fw.tripped and lastAction == "trip" and spill == 10 and not(driveEnabled))
      .then(idleSample)
      .expect(fw.tripped and fw.excess == 5)          // latched: no drain
      .then(consume).then(idleSample).then(consume).then(idleSample)
      .then(consume).then(idleSample).then(consume).then(idleSample)
      .then(consume).then(idleSample).then(consume).then(idleSample)
      .then(consume).then(idleSample).then(consume).then(idleSample)
      .then(consume).then(idleSample).then(consume).then(idleSample)
      .expect(spill == 0 and fw.tripped)
      .then(reseat).then(idleSample)
      .expect(lastAction == "resume" and not(fw.tripped) and not(fw.jam) and fw.disarm >= 0)
      .then(drive)
      .expect(not(fw.jam) and not(fw.tripped) and safety)

  // A send into an open outlet trips at the send budget. The printer gives
  // up (idle): count and latch start over, the retry gets a fresh budget and
  // trips again. A pull winds the count back and clears the latch.
  run sendNotTakenTest =
    init.then(detach).then(cmdSend)
      .then(drive).then(drive).then(drive).then(drive).then(drive)
      .expect(fw.tripped and fw.excess == 9 and spill == 8)
      .then(cmdSend).expect(fw.tripped and not(driveEnabled))
      .then(cmdUse).expect(not(fw.tripped) and fw.excess == 0)
      .then(cmdSend)
      .then(drive).then(drive).then(drive).then(drive).then(drive)
      .expect(fw.tripped and spill == 18)
      .then(cmdPull)
      .then(retract).then(retract).then(retract).then(retract).then(retract)
      .then(retract).then(retract).then(retract).then(retract).then(retract)
      .expect(lastAction == "unwound" and not(fw.tripped) and fw.excess == 0 and safety)

  // The gear slips on a held strand at the extruder: a slip. The hold lets
  // go, the extruder pulls, and the coasting gear turns with the strand: the
  // slip clears by itself, with no jam raised.
  run slipRecoverTest =
    init.then(cmdSend).then(drive).then(drive).then(drive).then(drive)
      .then(cmdUse).then(stickSlip)
      .then(drive).then(drive).then(drive).then(drive).then(drive)
      .expect(fw.slipped and not(fw.jam) and not(driveEnabled))
      .then(unstick)
      .then(consume).then(idleSample).then(consume).then(idleSample).then(consume)
      .expect(pulled > 0 and fw.slipped)
      .then(idleSample).then(idleSample).then(idleSample)
      .expect(lastAction == "recover" and not(fw.slipped) and fw.resumes == 1 and
              not(fw.jam) and driveEnabled and safety)

  // The same slip with the hold never letting go: escalated, and raised.
  run slipEscalateTest =
    init.then(cmdSend).then(drive).then(drive).then(drive).then(drive)
      .then(cmdUse).then(stickSlip)
      .then(drive).then(drive).then(drive).then(drive).then(drive)
      .then(consume).then(idleSample).then(consume).then(idleSample).then(consume)
      .then(idleSample).then(idleSample).then(idleSample).then(idleSample)
      .then(idleSample).then(idleSample)
      .expect(escalated(fw) and fw.jam and fw.slipped and safety)

  // A held spool: the jam latches on the buffer, the guard does not; the
  // printer's send_out clears it.
  run jamIsNotOpenTest =
    init.then(cmdSend).then(drive).then(drive).then(drive).then(drive)
      .then(cmdUse).then(stick)
      .then(consume).then(drive).then(consume).then(drive).then(consume).then(drive)
      .then(consume).then(drive)
      .expect(fw.jam and not(fw.tripped))
      .then(unstick).then(cmdSend)
      .expect(not(fw.jam) and safety)
}
