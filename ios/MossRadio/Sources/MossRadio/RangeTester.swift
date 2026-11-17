import Combine
import CoreLocation
import Foundation

// Range testing: every few seconds, send a PING on #moss; the Seed Vault
// answers each with a PONG saying how well it heard us, and we note how
// well we heard the answer. Each ping is logged with where the phone was.
// Pings are zero-hop by default (no repeaters), so the test measures the
// direct link and doesn't load the shared mesh.

struct PingRecord: Codable, Identifiable {
    var seq: Int
    var sentAt: Date
    var latitude: Double?
    var longitude: Double?
    var accuracy: Double?
    var altitude: Double?
    var distance: Double?           // metres to the Seed Vault, if its spot is set
    var sent = true                 // the radio took it
    var answered = false
    var vaultSNR: Double?           // how the vault heard us
    var vaultRSSI: Int?
    var vaultHops: Int?
    var phoneSNR: Double?           // how we heard the vault's answer
    var phoneRSSI: Int?
    var phoneHops: Int?
    var roundTrip: Double?          // seconds

    var id: Int { seq }

    var coordinate: CLLocationCoordinate2D? {
        guard let la = latitude, let lo = longitude else { return nil }
        return CLLocationCoordinate2D(latitude: la, longitude: lo)
    }
}

struct RangeRun: Codable, Identifiable {
    var id = UUID()
    var started = Date()
    var runID: UInt16
    var interval: Double
    var zeroHop: Bool
    var vaultName = ""
    var vaultLatitude: Double?
    var vaultLongitude: Double?
    var records: [PingRecord] = []

    var answered: Int { records.filter { $0.answered }.count }
    var sentCount: Int { records.filter { $0.sent }.count }

    var title: String {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f.string(from: started)
    }

    var farthestAnswered: Double? {
        return records.filter { $0.answered }.compactMap { $0.distance }.max()
    }
}

@MainActor
final class RangeTester: ObservableObject {
    @Published private(set) var runs: [RangeRun] = []
    @Published private(set) var running = false
    @Published private(set) var currentID: UUID? = nil
    @Published var interval: Double {
        didSet { UserDefaults.standard.set(interval, forKey: "pingInterval") }
    }
    @Published var zeroHop: Bool {
        didSet { UserDefaults.standard.set(zeroHop, forKey: "pingZeroHop") }
    }
    @Published private(set) var vaultSpot: CLLocationCoordinate2D? = nil
    @Published private(set) var lastEvent = ""

    static let file = "range-runs.json"
    let link: RadioLink
    let location: LocationService
    private var task: Task<Void, Never>?
    private var listener: UUID?

    init(link: RadioLink, location: LocationService) {
        self.link = link
        self.location = location
        let d = UserDefaults.standard
        interval = d.object(forKey: "pingInterval") as? Double ?? 10
        zeroHop = d.object(forKey: "pingZeroHop") as? Bool ?? true
        if let la = d.object(forKey: "vaultLat") as? Double, let lo = d.object(forKey: "vaultLon") as? Double {
            vaultSpot = CLLocationCoordinate2D(latitude: la, longitude: lo)
        }
        runs = Files.load([RangeRun].self, from: RangeTester.file) ?? []
        listener = link.listen { [weak self] d in
            self?.heard(d)
        }
    }

    var current: RangeRun? {
        guard let id = currentID else { return nil }
        return runs.first(where: { $0.id == id })
    }

    // ------------------------------------------------------------ the Seed Vault's spot

    func setVaultSpotHere() {
        guard let here = location.location?.coordinate else {
            lastEvent = "No GPS fix yet; try again in a moment."
            return
        }
        setVaultSpot(here)
    }

    func setVaultSpot(_ c: CLLocationCoordinate2D?) {
        vaultSpot = c
        let d = UserDefaults.standard
        if let c = c {
            d.set(c.latitude, forKey: "vaultLat")
            d.set(c.longitude, forKey: "vaultLon")
        } else {
            d.removeObject(forKey: "vaultLat")
            d.removeObject(forKey: "vaultLon")
        }
    }

    // ------------------------------------------------------------ running a test

    func start() {
        guard !running else { return }
        var run = RangeRun(runID: UInt16.random(in: 1...0xFFFF), interval: interval, zeroHop: zeroHop)
        run.vaultLatitude = vaultSpot?.latitude
        run.vaultLongitude = vaultSpot?.longitude
        runs.insert(run, at: 0)
        currentID = run.id
        running = true
        location.start(background: true)
        save()
        lastEvent = "Started. Walk away from the Seed Vault; pings go out every \(Int(interval)) s."
        task = Task { @MainActor [weak self] in
            var seq = 0
            while let self = self, self.running, !Task.isCancelled {
                seq += 1
                await self.ping(seq)
                await snooze(self.interval)
            }
        }
    }

    func stop() {
        running = false
        task?.cancel()
        task = nil
        location.stop()
        currentID = nil
        save()
        lastEvent = "Stopped."
    }

