// Hearsay's iPhone app: records the Omi pendant over BLE and uploads what it
// sends to Hearsay's capture receiver on the NAS, over the tailnet. See Spool
// for the upload format and hearsay/capture.py for how the NAS reads it.

import os
import SwiftUI

let log = Logger(subsystem: "hearsay", category: "capture")

/// The recording runs from launch, whether or not a screen is shown: iOS
/// relaunches the app in the background for the pendant (state restoration).
final class Recorder {
    static let shared = Recorder()

    let spool: Spool
    let pendant: Pendant
    let pause: Pause
    let buttons: Buttons
    let uploader: Uploader
    let location: Location

    private init() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = support.appendingPathComponent("spool")
        do {
            spool = try Spool(dir: dir)
        } catch {
            fatalError("can't use the spool at \(dir.path): \(error)")
        }
        pendant = Pendant()
        pause = Pause(root: support.appendingPathComponent("paused"), spool: spool, pendant: pendant)
        buttons = Buttons(pendant: pendant, pause: pause)
        uploader = Uploader(spool: spool)
        location = Location { [spool, pause] in
            switch pause.mode {
            case .off: nil
            case .paused: pause.window
            case .on: spool
            }
        }

        // Uploads cover a minute each; the NAS waits for 3 minutes without
        // speech before it transcribes, so there's
        // no point in smaller ones. Timers only fire while the app runs,
        // which in the background is while the pendant is sending.
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [spool, pause, location] _ in
            spool.sync()
            pause.window?.sync()
            location.tick()
        }
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [spool, pendant, pause] _ in
            spool.seal()
            pause.window?.seal()
            pause.expire()
            pendant.report()
        }
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [uploader] _ in uploader.drain() }
        uploader.drain()
    }
}

@main
struct HearsayApp: App {
    private let recorder = Recorder.shared

    var body: some Scene {
        WindowGroup {
            TabView {
                StatusView(pendant: recorder.pendant, pause: recorder.pause, buttons: recorder.buttons,
                           uploader: recorder.uploader, location: recorder.location)
                    .tabItem { Label("Recorder", image: "Voiceprint") }
                TagView()
                    .tabItem { Label("Tag", systemImage: "tag") }
                PlacesView()
                    .tabItem { Label("Places", systemImage: "map") }
            }
            .tint(.ink)
        }
    }
}

/// The orange at the core of the app icon: the pendant's audio reaching
/// Hearsay right now, and, faintly, the area a named place covers on the map.
extension Color {
    static let live = Color(red: 1, green: 0x5A / 255, blue: 0x1F / 255)

    /// The icon's rings: the tint for everything interactive, so the orange
    /// stays the only color on screen.
    static let ink = Color(UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 0xE8 / 255, green: 0xE4 / 255, blue: 0xDA / 255, alpha: 1)
            : UIColor(red: 0x1A / 255, green: 0x19 / 255, blue: 0x18 / 255, alpha: 1)
    })
}

struct StatusView: View {
    @ObservedObject var pendant: Pendant
    @ObservedObject var pause: Pause
    @ObservedObject var buttons: Buttons
    @ObservedObject var uploader: Uploader
    @ObservedObject var location: Location

