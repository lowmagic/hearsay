import AVFoundation
import Combine
import SwiftUI

/// Listen to a paused window, then upload one stretch of it (deleting the
/// rest), share a stretch as a WAV (deleting nothing), or delete it all.
/// Positions are seconds of audio, skipping silence; labels show the time of
/// day.
struct WindowView: View {
    let window: Pause.Window
    @ObservedObject var pause: Pause
    let uploader: Uploader

    @Environment(\.dismiss) private var dismiss
    @State private var audio: WindowAudio?
    @State private var failed: String?
    @State private var player: AVAudioPlayer?
    @State private var position = 0.0
    @State private var playing = false
    @State private var from = 0.0
    @State private var to = 0.0
    @State private var confirmUpload = false
    @State private var confirmDelete = false
    @State private var exporting = false
    /// The WAV being shared, kept until the share sheet has closed.
    @State private var exported: URL?
    @State private var sharing = false
    @State private var exportFailed: String?
    private let tick = Timer.publish(every: 0.2, on: .main, in: .common).autoconnect()

    var body: some View {
        Form {
            if let audio, audio.duration > 0 {
                editor(duration: audio.duration, clock: { clock(audio, $0) }, upload: { uploadSelection(audio) },
                       share: { shareSelection(audio) })
            } else if let failed {
                ContentUnavailableView("Can't Play This Window", systemImage: "exclamationmark.triangle",
                                       description: Text(failed))
            } else if audio != nil {
                ContentUnavailableView("No Audio in This Window", systemImage: "waveform.slash")
            } else {
                ProgressView("Decoding")
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            }
            Section {
                Button("Delete This Window", role: .destructive) { confirmDelete = true }
                    .confirmationDialog("Delete all of this window's audio?", isPresented: $confirmDelete,
                                        titleVisibility: .visible) {
                        Button("Delete", role: .destructive) {
                            pause.delete(window)
                            dismiss()
                        }
                    }
            }
        }
        .navigationTitle(window.start.formatted(date: .abbreviated, time: .shortened))
        .navigationBarTitleDisplayMode(.inline)
        .onAppear(perform: load)
        .onDisappear {
            player?.stop()
            audio?.close()
        }
        .sheet(isPresented: $sharing, onDismiss: removeExported) {
            if let exported { ShareSheet(file: exported) { sharing = false } }
        }
        .alert("Couldn't Share This Stretch", isPresented: Binding { exportFailed != nil } set: { if !$0 { exportFailed = nil } }) {
        } message: {
            Text(exportFailed ?? "")
        }
        .onReceive(tick) { _ in
            guard let player, playing else { return }
            position = player.currentTime
            if !player.isPlaying { playing = false }
        }
    }

