import XCTest
@testable import HearsayLogic

/// Metres north of HOME, as degrees of latitude.
private func north(_ metres: Double) -> Double { -26.12 + metres / 111_195 }

private func fix(_ at: Double, _ metresNorth: Double, accuracy: Double = 20) -> Movement.Fix {
    Movement.Fix(at: at, latitude: north(metresNorth), longitude: 80.14, accuracy: accuracy)
}

/// A recording session: drives a Movement, ticking every second as the app does, and keeps what it asked for.
private struct Session {
    var movement = Movement()
    var now = 0.0
    var effects: [Movement.Effect] = []

    mutating func run(until end: Double) {
        while now < end {
            now += 1
            effects += movement.tick(at: now)
        }
    }

    mutating func take() -> [Movement.Effect] {
        defer { effects = [] }
        return effects
    }

    var asked: Int { effects.filter { if case .requestFix = $0 { true } else { false } }.count }

    var recorded: [(Movement.State, Movement.Reason)] {
        effects.compactMap { if case let .record(state, _, reason, _) = $0 { (state, reason) } else { nil } }
    }

    /// Started, then settled at home with Core Motion saying still.
    static func settledAtHome() -> Session {
        var s = Session()
        s.effects += s.movement.recording(true, at: 0)
        s.effects += s.movement.activity(.stationary, .high, at: 0)
        s.effects += s.movement.fix(fix(5, 0))
        s.run(until: 130)
        s.effects += s.movement.fix(fix(130, 8))
        precondition(s.movement.state == .stationary)
        return s
    }
}

final class MovementTests: XCTestCase {
    func testStartingAsksForAPreciseFix() {
        var s = Session()
        let effects = s.movement.recording(true, at: 0)
        XCTAssertEqual(effects, [.record(.uncertain, confidence: 0.2, reason: .started, anchor: nil), .requestFix(precise: true)])
    }

    func testAFixThatNeverComesIsAskedForAgain() {
        var s = Session()
        _ = s.movement.recording(true, at: 0)  // no signal: the fix it asks for never comes
        s.run(until: 300)
        XCTAssertEqual(s.take(), [.requestFix(precise: true)])
    }

    func testTwoAgreeingFixesSettleAndFixesStop() throws {
        var s = Session.settledAtHome()
        XCTAssertTrue(s.effects.contains { if case .watch = $0 { true } else { false } })
        XCTAssertEqual(s.recorded.last?.1, .settled)
        let anchor = try XCTUnwrap(s.movement.anchor)
        _ = s.take()
        s.run(until: 130 + 1200)
        // No more fixes for a while, and a heartbeat every 5 minutes saying where: during a long
        // stay, the server's only readings of where audio was recorded.
        XCTAssertEqual(s.take(), Array(repeating: .record(.stationary, confidence: 0.9, reason: .heartbeat, anchor: anchor), count: 4))
        s.run(until: 130 + 1800)
        XCTAssertTrue(s.take().contains(.requestFix(precise: false)))  // a cheap check now and then
    }

    func testAFixFarAwayIsAnOutlierUntilASecondAgrees() {
        var s = Session.settledAtHome()
        _ = s.take()
        XCTAssertEqual(s.movement.fix(fix(200, 400)), [])  // drift, or a jump
        XCTAssertEqual(s.movement.state, .stationary)
        s.run(until: 260)
        XCTAssertEqual(s.take(), [.requestFix(precise: true)])  // confirms it
        XCTAssertEqual(s.movement.fix(fix(261, 5)), [])  // home after all
        XCTAssertEqual(s.movement.state, .stationary)

        XCTAssertEqual(s.movement.fix(fix(300, 400)), [])
        s.run(until: 360)
        _ = s.take()
        let effects = s.movement.fix(fix(361, 420))
        XCTAssertEqual(s.movement.state, .uncertain)
        XCTAssertEqual(effects.first, .unwatch)
    }

