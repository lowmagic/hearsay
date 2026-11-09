import Foundation

/// Whether the pendant's audio reaches the NAS: on, paused or off.
///
/// - On: everything the pendant sends goes to the upload spool.
/// - Paused: nothing reaches the NAS. The pendant stays connected and
///   subscribed (its firmware throws audio away while connected but
///   unsubscribed, rather than storing it), and the app keeps what arrives
///   in a paused window on the phone, for the owner to review, until
///   `retentionDays` after the window ends.
/// - Off: the pendant's mic is off in hardware (Pendant.setMuted), and
///   nothing is kept.
///
/// Off while on has no use of its own, and paused while off would keep
/// nothing, so they're one setting. Switching from paused to on sends the
/// conversation going on from where it began (Lookback); the rest of the
/// window stays to review. Stored as the pendant's mute and a paused flag,
/// so an older install's settings carry over.
///
/// A window is its own Spool under paused/<start millis>/, so its files are
/// already uploads: keeping one moves them into the upload spool, and the NAS
/// places them by their own times. Where the owner was goes in the window too
/// (Location), so a stretch uploaded later still gets its places, and nothing
/// from a pause reaches the NAS unless uploaded. A window with no audio (the
/// pendant away all along) is deleted as soon as it ends: there's nothing in
/// it to hear or keep.
///
/// Audio the pendant stored while away is downloaded on reconnect. If the
/// app was paused at any time while the pendant was away, all of it is
/// treated as paused, and if it was off, it's dropped (Pendant): only the
/// pendant's clock could split it, and that clock can be wrong.
final class Pause: ObservableObject {
    enum Mode: CaseIterable {
        case off, paused, on
    }

    static let retentionDayRange = 1...60
    private static let pausedKey = "paused"
    private static let retentionKey = "pausedRetentionDays"
    private static let windowKey = "pausedWindow"
    private static let leftPausedAtKey = "resumedAt"

    struct Window: Identifiable {
        let dir: URL
        let start: Date
        let end: Date
        let bytes: Int
        var id: URL { dir }
    }

    @Published private(set) var mode: Mode
    @Published private(set) var retentionDays: Int
    /// Finished windows, oldest first; the one being recorded isn't listed,
    /// nor one being split by Lookback.
    @Published private(set) var windows: [Window] = []
    /// What the last switch from paused to on sent.
    @Published private(set) var lookback: String?

    var retention: TimeInterval { Double(retentionDays) * 86400 }

    /// Where paused audio is kept right now: while paused, or while a
    /// download that counts as paused is running.
    private(set) var window: Spool?
    private var downloadPaused = false
    private var splitting: Set<URL> = []
    private let root: URL
    private let spool: Spool
    private let pendant: Pendant

    init(root: URL, spool: Spool, pendant: Pendant) {
        self.root = root
        self.spool = spool
        self.pendant = pendant
        mode = pendant.muted ? .off : UserDefaults.standard.bool(forKey: Self.pausedKey) ? .paused : .on
        let days = UserDefaults.standard.object(forKey: Self.retentionKey) as? Int ?? 30
        retentionDays = min(max(days, Self.retentionDayRange.lowerBound), Self.retentionDayRange.upperBound)
        if mode == .paused {
            // Relaunched mid-pause: carry on in the same window.
            let name = UserDefaults.standard.string(forKey: Self.windowKey) ?? Self.newWindowName()
            window = openWindow(name)
        }
        pendant.route(to: mode == .paused ? window : spool)
        pendant.pause = self
        WindowAudio.removeLeftovers()
        expire()
        for empty in windows where !Self.hasAudio(empty.dir) { delete(empty) }
    }

