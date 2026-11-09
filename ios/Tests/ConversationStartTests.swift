import XCTest
@testable import HearsayLogic

final class ConversationStartTests: XCTestCase {
    private let gap = ConversationStart.gap

    func testGoesBackOverShortSilencesToTheLastLongOne() {
        let speech = [(start: 100.0, end: 200.0), (start: 200 + gap, end: 500 + gap), (start: 600 + gap, end: 1000 + gap)]
        let now = 1010 + gap
        XCTAssertEqual(ConversationStart.find(speech: speech, now: now, searched: 0, earliest: 0), .began(200 + gap))
    }

    func testNoConversationOnceSpeechStoppedAGapAgo() {
        let speech = [(start: 100.0, end: 200.0)]
        XCTAssertEqual(ConversationStart.find(speech: speech, now: 200 + gap, searched: 0, earliest: 0), .none)
        XCTAssertEqual(ConversationStart.find(speech: [], now: 1000, searched: 1000 - gap, earliest: 0), .none)
    }

    func testSearchesFurtherUntilItHasPassedAGap() {
        // Speech runs back to the edge of what's been searched: it may go on before it.
        let speech = [(start: 610.0, end: 900.0)]
        XCTAssertEqual(ConversationStart.find(speech: speech, now: 905, searched: 600, earliest: 0), .unknown)
        XCTAssertEqual(ConversationStart.find(speech: speech, now: 905, searched: 610 - gap, earliest: 0), .began(610))
        // Nothing yet, but the last gap hasn't been searched in full.
        XCTAssertEqual(ConversationStart.find(speech: [], now: 905, searched: 905 - gap + 1, earliest: 0), .unknown)
    }

    func testTakesWhatThereIsOnceTheSearchRunsOut() {
        let speech = [(start: 610.0, end: 900.0)]
        XCTAssertEqual(ConversationStart.find(speech: speech, now: 905, searched: 600, earliest: 600), .began(610))
    }
}
