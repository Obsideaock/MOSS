import SwiftUI

// MOSS Radio: range-test the link to a Seed Vault, and read Wikipedia from it,
// through a MeshCore radio on Bluetooth. Part of MOSS (github.com/Obsideaock/MOSS).

let mossGreen = Color(red: 0.31, green: 0.60, blue: 0.36)
let appVersion = "0.1.0"

@main
struct MossRadioApp: App {
    @StateObject private var link: RadioLink
    @StateObject private var client: MossClient
    @StateObject private var location: LocationService
    @StateObject private var tester: RangeTester
    @StateObject private var library: Library

    init() {
        let link = RadioLink()
        let location = LocationService()
        _link = StateObject(wrappedValue: link)
        _location = StateObject(wrappedValue: location)
        _client = StateObject(wrappedValue: MossClient(link: link))
        _tester = StateObject(wrappedValue: RangeTester(link: link, location: location))
        _library = StateObject(wrappedValue: Library())
    }

    var body: some Scene {
        WindowGroup {
            TabView {
                RadioView()
                    .tabItem { Label("Radio", systemImage: "antenna.radiowaves.left.and.right") }
                RangeView()
                    .tabItem { Label("Range", systemImage: "map") }
                ReadView()
                    .tabItem { Label("Read", systemImage: "book") }
                VaultView()
                    .tabItem { Label("Vault", systemImage: "leaf") }
            }
            .tint(mossGreen)
            .environmentObject(link)
            .environmentObject(client)
            .environmentObject(location)
            .environmentObject(tester)
            .environmentObject(library)
        }
    }
}

/// "12 s ago", "3 min ago"
func ago(_ date: Date) -> String {
    let s = Int(Date().timeIntervalSince(date))
    if s < 60 { return "\(max(s, 0)) s ago" }
    if s < 3600 { return "\(s / 60) min ago" }
    return date.formatted(date: .omitted, time: .shortened)
}

func signalText(snr: Double?, rssi: Int?) -> String {
    var parts: [String] = []
    if let s = snr { parts.append(String(format: "SNR %.1f dB", s)) }
    if let r = rssi { parts.append("RSSI \(r) dBm") }
    return parts.isEmpty ? "signal unknown" : parts.joined(separator: " · ")
}

func distanceText(_ metres: Double?) -> String {
    guard let m = metres else { return "" }
    if m < 1000 { return String(format: "%.0f m", m) }
    return String(format: "%.2f km", m / 1000)
}
