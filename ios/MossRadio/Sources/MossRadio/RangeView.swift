import CoreLocation
import MapKit
import SwiftUI

struct RangeView: View {
    @EnvironmentObject var link: RadioLink
    @EnvironmentObject var tester: RangeTester
    @EnvironmentObject var location: LocationService
    @EnvironmentObject var client: MossClient
    @State private var camera: MapCameraPosition = .userLocation(fallback: .automatic)

    private let intervals: [Double] = [5, 10, 15, 30, 60]

    var body: some View {
        NavigationStack {
            List {
                Section {
                    RangeMap(run: tester.current ?? tester.runs.first, vault: tester.vaultSpot, camera: $camera)
                        .frame(height: 280)
                        .listRowInsets(EdgeInsets())
                }

                Section {
                    if tester.running {
                        Button(role: .destructive) {
                            tester.stop()
                        } label: {
                            Label("Stop the test", systemImage: "stop.circle.fill")
                        }
                    } else {
                        Button {
                            tester.start()
                        } label: {
                            Label("Start a range test", systemImage: "play.circle.fill")
                        }
                        .disabled(!link.isReady)
                    }
                    if !tester.lastEvent.isEmpty {
                        Text(tester.lastEvent)
                            .font(.callout)
                    }
                    if let run = tester.current {
                        RunSummary(run: run)
                    }
                    if !link.isReady {
                        Text("Connect to your radio in the Radio tab first.")
                            .foregroundStyle(.secondary)
                    }
                    if let v = client.vault, (v.protocolLevel ?? 0) < 4 {
                        Text("Your Seed Vault (MOSS \(v.version ?? "older than 0.6")) doesn't answer pings yet. On the Pi: moss update.")
                            .font(.callout)
                            .foregroundStyle(.orange)
                    }
                    if !location.allowed {
                        Text("Location is off for MOSS Radio, so pings won't be placed on the map. Settings → MOSS Radio → Location.")
                            .font(.callout)
                            .foregroundStyle(.orange)
                    }
                } footer: {
                    Text("Every few seconds the phone sends a ping on #moss. The Seed Vault answers each one with how well it heard you; the phone notes how well it heard the answer, and where you were.")
                }

                Section {
                    Picker("Ping every", selection: $tester.interval) {
                        ForEach(intervals, id: \.self) { s in
                            Text("\(Int(s)) s").tag(s)
                        }
                    }
                    .disabled(tester.running)
                    Toggle("Direct only (no repeaters)", isOn: $tester.zeroHop)
                        .disabled(tester.running)
                    if let spot = tester.vaultSpot {
                        LabeledContent("Seed Vault's spot",
                                       value: String(format: "%.5f, %.5f", spot.latitude, spot.longitude))
                        if let here = location.location {
                            LabeledContent("You're", value: distanceText(here.distance(from: CLLocation(latitude: spot.latitude, longitude: spot.longitude))) + " away")
                        }
                        Button("Forget the Seed Vault's spot", role: .destructive) {
                            tester.setVaultSpot(nil)
                        }
                    } else {
                        Button {
                            tester.setVaultSpotHere()
                        } label: {
                            Label("I'm at the Seed Vault: set its spot here", systemImage: "mappin.and.ellipse")
                        }
                    }
                } header: {
                    Text("Settings")
                } footer: {
                    Text("Direct only: pings go to radios in range, never through repeaters, so the test measures the direct link and doesn't load the shared mesh. Turn it off to test reaching the vault through repeaters. Setting the vault's spot gives each ping a distance.")
                }

                if let run = tester.current, !run.records.isEmpty {
                    Section("This test") {
                        ForEach(run.records.reversed()) { r in
                            PingRow(record: r)
                        }
                    }
                }

                Section("Earlier tests") {
                    if tester.runs.isEmpty {
                        Text("None yet.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(tester.runs) { run in
                        NavigationLink {
                            RunDetail(run: run)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(run.title)
                                Text("\(run.answered) of \(run.sentCount) answered" +
                                     (run.farthestAnswered.map { " · farthest answer \(distanceText($0))" } ?? ""))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .onDelete { offsets in
                        let doomed = offsets.map { tester.runs[$0] }
                        for run in doomed where run.id != tester.currentID {
                            tester.delete(run)
                        }
                    }
                }
            }
            .navigationTitle("Range test")
            .onAppear { location.start(background: false) }
            .onDisappear {
                if !tester.running { location.stop() }
            }
        }
    }
}

/// Green: heard both ways. Red: sent, no answer. Grey: never left the phone.
func pingColor(_ r: PingRecord) -> Color {
    if r.answered { return .green }
    if r.sent { return .red }
    return .gray
}

struct RangeMap: View {
    let run: RangeRun?
    let vault: CLLocationCoordinate2D?
    @Binding var camera: MapCameraPosition

    var body: some View {
        Map(position: $camera) {
            UserAnnotation()
            if let v = vault {
                Marker("Seed Vault", systemImage: "leaf.fill", coordinate: v)
                    .tint(mossGreen)
            }
            if let run = run {
                ForEach(run.records.filter { $0.coordinate != nil }) { r in
                    Annotation("", coordinate: r.coordinate!, anchor: .center) {
                        Circle()
                            .fill(pingColor(r))
                            .frame(width: 11, height: 11)
                            .overlay(Circle().stroke(Color.white, lineWidth: 1.5))
                    }
                }
            }
        }
        .mapStyle(.standard(pointsOfInterest: .excludingAll))
        .mapControls {
            MapUserLocationButton()
            MapCompass()
            MapScaleView()
        }
    }
}

struct RunSummary: View {
    let run: RangeRun

    var body: some View {
        let answered = run.records.filter { $0.answered }
        let best = answered.compactMap { $0.vaultSNR }.min()
        VStack(alignment: .leading, spacing: 4) {
            Text("\(run.answered) of \(run.sentCount) pings answered")
                .font(.headline)
            if let far = run.farthestAnswered {
                Text("Farthest answered: \(distanceText(far))")
            }
            if let b = best {
                Text(String(format: "Weakest answered ping: SNR %.1f dB at the vault", b))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct PingRow: View {
    let record: PingRecord

    var body: some View {
        HStack(alignment: .top) {
            Circle()
                .fill(pingColor(record))
                .frame(width: 10, height: 10)
                .padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text("Ping \(record.seq)")
                        .font(.headline)
                    if let d = record.distance {
                        Text(distanceText(d))
                    }
                    Spacer()
                    Text(record.sentAt.formatted(date: .omitted, time: .standard))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if record.answered {
                    Text("Vault heard you: \(signalText(snr: record.vaultSNR, rssi: record.vaultRSSI)), \(describeHops(record.vaultHops.map { UInt8(clamping: $0) }))")
                        .font(.caption)
                    Text("You heard it: \(signalText(snr: record.phoneSNR, rssi: record.phoneRSSI)), \(describeHops(record.phoneHops.map { UInt8(clamping: $0) }))")
                        .font(.caption)
                    if let t = record.roundTrip {
                        Text(String(format: "Answer after %.1f s", t))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if record.sent {
                    Text("No answer")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Text("Not sent (radio busy or disconnected)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

struct RunDetail: View {
    @EnvironmentObject var tester: RangeTester
    let run: RangeRun
    @State private var camera: MapCameraPosition = .automatic

    var vaultSpot: CLLocationCoordinate2D? {
        guard let la = run.vaultLatitude, let lo = run.vaultLongitude else { return nil }
        return CLLocationCoordinate2D(latitude: la, longitude: lo)
    }

    var body: some View {
        List {
            Section {
                RangeMap(run: run, vault: vaultSpot, camera: $camera)
                    .frame(height: 300)
                    .listRowInsets(EdgeInsets())
            }
            Section {
                RunSummary(run: run)
                LabeledContent("Pinged every", value: "\(Int(run.interval)) s")
                LabeledContent("Route", value: run.zeroHop ? "direct only" : "repeaters allowed")
                if !run.vaultName.isEmpty {
                    LabeledContent("Seed Vault", value: run.vaultName)
                }
                ShareLink(item: tester.csv(run)) {
                    Label("Export as CSV", systemImage: "square.and.arrow.up")
                }
            }
            Section("Pings") {
                ForEach(run.records.reversed()) { r in
                    PingRow(record: r)
                }
            }
        }
        .navigationTitle(run.title)
        .navigationBarTitleDisplayMode(.inline)
    }
}