    func testAnInaccurateFixNeverMovesTheAnchor() {
        var s = Session.settledAtHome()
        _ = s.take()
        XCTAssertEqual(s.movement.fix(fix(200, 1500, accuracy: 900)), [])  // a cell tower's guess
        XCTAssertEqual(s.movement.fix(fix(201, 160, accuracy: 120)), [])  // within its accuracy of home
        s.run(until: 400)
        XCTAssertEqual(s.asked, 0)
        XCTAssertEqual(s.movement.state, .stationary)
    }

    func testWalkingFiftyMetresAwayNeedsTwoGoodFixesAMinuteApart() {
        var s = Session.settledAtHome()
        _ = s.take()
        _ = s.movement.activity(.walking, .high, at: 200)
        s.run(until: 259)
        XCTAssertEqual(s.asked, 0)  // walking around the house isn't leaving yet
        s.run(until: 260)
        XCTAssertEqual(s.take(), [.requestFix(precise: true)])
        XCTAssertEqual(s.movement.fix(fix(270, 50)), [])
        XCTAssertEqual(s.movement.fix(fix(300, 55)), [])  // too soon to confirm
        XCTAssertEqual(s.movement.state, .stationary)
        s.run(until: 330)
        XCTAssertEqual(s.take(), [.requestFix(precise: true)])
        let effects = s.movement.fix(fix(335, 55))
        XCTAssertEqual(effects, [.unwatch, .record(.walking, confidence: 0.9, reason: .departed, anchor: nil)])
    }

    func testPoorIndoorFixesNeverDepartOrAskForConfirmationEvenWhileWalking() {
        var s = Session.settledAtHome()
        _ = s.take()
        _ = s.movement.activity(.walking, .high, at: 200)
        s.run(until: 260)
        _ = s.take()
        XCTAssertEqual(s.movement.fix(fix(270, 400, accuracy: 150)), [])
        s.run(until: 330)
        XCTAssertEqual(s.asked, 0)
        XCTAssertEqual(s.movement.fix(fix(335, 500, accuracy: 150)), [])
        XCTAssertEqual(s.movement.state, .stationary)
    }

    func testAGeofenceExitStillNeedsTwoGoodFixes() {
        var s = Session.settledAtHome()
        _ = s.take()
        XCTAssertEqual(s.movement.leftAnchor(at: 200), [.requestFix(precise: true)])
        XCTAssertEqual(s.movement.fix(fix(210, 300)), [])  // before Core Motion notices
        XCTAssertEqual(s.movement.state, .stationary)
        let effects = s.movement.fix(fix(270, 320))
        XCTAssertEqual(effects, [.unwatch, .record(.uncertain, confidence: 0.2, reason: .departed, anchor: nil)])
    }

    func testTheAnchorKeepsTheSharpestFixAndHeartbeatsKeepItsAccuracy() throws {
        var s = Session()
        _ = s.movement.recording(true, at: 0)
        _ = s.movement.activity(.stationary, .high, at: 0)
        _ = s.movement.fix(fix(5, 0, accuracy: 80))
        _ = s.movement.fix(fix(130, 20, accuracy: 8))
        let anchor = try XCTUnwrap(s.movement.anchor)
        XCTAssertEqual(anchor.latitude, north(20))
        XCTAssertEqual(anchor.accuracy, 8)
        XCTAssertEqual(anchor.radius, 120)
        s.run(until: 430)
        XCTAssertTrue(s.take().contains(.record(.stationary, confidence: 0.9, reason: .heartbeat, anchor: anchor)))
    }

