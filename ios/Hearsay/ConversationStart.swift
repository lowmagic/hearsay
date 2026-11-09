/// Where the conversation going on at `now` began, as hearsay/conversations.py
/// would draw it: speech with no gap of `gap` or more. Plain logic, so it's
/// tested on its own (`swift test` in ios/); Lookback finds the speech.
///
/// The speech is searched for a stretch at a time, latest first, so a
/// conversation that began minutes ago costs minutes of analysis. Until the
/// search has gone back past a gap, the start isn't known yet.
enum ConversationStart: Equatable {
    /// hearsay/conversations.py GAP: a conversation ends after this long
    /// without speech.
    static let gap = 180.0

    /// No conversation is going on: no speech in the last `gap`.
    case none
    case began(Double)
    /// Search further back.
    case unknown

    /// `speech`: (start, end) of the speech found from `searched` to `now`,
    /// in order. `earliest`: as far back as there is to search, where the
    /// audio starts or the search gives up.
    static func find(speech: [(start: Double, end: Double)], now: Double, searched: Double,
                     earliest: Double) -> ConversationStart {
        let exhausted = searched <= earliest
        guard let last = speech.last else {
            return exhausted || now - searched >= gap ? .none : .unknown
        }
        if now - last.end >= gap { return .none }
        var start = last.start
        for earlier in speech.dropLast().reversed() {
            if start - earlier.end >= gap { return .began(start) }
            start = min(start, earlier.start)
        }
        return exhausted || start - searched >= gap ? .began(start) : .unknown
    }
}
