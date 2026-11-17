import SwiftUI

struct RadioView: View {
    @EnvironmentObject var link: RadioLink

    var body: some View {
        NavigationStack {
            List {
                Section {
                    statusRow
                    if let p = link.problem {
                        Text(p)
                            .font(.callout)
                            .foregroundStyle(.red)
                    }
                    actions
                }

                if link.isReady, let info = link.info {
                    Section("Radio") {
                        LabeledContent("Name", value: link.radioName)
                        LabeledContent("Settings", value: info.summary)
                        if !link.model.isEmpty {
                            LabeledContent("Model", value: link.model)
                        }
                        if !link.firmware.isEmpty {
                            LabeledContent("Firmware", value: link.firmware)
                        }
                        LabeledContent(link.channelName, value: "slot \(link.slot ?? -1)")
                    }
                }

                if link.phase == .scanning || (!link.radios.isEmpty && !link.isReady) {
                    Section {
                        if link.radios.isEmpty {
                            HStack {
                                ProgressView()
                                Text("Looking for MeshCore radios…")
                                    .foregroundStyle(.secondary)
                            }
                        }
                        ForEach(link.radios) { r in
                            Button {
                                link.connect(r)
                            } label: {
                                HStack {
                                    Image(systemName: "antenna.radiowaves.left.and.right")
                                    Text(r.name)
                                    Spacer()
                                    Text("\(r.rssi) dBm")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    } header: {
                        Text("Radios nearby")
                    } footer: {
                        Text("Tap one to connect. The first time, iOS asks for its PIN: it's in the MeshCore app (that radio's settings) or on the radio's screen.")
                    }
                }

                Section {
                    if link.heard.isEmpty {
                        Text(link.isReady ? "Nothing yet." : "Connect to a radio first.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(link.heard.prefix(40)) { h in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(h.what)
                                .lineLimit(2)
                            Text("\(ago(h.at)) · \(signalText(snr: h.snr, rssi: h.rssi)) · \(describeHops(h.hops))")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                } header: {
                    Text("Heard on \(link.channelName)")
                } footer: {
                    Text("Everything MOSS-related your radio hears, with its signal. \"direct\" means no repeater was involved.")
                }

                if !link.notes.isEmpty {
                    Section("Notes") {
                        ForEach(Array(link.notes.reversed().enumerated()), id: \.offset) { item in
                            Text(item.element)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("Radio")
        }
    }

    private var statusRow: some View {
        HStack {
            Circle()
                .fill(statusColor)
                .frame(width: 10, height: 10)
            Text(statusText)
            Spacer()
            if link.phase == .connecting || link.phase == .preparing {
                ProgressView()
            }
        }
    }

    private var statusText: String {
        switch link.phase {
        case .bluetoothOff: return "Bluetooth unavailable"
        case .idle: return link.lastRadioID == nil ? "No radio yet" : "Not connected"
        case .scanning: return "Looking for radios"
        case .connecting: return "Connecting to \(link.radioName)…"
        case .preparing: return "Setting up \(link.channelName) on \(link.radioName)…"
        case .ready: return "Connected to \(link.radioName)"
        }
    }

    private var statusColor: Color {
        switch link.phase {
        case .ready: return .green
        case .connecting, .preparing, .scanning: return .orange
        default: return .gray
        }
    }

    @ViewBuilder
    private var actions: some View {
        switch link.phase {
        case .idle, .bluetoothOff:
            Button {
                link.scan()
            } label: {
                Label("Find radios", systemImage: "magnifyingglass")
            }
            if link.lastRadioID != nil {
                Button {
                    link.reconnectLast()
                } label: {
                    Label("Reconnect to the last radio", systemImage: "arrow.triangle.2.circlepath")
                }
            }
        case .scanning:
            Button("Stop looking") { link.stopScan() }
        case .connecting, .preparing:
            Button("Cancel", role: .cancel) { link.disconnect() }
        case .ready:
            Button {
                Task { await link.sendText("moss ping from \(link.radioName) (iPhone)") }
            } label: {
                Label("Send a chat ping", systemImage: "dot.radiowaves.right")
            }
            Button("Disconnect") { link.disconnect() }
            Button("Forget this radio", role: .destructive) { link.forgetRadio() }
        }
    }
}
