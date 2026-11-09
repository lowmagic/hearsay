import AVFoundation
import SoundAnalysis

/// Switching from paused to on in the middle of a conversation sends that
/// conversation from where it began, not from the switch: the paused window's
/// audio from there on goes to the upload spool, and the rest stays in the
/// window to review.
///
/// Where it began is found on the phone, from speech: Apple's on-device
/// sound classifier (SoundAnalysis), 3 s at a time, over the window's audio
/// decoded `stretch` at a time, latest first, back to a gap as long as the
/// NAS's between conversations (ConversationStart). It isn't the NAS's
/// speech detector (Silero), so the start can differ from where the NAS
/// draws it; what isn't sent stays in the window, and the NAS draws its own
/// boundaries from whatever arrives. The search goes back `limit` at most.
enum Lookback {
    static let stretch = 600.0
    static let limit = 2 * 3600.0
    /// Sent from before the first speech found, which 3 s windows place only
    /// roughly.
    static let lead = 10.0
    private static let speechLabels = ["speech", "shout", "whispering"]
    private static let speechConfidence = 0.5

    struct Split {
        let began: Double
        /// For the upload spool.
        let uploads: [Data]
        /// What's left of each window file the uploads took from: empty if
        /// nothing is.
        let rest: [(file: URL, data: Data)]
    }

    /// `files`: the window's sealed files, oldest first. nil if no
    /// conversation was going on at `now`.
    static func split(_ files: [URL], now: Double) throws -> Split? {
        let cutoff = now - limit
        let (loaded, codec) = try load(files, since: cutoff)
        let records = loaded.flatMap(\.records)
        let frames = WindowAudio.frames(records, codec: codec).filter { $0.end >= cutoff }
        guard let first = frames.first else { return nil }

        let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
        request.windowDuration = CMTime(seconds: 3, preferredTimescale: CMTimeScale(WindowAudio.sampleRate))
        request.overlapFactor = 0
        var speech: [(start: Double, end: Double)] = []
        var searched = now, high = frames.endIndex
        var answer = ConversationStart.unknown
        while answer == .unknown {
            searched = max(searched - stretch, first.end)
            let low = frames[..<high].firstIndex { $0.end >= searched } ?? high
            speech = try detectSpeech(frames[low..<high], request) + speech
            high = low
            answer = ConversationStart.find(speech: speech, now: now, searched: searched, earliest: first.end)
        }
        guard case .began(let began) = answer else { return nil }

        let start = began - lead
        var rest: [(file: URL, data: Data)] = []
        var current = codec
        for file in loaded {
            var kept = Data()
            var took = false
            for record in file.records {
                if record.kind == Spool.connected { current = record.data.first }
                if WindowAudio.within(record, codec: current, from: start, to: .infinity) {
                    took = true
                } else {
                    kept.append(record.encoded)
                }
            }
            if took { rest.append((file.url, kept)) }
        }
        return Split(began: began, uploads: WindowAudio.uploads(records, codec: codec, from: start, to: .infinity),
                     rest: rest)
    }

    private struct File {
        let url: URL
        let records: [Spool.Record]
    }

    /// The files sealed since `cutoff` (none of the others holds anything
    /// from after it), and the codec in effect before them: from the last
    /// CONNECTED before them, looked for only if their first live audio
    /// comes before one of their own.
    private static func load(_ files: [URL], since cutoff: Double) throws -> ([File], UInt8?) {
        var earlier = files[...]
        var loaded: [File] = []
        while let file = earlier.last,
              let sealed = try file.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate,
              sealed.timeIntervalSince1970 >= cutoff {
            loaded.append(File(url: file, records: try Spool.records(in: file)))
            earlier = earlier.dropLast()
        }
        loaded.reverse()
        let opening = loaded.lazy.flatMap(\.records).first { $0.kind == Spool.connected || $0.kind == Spool.audio }
        guard opening?.kind == Spool.audio else { return (loaded, nil) }
        for file in earlier.reversed() {
            if let connected = try Spool.records(in: file).last(where: { $0.kind == Spool.connected }) {
                return (loaded, connected.data.first)
            }
        }
        return (loaded, nil)
    }