    /// The player, then the stretch to keep. `clock` labels a position with
    /// its time of day.
    @ViewBuilder
    private func editor(duration: Double, clock: @escaping (Double) -> String, upload: @escaping () -> Void,
                        share: @escaping () -> Void) -> some View {
        Section {
            VStack(spacing: 16) {
                Text(clock(position))
                    .font(.largeTitle.weight(.semibold))
                    .monospacedDigit()
                    .contentTransition(.numericText())
                    .animation(.snappy, value: position)
                VStack(spacing: 4) {
                    Slider(value: $position, in: 0...duration) { editing in
                        if !editing { player?.currentTime = position }
                    }
                    .accessibilityLabel("Position")
                    .accessibilityValue(clock(position))
                    HStack {
                        Text(clock(0))
                        Spacer()
                        Text(clock(duration))
                    }
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                }
                Button { playing ? stop() : play() } label: {
                    Image(systemName: playing ? "pause.fill" : "play.fill")
                        .font(.title)
                        .contentTransition(.symbolEffect(.replace))
                        .frame(width: 64, height: 64)
                }
                .buttonStyle(.borderless)
                .background(.fill.tertiary, in: .circle)
                .accessibilityLabel(playing ? "Pause" : "Play")
            }
            .padding(.vertical, 8)
        }

        Section {
            bound("From", value: $from, duration: duration, clock: clock) {
                from = min(position, to)
            } play: {
                position = from
                player?.currentTime = from
                play()
            }
            bound("To", value: $to, duration: duration, clock: clock) {
                to = max(position, from)
            }
        } header: {
            Text("Keep")
        } footer: {
            Text("\(minutes(to - from)) of \(minutes(duration)) of audio. The rest is deleted from this phone.")
        }

        Section {
            Button { confirmUpload = true } label: {
                Text("Upload This Stretch")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .foregroundStyle(Color(.systemBackground))
            .disabled(to <= from)
            .listRowInsets(EdgeInsets())
            .listRowBackground(Color.clear)
            .confirmationDialog("Upload \(clock(from))–\(clock(to)) and delete the rest of this window?",
                                isPresented: $confirmUpload, titleVisibility: .visible) {
                Button("Upload", action: upload)
            }
        }

        Section {
            Button(action: share) {
                if exporting {
                    ProgressView().frame(maxWidth: .infinity)
                } else {
                    Label("Share This Stretch", systemImage: "square.and.arrow.up")
                        .frame(maxWidth: .infinity)
                }
            }
            .disabled(to <= from || exporting)
        } footer: {
            Text("A WAV of \(clock(from))–\(clock(to)), by AirDrop or to Files. Nothing is deleted, and the copy is yours: Hearsay doesn't delete it later.")
        }
    }

    /// One end of the stretch: its time, a button to move it to the playhead,
    /// and a slider.
    private func bound(_ title: String, value: Binding<Double>, duration: Double, clock: @escaping (Double) -> String,
                       here: @escaping () -> Void, play: (() -> Void)? = nil) -> some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                Text(title)
                Text(clock(value.wrappedValue))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                Spacer()
                if let play {
                    Button(action: play) { Image(systemName: "play.fill") }
                        .accessibilityLabel("Play from here")
                }
                Button("Here", action: here)
                    .accessibilityHint("Moves the \(title.lowercased()) point to the playhead")
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            Slider(value: value, in: 0...duration)
                .accessibilityLabel(title)
                .accessibilityValue(clock(value.wrappedValue))
        }
        .padding(.vertical, 4)
    }

    private func load() {
        guard audio == nil else { return }
        let dir = window.dir
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try WindowAudio(dir: dir) }
            DispatchQueue.main.async {
                switch result {
                case .success(let decoded):
                    audio = decoded
                    to = decoded.duration
                    player = try? AVAudioPlayer(contentsOf: decoded.wav)
                case .failure(let error):
                    failed = error.localizedDescription
                }
            }
        }
    }

    private func play() {
        try? AVAudioSession.sharedInstance().setCategory(.playback)
        try? AVAudioSession.sharedInstance().setActive(true)
        player?.play()
        playing = true
    }

    private func stop() {
        player?.pause()
        playing = false
    }

    private func uploadSelection(_ audio: WindowAudio) {
        player?.stop()
        pause.upload(window, cropped: audio.cropped(from: from, to: to))
        uploader.drain()
        dismiss()
    }

    private func shareSelection(_ audio: WindowAudio) {
        stop()
        exporting = true
        let from = from, to = to
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try audio.exported(from: from, to: to) }
            DispatchQueue.main.async {
                exporting = false
                switch result {
                case .success(let url):
                    exported = url
                    sharing = true
                case .failure(let error): exportFailed = error.localizedDescription
                }
            }
        }
    }

    /// The shared WAV's folder, once the share sheet has closed.
    private func removeExported() {
        guard let exported else { return }
        try? FileManager.default.removeItem(at: exported.deletingLastPathComponent())
        self.exported = nil
    }

    private func clock(_ audio: WindowAudio, _ position: Double) -> String {
        audio.wallTime(position).formatted(date: .omitted, time: .standard)
    }

    private func minutes(_ seconds: Double) -> String {
        Duration.seconds(max(seconds, 0)).formatted(.time(pattern: .minuteSecond))
    }
}

/// iOS's share sheet for a stretch's WAV, without the ways out that put it
/// on someone else's servers or devices: messages, mail, social networks,
/// the clipboard (Universal Clipboard reaches other devices). AirDrop and
/// Save to Files stay; other apps' share extensions can't be hidden.
private struct ShareSheet: UIViewControllerRepresentable {
    let file: URL
    let done: () -> Void

    func makeUIViewController(context: Context) -> UIActivityViewController {
        let sheet = UIActivityViewController(activityItems: [file], applicationActivities: nil)
        sheet.excludedActivityTypes = [
            .message, .mail, .postToFacebook, .postToTwitter, .postToWeibo, .postToTencentWeibo, .postToFlickr,
            .postToVimeo, .copyToPasteboard, .assignToContact, .print, .addToReadingList, .openInIBooks, .markupAsPDF,
            .sharePlay, .collaborationInviteWithLink, .collaborationCopyLink,
        ]
        sheet.completionWithItemsHandler = { _, _, _, _ in done() }
        return sheet
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