    var body: some View {
        NavigationStack {
            List {
                Section {
                    header
                    HStack(spacing: 12) {
                        ForEach(Pause.Mode.allCases, id: \.self) { mode in
                            control(mode.label, symbol: mode.symbol, on: pause.mode == mode) { pause.set(mode) }
                        }
                    }
                    .listRowSeparator(.hidden)
                } footer: {
                    Text(recordingStatus)
                }

                Section("Activity") {
                    if !pendant.lastMinute.isEmpty { detail("Last minute", pendant.lastMinute) }
                    if !pendant.stored.isEmpty { detail("Stored while away", pendant.stored) }
                    HStack {
                        detail("Uploads", "\(uploader.pending) waiting · \(uploader.lastResult)")
                        Spacer()
                        Button("Send Now") { uploader.drain() }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                }

                if !pause.windows.isEmpty {
                    Section {
                        ForEach(pause.windows) { window in
                            NavigationLink {
                                WindowView(window: window, pause: pause, uploader: uploader)
                            } label: {
                                detail(timeRange(window), details(window))
                            }
                            .swipeActions {
                                Button("Delete", role: .destructive) { pause.delete(window) }
                                Button("Upload") {
                                    pause.upload(window)
                                    uploader.drain()
                                }
                                .tint(.blue)
                            }
                        }
                    } header: {
                        Text("Paused audio to review")
                    } footer: {
                        Text("Tap to listen and keep a stretch. Swipe to upload or delete all of it.")
                    }
                }
            }
            .navigationTitle(headline)
            .toolbar {
                NavigationLink {
                    SettingsView(pendant: pendant, pause: pause, buttons: buttons,
                                 uploader: uploader, location: location)
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
            }
        }
    }

    private var connected: Bool { pendant.state == "connected" }
    private var live: Bool { connected && pause.mode == .on }

    /// The pendant's own state, under the title that says what's happening.
    private var header: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: "circle.fill")
                .font(.caption)
                .foregroundStyle(live ? Color.live : Color.secondary.opacity(0.5))
                .symbolEffect(.pulse, isActive: live)
                .accessibilityHidden(true)
            Text(connected ? "Pendant connected" : pendant.state.prefix(1).uppercased() + pendant.state.dropFirst())
                .foregroundStyle(.secondary)
            Spacer()
            if let battery = pendant.battery {
                Label("\(battery)%", systemImage: batterySymbol(battery))
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var headline: String {
        switch pause.mode {
        case .off: "Off"
        case .paused: "Paused"
        case .on: connected ? "Recording" : "Pendant disconnected"
        }
    }

    /// Filled while its state is on, so the button shows the state too.
    @ViewBuilder
    private func control(_ title: String, symbol: String, on: Bool, action: @escaping () -> Void) -> some View {
        let button = Button(action: action) {
            Label(title, systemImage: symbol)
                .fontWeight(.medium)
                .frame(maxWidth: .infinity)
        }
        .controlSize(.large)
        .tint(.ink)
        if on {
            button.buttonStyle(.borderedProminent).foregroundStyle(Color(.systemBackground))
        } else {
            button.buttonStyle(.bordered).foregroundStyle(.primary)
        }
    }

    private func detail(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
            Text(value)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
    }

    private var recordingStatus: String {
        switch pause.mode {
        case .off: "Mic off on the pendant, even out of range. Nothing is kept."
        case .paused: "Kept on this phone for \(pause.retentionDays) days, never sent unless you upload it. Switching on mid-conversation sends it from where it began."
        case .on: pause.lookback ?? "Everything the pendant hears reaches Hearsay."
        }
    }

    private func batterySymbol(_ level: Int) -> String {
        if pendant.isCharging == true { return "battery.100.bolt" }
        return "battery.\([0, 25, 50, 75, 100].min { abs($0 - level) < abs($1 - level) }!)"
    }

    private func timeRange(_ window: Pause.Window) -> String {
        let start = window.start.formatted(.dateTime.month(.abbreviated).day().hour().minute())
        return "\(start) – \(window.end.formatted(date: .omitted, time: .shortened))"
    }

    private func details(_ window: Pause.Window) -> String {
        let length = Duration.seconds(window.end.timeIntervalSince(window.start))
            .formatted(.units(allowed: [.hours, .minutes, .seconds], width: .narrow, maximumUnitCount: 2))
        let size = ByteCountFormatter.string(fromByteCount: Int64(window.bytes), countStyle: .file)
        let expires = window.end.addingTimeInterval(pause.retention).formatted(.dateTime.month(.abbreviated).day())
        return "\(length), \(size), deleted \(expires)"
    }
}

extension Pause.Mode {
    var label: String {
        switch self {
        case .off: "Off"
        case .paused: "Paused"
        case .on: "On"
        }
    }

    var symbol: String {
        switch self {
        case .off: "mic.slash.fill"
        case .paused: "pause.fill"
        case .on: "mic.fill"
        }
    }
}

extension Movement.State {
    var label: String {
        switch self {
        case .off: "Not recording"
        case .uncertain: "Unsure"
        case .stationary: "Staying put"
        case .walking: "Walking"
        case .cycling: "Cycling"
        case .driving: "Driving"
        }
    }
}