    func delete(_ run: RangeRun) {
        runs.removeAll { $0.id == run.id }
        save()
    }

    private func ping(_ seq: Int) async {
        guard let id = currentID, let i = runs.firstIndex(where: { $0.id == id }) else { return }
        let run = runs[i]
        var r = PingRecord(seq: seq, sentAt: Date())
        if let loc = location.location, Date().timeIntervalSince(loc.timestamp) < 30 {
            r.latitude = loc.coordinate.latitude
            r.longitude = loc.coordinate.longitude
            r.accuracy = loc.horizontalAccuracy
            r.altitude = loc.altitude
            if let la = run.vaultLatitude, let lo = run.vaultLongitude {
                r.distance = loc.distance(from: CLLocation(latitude: la, longitude: lo))
            }
        }
        guard link.isReady else {
            r.sent = false
            append(r, to: id)
            lastEvent = "Ping \(seq): radio not connected."
            return
        }
        append(r, to: id)
        let ok = await link.send(Moss.makePing(runID: run.runID, seq: UInt16(seq & 0xFFFF), zeroHop: run.zeroHop),
                                 zeroHop: run.zeroHop)
        if !ok {
            update(id, seq) { $0.sent = false }
            lastEvent = "Ping \(seq): the radio wouldn't send it."
        } else {
            lastEvent = "Ping \(seq) sent" + (r.distance.map { String(format: " at %.0f m", $0) } ?? "")
            if seq >= 4, let now = runs.first(where: { $0.id == id }), now.answered == 0 {
                lastEvent += ". No answers yet: out of range, or the Seed Vault needs MOSS 0.6.1 or newer."
            }
        }
    }

    private func heard(_ d: Datagram) {
        guard let pong = Moss.parsePong(d.payload), let id = currentID,
              let i = runs.firstIndex(where: { $0.id == id }), pong.runID == runs[i].runID else { return }
        let seq = Int(pong.seq)
        update(id, seq) { r in
            if r.answered { return }
            r.answered = true
            r.vaultSNR = pong.snr
            r.vaultRSSI = pong.rssi
            r.vaultHops = Int(pong.hops)
            r.phoneSNR = d.snr
            r.phoneRSSI = d.rssi
            r.phoneHops = Int(d.hops)
            r.roundTrip = Date().timeIntervalSince(r.sentAt)
        }
        if runs[i].vaultName.isEmpty && !pong.name.isEmpty {
            runs[i].vaultName = pong.name
        }
        let them = pong.snr.map { String(format: "%.1f", $0) } ?? "?"
        lastEvent = "Ping \(seq) answered: the vault heard you at SNR \(them) dB, you heard it at \(String(format: "%.1f", d.snr)) dB."
        save()
    }

    private func append(_ r: PingRecord, to id: UUID) {
        guard let i = runs.firstIndex(where: { $0.id == id }) else { return }
        runs[i].records.append(r)
        save()
    }

    private func update(_ id: UUID, _ seq: Int, _ change: (inout PingRecord) -> Void) {
        guard let i = runs.firstIndex(where: { $0.id == id }),
              let j = runs[i].records.firstIndex(where: { $0.seq == seq }) else { return }
        change(&runs[i].records[j])
        save()
    }

    private func save() {
        Files.save(runs, to: RangeTester.file)
    }

    // ------------------------------------------------------------ export

    /// The run as a CSV file (for a spreadsheet), written to a temporary file.
    func csv(_ run: RangeRun) -> URL {
        var lines = ["seq,time,latitude,longitude,accuracy_m,altitude_m,distance_m,sent,answered,"
                     + "vault_snr_db,vault_rssi_dbm,vault_hops,phone_snr_db,phone_rssi_dbm,phone_hops,round_trip_s"]
        let iso = ISO8601DateFormatter()
        func n(_ v: Double?, _ f: String = "%.6f") -> String { v.map { String(format: f, $0) } ?? "" }
        func i(_ v: Int?) -> String { v.map { String($0) } ?? "" }
        for r in run.records {
            let cells: [String] = [
                String(r.seq), iso.string(from: r.sentAt),
                n(r.latitude), n(r.longitude), n(r.accuracy, "%.1f"), n(r.altitude, "%.1f"),
                n(r.distance, "%.1f"), r.sent ? "1" : "0", r.answered ? "1" : "0",
                n(r.vaultSNR, "%.2f"), i(r.vaultRSSI), i(r.vaultHops),
                n(r.phoneSNR, "%.2f"), i(r.phoneRSSI), i(r.phoneHops), n(r.roundTrip, "%.2f"),
            ]
            lines.append(cells.joined(separator: ","))
        }
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd-HHmm"
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("moss-range-\(f.string(from: run.started)).csv")
        try? lines.joined(separator: "\n").write(to: url, atomically: true, encoding: .utf8)
        return url
    }
}
