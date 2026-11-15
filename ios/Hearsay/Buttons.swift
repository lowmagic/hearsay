import Foundation

/// What the pendant's button does: an action for a single tap and one for a
/// double tap, confirmed by a buzz. The consumer firmware reports no other
/// gesture (holds go unreported, and 3 s powers the pendant off); see
/// docs/pendant-ble.md.
///
/// After each tap it handles, the app spools an ACTION record (the event and
/// what it did) next to the tap's own BUTTON record, so the NAS knows which
/// taps were conversation marks (hearsay/capture.py, marks).
final class Buttons: ObservableObject {
    /// Tapped while on, pause pauses and off switches off; tapped while
    /// paused or off, either switches on.
    enum Action: String, CaseIterable, Identifiable {
        case nothing, start, end, pause, mute
        var id: Self { self }

        var label: String {
            switch self {
            case .nothing: "Nothing"
            case .start: "Mark: keep this conversation"
            case .end: "Mark: end the conversation"
            case .pause: "Pause / on"
            case .mute: "Off / on"
            }
        }
    }

    /// What a tap did. The raw values are the ACTION record's codes: paused,
    /// resumed (paused to on), muted (to off), unmuted (off to on).
    enum Outcome: UInt8 {
        case nothing = 0, start, end, paused, resumed, muted, unmuted

        /// Buzzes of 100 (1), 300 (2) and 500 ms (3): one per mode reached.
        var buzz: [UInt8] {
            switch self {
            case .nothing: []
            case .start: [1]
            case .end: [2]
            case .paused: [3]
            case .resumed, .unmuted: [1, 1]
            case .muted: [3, 3]
            }
        }
    }

    static let singleTap: UInt8 = 1
    static let doubleTap: UInt8 = 2

    @Published var single: Action { didSet { UserDefaults.standard.set(single.rawValue, forKey: "singleTap") } }
    @Published var double: Action { didSet { UserDefaults.standard.set(double.rawValue, forKey: "doubleTap") } }

    private let pendant: Pendant
    private let pause: Pause

    init(pendant: Pendant, pause: Pause) {
        self.pendant = pendant
        self.pause = pause
        // A single tap marked a conversation before taps were configurable.
        single = UserDefaults.standard.string(forKey: "singleTap").flatMap(Action.init) ?? .start
        double = UserDefaults.standard.string(forKey: "doubleTap").flatMap(Action.init) ?? .pause
        pendant.onButton = { [weak self] event in self?.handle(event) }
    }

    private func handle(_ event: UInt8) {
        let action: Action
        switch event {
        case Self.singleTap: action = single
        case Self.doubleTap: action = double
        default: return  // the release that follows every tap
        }
        let mode = pause.mode
        let target: Pause.Mode? = switch action {
        case .nothing, .start, .end: nil
        case .pause: mode == .on ? .paused : .on
        case .mute: mode == .on ? .off : .on
        }
        let outcome: Outcome = switch (action, target) {
        case (.start, _): .start
        case (.end, _): .end
        case (_, .paused): .paused
        case (_, .off): .muted
        case (_, .on): mode == .off ? .unmuted : .resumed
        default: .nothing
        }
        log.info("button \(event): \(action.rawValue), \(String(describing: outcome))")

        // Recorded where the tap itself went: before pausing, so it isn't
        // withheld, and after switching on.
        let record = Data([event, outcome.rawValue])
        if target != .on { pendant.record(kind: Spool.action, data: record) }
        if let target { pause.set(target) }
        if target == .on { pendant.record(kind: Spool.action, data: record) }
        pendant.buzz(outcome.buzz)
    }
}