    func set(_ new: Mode) {
        let old = mode
        guard new != old else { return }
        mode = new
        UserDefaults.standard.set(new == .paused, forKey: Self.pausedKey)
        log.info("\(String(describing: old)) to \(String(describing: new))")
        if new == .paused {
            lookback = nil
            spool.seal()
            if window == nil { window = openWindow(Self.newWindowName()) }
            pendant.route(to: window)
        }
        if old == .paused {
            UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: Self.leftPausedAtKey)
            pendant.route(to: spool)
            if new == .on, let window { lookBack(window) }
            if !downloadPaused { endWindow() }
        }
        if (old == .off) != (new == .off) { pendant.setMuted(new == .off) }
    }

    /// How many days a window is kept after it ends. Shortening it deletes
    /// windows older than that at once.
    func setRetentionDays(_ days: Int) {
        retentionDays = min(max(days, Self.retentionDayRange.lowerBound), Self.retentionDayRange.upperBound)
        UserDefaults.standard.set(retentionDays, forKey: Self.retentionKey)
        expire()
    }

    /// A download of stored audio is starting; `awaySince` is when the
    /// pendant left (nil if unknown).
    func beginDownload(awaySince: Date?) {
        let leftPausedAt = UserDefaults.standard.object(forKey: Self.leftPausedAtKey) as? Double
        var pausedWhileAway = false
        if let awaySince, let leftPausedAt { pausedWhileAway = leftPausedAt >= awaySince.timeIntervalSince1970 }
        downloadPaused = mode == .paused || pausedWhileAway
        if downloadPaused { log.info("paused while the pendant was away: its stored audio counts as paused") }
    }

    /// Where the download's audio goes; nil drops it. Asked per packet, so
    /// pausing mid-download takes effect at once.
    var storedTarget: Spool? {
        guard mode == .paused || downloadPaused else { return spool }
        if window == nil { window = openWindow(Self.newWindowName()) }
        return window
    }

    func endDownload() {
        downloadPaused = false
        if mode != .paused && window != nil { endWindow() }
    }

    /// Upload a window like normal audio.
    func upload(_ window: Window) {
        do {
            for file in try Spool(dir: window.dir).pending() {
                try FileManager.default.moveItem(at: file, to: spool.dir.appendingPathComponent(file.lastPathComponent))
            }
            try FileManager.default.removeItem(at: window.dir)
        } catch {
            log.error("keeping paused window \(window.dir.lastPathComponent) failed: \(error)")
        }
        refresh()
    }

    /// Upload part of a window (WindowAudio.cropped) and delete the rest.
    /// The parts are written under temporary names and only then queued, so
    /// a failure leaves the window as it was rather than half of it sent.
    func upload(_ window: Window, cropped uploads: [Data]) {
        var parts: [URL] = []
        do {
            for upload in uploads {
                let part = spool.dir.appendingPathComponent(Spool.uploadName() + ".part")
                try upload.write(to: part, options: .atomic)
                parts.append(part)
            }
        } catch {
            log.error("cropping paused window \(window.dir.lastPathComponent) failed: \(error)")
            for part in parts { try? FileManager.default.removeItem(at: part) }
            return
        }
        for part in parts {
            try? FileManager.default.moveItem(at: part, to: part.deletingPathExtension())
        }
        delete(window)
    }

    func delete(_ window: Window) {
        try? FileManager.default.removeItem(at: window.dir)
        refresh()
    }

    /// Delete windows past retention; called every minute.
    func expire() {
        refresh()
        let cutoff = Date().addingTimeInterval(-retention)
        for window in windows where window.end < cutoff {
            log.info("deleting paused window \(window.dir.lastPathComponent), past retention")
            try? FileManager.default.removeItem(at: window.dir)
        }
        refresh()
    }

    /// Send the conversation going on from where it began, off the main
    /// thread; the window stays out of the list until it's done.
    private func lookBack(_ window: Spool) {
        window.seal()
        let files = window.pending(), dir = window.dir, spoolDir = spool.dir, now = Date().timeIntervalSince1970
        splitting.insert(dir)
        lookback = "Looking for where this conversation began."
        DispatchQueue.global(qos: .utility).async {
            let outcome: String
            do {
                if let split = try Lookback.split(files, now: now) {
                    try Self.apply(split, spool: spoolDir)
                    let began = Date(timeIntervalSince1970: split.began).formatted(date: .omitted, time: .shortened)
                    log.info("sent the conversation from \(began) out of paused window \(dir.lastPathComponent)")
                    outcome = "Sent this conversation from \(began), when it began; the paused audio before it is kept to review."
                } else {
                    outcome = "No conversation was going on; the paused audio is kept to review."
                }
            } catch {
                log.error("looking back through paused window \(dir.lastPathComponent) failed: \(error)")
                outcome = "Couldn't find where this conversation began (\(error.localizedDescription)); all of the paused audio is kept to review."
            }
            DispatchQueue.main.async {
                self.splitting.remove(dir)
                if self.window?.dir != dir && !Self.hasAudio(dir) { try? FileManager.default.removeItem(at: dir) }
                self.lookback = outcome
                self.refresh()
            }
        }
    }

    /// Queue the uploads, then cut the window's files down to what's left.
    /// Both are written under temporary names first, so a failure leaves the
    /// window as it was; a crash in between leaves the sent stretch in the
    /// window as well.
    private static func apply(_ split: Lookback.Split, spool: URL) throws {
        var parts: [URL] = []
        do {
            for upload in split.uploads {
                let part = spool.appendingPathComponent(Spool.uploadName() + ".part")
                try upload.write(to: part, options: .atomic)
                parts.append(part)
            }
            for rest in split.rest where !rest.data.isEmpty {
                let part = rest.file.appendingPathExtension("part")
                try rest.data.write(to: part, options: .atomic)
                parts.append(part)
            }
        } catch {
            for part in parts { try? FileManager.default.removeItem(at: part) }
            throw error
        }
        for part in parts where part.deletingLastPathComponent() == spool {
            try FileManager.default.moveItem(at: part, to: part.deletingPathExtension())
        }
        for rest in split.rest {
            if rest.data.isEmpty {
                try FileManager.default.removeItem(at: rest.file)
            } else {
                _ = try FileManager.default.replaceItemAt(rest.file, withItemAt: rest.file.appendingPathExtension("part"))
            }
        }
    }

    private func refresh() {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        windows = names.sorted().compactMap { name in
            let dir = root.appendingPathComponent(name)
            guard name != window?.dir.lastPathComponent, !splitting.contains(dir), let millis = Int64(name) else { return nil }
            // Opening it seals whatever a crash left in current.rec.
            guard let files = try? Spool(dir: dir).pending() else { return nil }
            var end = Date(timeIntervalSince1970: Double(millis) / 1000)
            var bytes = 0
            for file in files {
                let values = try? file.resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey])
                end = max(end, values?.contentModificationDate ?? end)
                bytes += values?.fileSize ?? 0
            }
            return Window(dir: dir, start: Date(timeIntervalSince1970: Double(millis) / 1000), end: end, bytes: bytes)
        }
    }

    private func endWindow() {
        window?.seal()
        if let ended = window?.dir, !splitting.contains(ended), !Self.hasAudio(ended) {
            log.info("paused window \(ended.lastPathComponent) has no audio; deleting it")
            try? FileManager.default.removeItem(at: ended)
        }
        window = nil
        UserDefaults.standard.removeObject(forKey: Self.windowKey)
        refresh()
    }

    /// Stops at the first audio record, so a window with audio costs little.
    private static func hasAudio(_ dir: URL) -> Bool {
        let files = (try? Spool(dir: dir).pending()) ?? []
        return files.contains { file in
            ((try? Spool.records(in: file)) ?? []).contains { $0.kind == Spool.audio || $0.kind == Spool.stored }
        }
    }

    /// nil (so paused audio is dropped) if the window can't be made.
    private func openWindow(_ name: String) -> Spool? {
        do {
            let opened = try Spool(dir: root.appendingPathComponent(name))
            UserDefaults.standard.set(name, forKey: Self.windowKey)
            return opened
        } catch {
            log.error("can't keep paused audio in \(name): \(error); dropping it")
            return nil
        }
    }

    private static func newWindowName() -> String {
        "\(Int64(Date().timeIntervalSince1970 * 1000))"
    }
}