    func testDepartureUsesBothFixErrorsAndAnUncertainFixRestartsConfirmation() {
        for accuracy in [21.0, 900.0] {
            var s = Session()
            _ = s.movement.recording(true, at: 0)
            _ = s.movement.fix(fix(5, 0, accuracy: 80))
            _ = s.movement.fix(fix(130, 0, accuracy: 80))
            _ = s.movement.fix(fix(200, 90))
            _ = s.movement.fix(fix(260, 95))
            XCTAssertEqual(s.movement.state, .stationary)  // within 80 + 20 m uncertainty
            _ = s.movement.fix(fix(300, 110))
            _ = s.movement.fix(fix(350, 120, accuracy: accuracy))
            _ = s.movement.fix(fix(420, 120))
            XCTAssertEqual(s.movement.state, .stationary)  // not a confirmation of the 300 s fix
            _ = s.movement.fix(fix(480, 125))
            XCTAssertEqual(s.movement.state, .uncertain)
            XCTAssertNil(s.movement.anchor)
        }
    }

    func testWalkingAroundAtHomeChecksLessAndLessOften() {
        var s = Session.settledAtHome()
        _ = s.take()
        _ = s.movement.activity(.walking, .medium, at: 200)
        var checks: [Double] = []
        while s.now < 200 + 3600 {
            s.run(until: s.now + 1)
            if s.take().contains(.requestFix(precise: true)) {
                checks.append(s.now)
                _ = s.movement.fix(fix(s.now + 5, 10))  // still home
            }
        }
        XCTAssertEqual(checks, [260, 560, 1160, 2360])  // 5, 10, then 20 minutes apart
        XCTAssertEqual(s.movement.state, .stationary)
    }

    /// A party or a yoga class: walking all along, fixes every 5 minutes, going nowhere. Only precise
    /// fixes say so: vague ones agree even after walking a good way.
    func testWalkingAboutOneSpotSettlesOncePreciseFixesAgreeForTenMinutes() {
        for (accuracy, expected) in [(20.0, Movement.State.stationary), (120.0, .walking)] {
            var s = Session()
            _ = s.movement.recording(true, at: 0)
            _ = s.movement.fix(fix(5, 0, accuracy: accuracy))
            var offsets = [25.0, -15.0]
            while !offsets.isEmpty && s.now < 1200 {
                _ = s.movement.activity(.walking, .high, at: s.now)
                s.run(until: s.now + 60)
                if s.take().contains(.requestFix(precise: true)) {
                    _ = s.movement.fix(fix(s.now + 5, offsets.removeFirst(), accuracy: accuracy))
                }
            }
            XCTAssertLessThan(s.now, 700)  // the third fix, 10 minutes after the first
            XCTAssertEqual(s.movement.state, expected, "accuracy \(accuracy)")
        }
    }

    /// Fixes every 5 minutes on the way somewhere: walking, or crawling through a jam with each fix
    /// near the last.
    func testGoingSomewhereNeverSettles() {
        for (activity, step, moving) in [(Movement.Activity.walking, 400.0, Movement.State.walking), (.automotive, 20, .driving)] {
            var s = Session()
            _ = s.movement.recording(true, at: 0)
            _ = s.movement.fix(fix(5, 0))
            var along = 0.0
            while s.now < 1800 {
                _ = s.movement.activity(activity, .high, at: s.now)
                s.run(until: s.now + 60)
                if s.take().contains(.requestFix(precise: true)) {
                    along += step
                    _ = s.movement.fix(fix(s.now + 5, along))
                    XCTAssertEqual(s.movement.state, moving, "\(activity), \(along) m along")
                }
            }
        }
    }

    func testARedLightOrAQueueDoesNotSettle() {
        var s = Session()
        _ = s.movement.recording(true, at: 0)
        _ = s.movement.activity(.automotive, .high, at: 0)
        _ = s.movement.car(connected: true, at: 0)
        _ = s.movement.fix(fix(5, 0))
        XCTAssertEqual(s.movement.state, .driving)
        _ = s.movement.activity(.stationary, .high, at: 100)  // stopped in traffic
        s.run(until: 400)
        _ = s.take()
        _ = s.movement.fix(fix(305, 2000))
        s.run(until: 650)
        XCTAssertTrue(s.take().contains(.requestFix(precise: true)))
        _ = s.movement.fix(fix(650, 2010))
        XCTAssertNotEqual(s.movement.state, .stationary)  // under vehicleDwell between agreeing fixes
    }