    /// Speech in `frames` (in the order they end), merged where it runs on.
    private static func detectSpeech(_ frames: ArraySlice<WindowAudio.Frame>,
                                     _ request: SNClassifySoundRequest) throws -> [(start: Double, end: Double)] {
        var speech: [(start: Double, end: Double)] = []
        var run: Run?
        for frame in frames {
            guard let samples = WindowAudio.frameSamples[frame.codec] else { continue }
            let length = Double(samples) / WindowAudio.sampleRate
            if run == nil || frame.codec != run!.codec || abs(frame.end - (run!.expectedEnd + length)) > WindowAudio.reanchor {
                if let run { speech += try run.finish() }
                run = try Run(start: frame.end - length, codec: frame.codec, samples: samples, request: request)
            }
            run!.add(frame.bytes, length: length)
        }
        if let run { speech += try run.finish() }

        var merged: [(start: Double, end: Double)] = []
        for found in speech.sorted(by: { $0.start < $1.start }) {
            if let last = merged.last, found.start <= last.end + 0.5 {
                merged[merged.count - 1].end = max(last.end, found.end)
            } else {
                merged.append(found)
            }
        }
        return merged
    }

    /// Frames end to end, decoded and classified from the start of the run,
    /// a second at a time.
    private final class Run: NSObject, SNResultsObserving {
        static let pcm = AVAudioFormat(standardFormatWithSampleRate: WindowAudio.sampleRate, channels: 1)!

        let start: Double
        let codec: UInt8
        private(set) var expectedEnd: Double
        private let decoder: WindowAudio.Decoder
        private let analyzer = SNAudioStreamAnalyzer(format: pcm)
        private let pending = AVAudioPCMBuffer(pcmFormat: pcm, frameCapacity: AVAudioFrameCount(WindowAudio.sampleRate))!
        private var position: AVAudioFramePosition = 0
        // Results arrive on the analyzer's own queue.
        private let lock = NSLock()
        private var found: [(start: Double, end: Double)] = []
        private var failure: Error?

        init(start: Double, codec: UInt8, samples: Int, request: SNClassifySoundRequest) throws {
            self.start = start
            self.codec = codec
            expectedEnd = start
            decoder = try WindowAudio.Decoder(codec: codec, samples: samples, pcm: Self.pcm)
            super.init()
            try analyzer.add(request, withObserver: self)
        }

        func add(_ bytes: Data, length: Double) {
            expectedEnd += length
            guard let decoded = decoder.decode(bytes) else { return }
            var offset: AVAudioFrameCount = 0
            while offset < decoded.frameLength {
                let count = min(decoded.frameLength - offset, pending.frameCapacity - pending.frameLength)
                memcpy(pending.floatChannelData![0] + Int(pending.frameLength), decoded.floatChannelData![0] + Int(offset),
                       Int(count) * MemoryLayout<Float>.size)
                pending.frameLength += count
                offset += count
                if pending.frameLength == pending.frameCapacity { flush() }
            }
        }

        func finish() throws -> [(start: Double, end: Double)] {
            flush()
            analyzer.completeAnalysis()
            return try lock.withLock {
                if let failure { throw failure }
                return found
            }
        }

        private func flush() {
            guard pending.frameLength > 0, let copy = AVAudioPCMBuffer(pcmFormat: Self.pcm, frameCapacity: pending.frameLength) else { return }
            memcpy(copy.floatChannelData![0], pending.floatChannelData![0], Int(pending.frameLength) * MemoryLayout<Float>.size)
            copy.frameLength = pending.frameLength
            analyzer.analyze(copy, atAudioFramePosition: position)
            position += AVAudioFramePosition(copy.frameLength)
            pending.frameLength = 0
        }

        func request(_ request: SNRequest, didProduce result: SNResult) {
            guard let result = result as? SNClassificationResult else { return }
            let confidence = Lookback.speechLabels.compactMap { result.classification(forIdentifier: $0)?.confidence }.max() ?? 0
            guard confidence >= Lookback.speechConfidence else { return }
            let range = result.timeRange
            lock.withLock { found.append((start + range.start.seconds, start + range.end.seconds)) }
        }

        func request(_ request: SNRequest, didFailWithError error: Error) {
            lock.withLock { failure = error }
        }
    }
}