    func testTheCarDisconnectingIsAPossibleArrival() {
        var s = Session()
        _ = s.movement.recording(true, at: 0)
        _ = s.movement.activity(.automotive, .high, at: 0)
        _ = s.movement.car(connected: true, at: 0)
        _ = s.movement.fix(fix(5, 0))
        _ = s.movement.activity(.stationary, .medium, at: 290)
        s.run(until: 300)
        _ = s.take()
        _ = s.movement.fix(fix(301, 5000))
        let arrived = s.movement.car(connected: false, at: 320)
        XCTAssertEqual(arrived.first, .requestFix(precise: true))
        _ = s.movement.fix(fix(325, 5005))
        s.run(until: 445)
        XCTAssertTrue(s.take().contains(.requestFix(precise: true)))  // the second fix, to agree
        _ = s.movement.fix(fix(450, 5010))
        XCTAssertEqual(s.movement.state, .stationary)
    }

    func testTheCarConnectingWhileSettledOnlyChecks() {
        var s = Session.settledAtHome()
        _ = s.take()
        XCTAssertEqual(s.movement.car(connected: true, at: 200), [.requestFix(precise: true)])
        XCTAssertEqual(s.movement.fix(fix(205, 10)), [])  // sitting in it in the driveway
        XCTAssertEqual(s.movement.state, .stationary)
    }

    func testPausingStopsAndResumingAtTheSameSpotSettlesAtOnce() {
        var s = Session.settledAtHome()
        _ = s.take()
        XCTAssertEqual(s.movement.recording(false, at: 500), [.unwatch, .record(.off, confidence: 1, reason: .stopped, anchor: nil)])
        XCTAssertEqual(s.movement.tick(at: 2000), [])
        XCTAssertEqual(s.movement.fix(fix(2001, 0)), [])  // a late fix while paused is ignored
        _ = s.movement.recording(true, at: 4000)
        let effects = s.movement.fix(fix(4005, 15))
        XCTAssertEqual(s.movement.state, .stationary)
        XCTAssertTrue(effects.contains { if case .record(.stationary, _, .resumed, _) = $0 { true } else { false } })
    }

    func testResumingSomewhereElseStartsOver() {
        var s = Session.settledAtHome()
        _ = s.movement.recording(false, at: 500)
        _ = s.movement.recording(true, at: 4000)
        _ = s.movement.fix(fix(4005, 3000))
        XCTAssertEqual(s.movement.state, .uncertain)
    }

    func testUnsureMotionStaysUncertainAndFixesStillSettle() {
        var s = Session()
        _ = s.movement.recording(true, at: 0)
        _ = s.movement.activity(.walking, .low, at: 0)  // not sure enough to call it walking
        _ = s.movement.fix(fix(5, 0, accuracy: 80))
        XCTAssertEqual(s.movement.state, .uncertain)
        s.run(until: 300)
        XCTAssertTrue(s.take().contains(.requestFix(precise: true)))  // fixed interval without motion
        _ = s.movement.fix(fix(305, 60, accuracy: 80))
        XCTAssertEqual(s.movement.state, .stationary)
        XCTAssertEqual(s.movement.anchor?.radius, 120)  // 1.5x the fixes' accuracy
    }

    func testTheCarConnectedSuggestsDrivingButWalkingWins() {
        var s = Session()
        _ = s.movement.recording(true, at: 0)
        _ = s.movement.fix(fix(5, 0))
        XCTAssertEqual(s.movement.car(connected: true, at: 10), [.record(.driving, confidence: 0.4, reason: .activity, anchor: nil)])
        XCTAssertEqual(s.movement.activity(.walking, .medium, at: 20), [.record(.walking, confidence: 0.6, reason: .activity, anchor: nil)])
    }
}
