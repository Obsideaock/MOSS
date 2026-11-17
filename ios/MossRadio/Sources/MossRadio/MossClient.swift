import Combine
import CryptoKit
import Foundation

// Asking the Seed Vault for something and collecting the answer: a port of
// Requester in moss/client.py, with the same timings. Every request is
// encrypted; the reply arrives as numbered spores; missing ones are asked
// for again ("bare patches") until the page is whole and checks out.

struct TransferProgress {
    var stage = "requesting"     // requesting, queued, fetching, receiving, repairing, verifying
    var message = ""
    var elapsed: Double = 0
    var total = 0                // spores expected (0 until the capsule)
    var have: Set<Int> = []
    var eta: Double?
    var queuePosition = 0
    var attempt = 0

    var fraction: Double { total > 0 ? Double(have.count) / Double(total) : 0 }
}

struct Fetched {
    var name: String
    var content: Data
    var elapsed: Double
    var spores: Int
}

/// Everything known about the request in flight.
private final class Exchange {
    let rid: UInt16
    let seal: ReaderSession
    var capsule: ReaderSession.Capsule?
    var chunks: [Int: Data] = [:]
    var early: [Int: Data] = [:]          // spores that came before the capsule
    var orphans = 0                       // ...since the last re-ask
    var status: (state: UInt8, value: Int)?
    var statusAt = uptime()
    var error: String?
    var lastHeard = uptime()
    var firstChunkAt: Double?
    var badCapsules = 0

    init(rid: UInt16, seal: ReaderSession) {
        self.rid = rid
        self.seal = seal
    }

    func take(_ raw: Data) {
        guard raw.count >= 3, Moss.peekID(raw) == rid else { return }     // someone else's conversation
        let kind = raw.at(0)
        lastHeard = uptime()
        switch kind {
        case Moss.sealedCapsule:
            if capsule != nil { return }
            do {
                let c = try seal.openCapsule(raw)
                capsule = c
                for (i, p) in early where i < c.total {
                    chunks[i] = p
                }
                early = [:]
                if !chunks.isEmpty && firstChunkAt == nil { firstChunkAt = uptime() }
            } catch {
                badCapsules += 1             // not ours (same number), or tampered with
            }
        case Moss.chunk:
            guard let parsed = Moss.parseChunk(raw) else { return }
            let index = parsed.index
            let payload = parsed.payload
            if let c = capsule {
                if index < c.total && chunks[index] == nil {
                    if firstChunkAt == nil { firstChunkAt = uptime() }
                    chunks[index] = payload
                }
            } else {
                if early[index] == nil { early[index] = payload }
                orphans += 1
            }
        case Moss.status:
            if let s = Moss.parseStatus(raw) {
                if status == nil || status!.state != s.state || status!.value != s.value {
                    statusAt = uptime()
                }
                status = s
            }
        case Moss.sealedError:
            if let text = try? seal.openError(raw) {
                error = text.isEmpty ? "the Seed Vault refused the request" : text
            }
        default:
            break
        }
    }
}

@MainActor
final class MossClient: ObservableObject {
    @Published private(set) var vault: TrustedVault? = nil
    @Published private(set) var otherVault: String? = nil      // a vault with a different key answered
    @Published var notice: String? = nil

    let link: RadioLink
    let keyring = Keyring()

    // Timings, as in the laptop reader.
    var quiet = 6.0              // silence before asking for missing spores
    var requestTimeout = 25.0    // before repeating an unanswered request
    var requestAttempts = 4
    var fetchTimeout = 120.0
    var idle = 120.0
    var maxNacks = 14
    var manifestRetry = 4.0
    var nackSpacing = 1.0

    private var busy = false
    private var active: Exchange?
    private var heardHello = false
    private var listener: UUID?

    init(link: RadioLink) {
        self.link = link
        vault = keyring.vault
        listener = link.listen { [weak self] d in
            self?.on(d)
        }
    }

    private var lastHelloSignal: (snr: Double, rssi: Int?, hops: UInt8)?

    private func on(_ d: Datagram) {
        let raw = d.payload
        if raw.at(0) == Moss.hello {
            guard let hello = Seal.verifyHello(raw) else { return }
            lastHelloSignal = (d.snr, d.rssi, d.hops)
            switch keyring.offer(hello) {
            case .trusted:
                notice = "Trusting Seed Vault \(hello.fingerprint) (\(hello.name)), first contact. Check it matches the fingerprint the Pi shows."
                heardHello = true
            case .known:
                heardHello = true
            case .mismatch:
                otherVault = hello.fingerprint
            }
            vault = keyring.vault
            return
        }
        active?.take(raw)
    }

    /// Ask "who's there?" and wait for the Seed Vault's hello: is it reachable,
    /// and how strong is its signal here?
    func lookForVault() async -> String {
        guard link.isReady else { return "Connect to your radio first." }
        heardHello = false
        lastHelloSignal = nil
        otherVault = nil
        await link.send(Moss.makeWhois(UInt16.random(in: 1...0xFFFF)))
        let until = uptime() + 15
        while uptime() < until && !heardHello && otherVault == nil {
            await snooze(0.25)
        }
        if heardHello, let v = vault {
            let how = lastHelloSignal.map { "\(signalText(snr: $0.snr, rssi: $0.rssi)), \(describeHops($0.hops))" } ?? ""
            return "\(v.name) answered: \(how)."
        }
        if let other = otherVault {
            return "A Seed Vault answered, but not the one you trust (\(other))."
        }
        return "No answer in 15 s. Is the Seed Vault running, and in range?"
    }

    func forgetVault() {
        keyring.forget()
        vault = nil
        otherVault = nil
    }

    /// The trusted vault's public key, asking for it over the air the first time.
    func findVault(_ report: @MainActor (TransferProgress) -> Void) async throws -> Data {
        if let x = keyring.xPub { return x }
        let started = uptime()
        for attempt in 1...(requestAttempts + 5) {
            var p = TransferProgress()
            p.message = "looking for the Seed Vault"
            p.attempt = attempt
            p.elapsed = uptime() - started
            report(p)
            heardHello = false
            await link.send(Moss.makeWhois(UInt16.random(in: 1...0xFFFF)))
            let until = uptime() + requestTimeout
            while uptime() < until && !heardHello {
                await snooze(0.25)
            }
            if let x = keyring.xPub { return x }
        }
        throw AppProblem("No answer from a Seed Vault. Is it running, and in range?")
    }

    // ------------------------------------------------------------ what the app asks for

    func search(_ query: String, report: @escaping @MainActor (TransferProgress) -> Void) async throws -> (wiki: [Moss.SearchResult], community: [String]) {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { throw AppProblem("Type something to search for.") }
        do {
            let got = try await fetch(kind: Moss.kindSearchAll, query: "0\u{1F}\(q)", report: report)
            guard let decoded = Moss.decodeSearchAll(got.content) else {
                throw AppProblem("The Seed Vault's answer didn't make sense.")
            }
            return decoded
        } catch let e as AppProblem where e.description.trimmingCharacters(in: .whitespaces) == "bad request" {
            // A vault from before combined search: Wikipedia only.
            let got = try await fetch(kind: Moss.kindSearch, query: q, report: report)
            return (Moss.decodeResults(got.content), [])
        }
    }

    func article(_ title: String, report: @escaping @MainActor (TransferProgress) -> Void) async throws -> (title: String, text: String, spores: Int, seconds: Double) {
        let t = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { throw AppProblem("No article title.") }
        let got = try await fetch(kind: Moss.kindArticle, query: t, report: report)
        return (got.name.isEmpty ? t : got.name, String(decoding: got.content, as: UTF8.self), got.spores, got.elapsed)
    }

    // ------------------------------------------------------------ one request

    func fetch(kind: UInt8, query: String, report: @escaping @MainActor (TransferProgress) -> Void) async throws -> Fetched {
        guard link.isReady else { throw AppProblem("Connect to your radio first (Radio tab).") }
        guard !busy else { throw AppProblem("Already fetching something; one at a time.") }
        busy = true
        defer {
            busy = false
            active = nil
        }
        otherVault = nil
        let vaultX = try await findVault(report)
        let rid = UInt16.random(in: 1...0xFFFF)
        let session = try ReaderSession(vaultX: vaultX, rid: rid, kind: kind, query: query)
        let ex = Exchange(rid: rid, seal: session)
        active = ex
        return try await run(ex, kind: kind, report: report)
    }

    private func run(_ ex: Exchange, kind: UInt8, report: @MainActor (TransferProgress) -> Void) async throws -> Fetched {
        let started = uptime()
        var attempts = 0
        var lastRequest = 0.0
        var lastStatusAt = started
        var nacks = 0
        var fruitless = 0
        var haveAtNack = 0
        var vaultETA: Double? = nil
        var lastReport = 0.0
        let request = ex.seal.request

        func snapshot(_ stage: String) -> TransferProgress {
            var p = TransferProgress()
            p.stage = stage
            p.elapsed = uptime() - started
            p.total = ex.capsule?.total ?? 0
            p.have = Set(ex.chunks.keys)
            p.attempt = attempts
            if let s = ex.status, s.state == Moss.stateQueued { p.queuePosition = s.value }
            if stage == "repairing" {
                p.eta = nil
            } else if let c = ex.capsule, let first = ex.firstChunkAt, ex.chunks.count >= 5 {
                let per = (uptime() - first) / Double(ex.chunks.count - 1)
                p.eta = max(0, Double(c.total - ex.chunks.count) * per)
            } else if let e = vaultETA {
                p.eta = max(0, e - (uptime() - lastStatusAt))
            }
            return p
        }

        while true {
            let now = uptime()
            if let e = ex.error {
                throw AppProblem(e)
            }
            if !link.isReady {
                throw AppProblem("Lost the radio in the middle of the transfer.")
            }

            // Say it (again) until the vault acknowledges.
            if ex.capsule == nil && ex.status == nil {
                if attempts == 0 || now - lastRequest >= requestTimeout {
                    if attempts >= requestAttempts {
                        if let other = otherVault {
                            throw AppProblem("Your Seed Vault didn't answer, but one with a different key did (\(other)). If the Pi was reset on purpose, forget the old vault in the Vault tab.")
                        }
                        throw AppProblem("No answer from the Seed Vault. Is it running, and in range?")
                    }
                    await link.send(request)
                    if attempts > 0 {
                        await link.send(Moss.makeWhois(ex.rid))       // is it there at all, with the key we know?
                    }
                    attempts += 1
                    lastRequest = uptime()
                }
            }

            // The vault answered but the page hasn't started.
            if ex.capsule == nil, let s = ex.status {
                if s.state == Moss.stateSending {
                    vaultETA = Double(s.value)
                    lastStatusAt = ex.statusAt
                }
                let flowing = ex.orphans > 0 && now - lastRequest >= manifestRetry
                let stalled = !ex.early.isEmpty && now - ex.lastHeard >= quiet && now - lastRequest >= quiet
                let silent = now - ex.lastHeard >= requestTimeout && now - lastRequest >= requestTimeout
                if flowing || stalled || silent {
                    await link.send(request)          // the vault answers a repeat by re-sending its capsule
                    lastRequest = uptime()
                    ex.orphans = 0
                }
                if now - ex.lastHeard > fetchTimeout {
                    throw AppProblem("The Seed Vault went quiet before the spores arrived.")
                }
            }

            // Page in flight.
            if let c = ex.capsule {
                if ex.chunks.count == c.total {
                    report(snapshot("verifying"))
                    return try finish(ex, c, started: started)
                }
                if now - ex.lastHeard > idle {
                    throw AppProblem("Nothing heard for \(Int(idle)) s; got \(ex.chunks.count) of \(c.total) spores.")
                }
                if now - ex.lastHeard >= quiet {
                    fruitless = ex.chunks.count > haveAtNack ? 0 : fruitless + 1
                    haveAtNack = ex.chunks.count
                    if fruitless > maxNacks {
                        throw AppProblem("Transfer incomplete: \(ex.chunks.count) of \(c.total) spores.")
                    }
                    await askForGaps(ex, c)
                    nacks += 1
                    ex.lastHeard = uptime()
                }
            }

            let stage: String
            if ex.capsule != nil {
                stage = nacks > 0 ? "repairing" : "receiving"
            } else if let s = ex.status {
                stage = s.state == Moss.stateQueued ? "queued" : (s.state == Moss.stateFetching ? "fetching" : "receiving")
            } else {
                stage = "requesting"
            }
            if now - lastReport >= 0.25 {
                report(snapshot(stage))
                lastReport = now
            }
            await snooze(0.25)
        }
    }

    private func askForGaps(_ ex: Exchange, _ c: ReaderSession.Capsule) async {
        let missing = (0..<c.total).filter { ex.chunks[$0] == nil }
        var batches: [[Int]] = []
        var i = 0
        while i < missing.count {
            batches.append(Array(missing[i..<min(i + Moss.nackRoom, missing.count)]))
            i += Moss.nackRoom
        }
        for (n, batch) in batches.enumerated() {
            await link.send(Moss.makeNack(ex.rid, missing: batch))
            if n < batches.count - 1 { await snooze(nackSpacing) }
        }
    }

    private func finish(_ ex: Exchange, _ c: ReaderSession.Capsule, started: Double) throws -> Fetched {
        var wire = Data()
        for i in 0..<c.total {
            if let piece = ex.chunks[i] { wire.append(piece) }
        }
        let opened: (name: String, blob: Data)
        do {
            opened = try ex.seal.openBody(wire, bodyLength: c.bodyLength)
        } catch {
            throw AppProblem("The page came through blighted (failed its integrity check); not keeping it.")
        }
        var content = opened.blob
        if c.flags & Moss.flagZlib != 0 {
            guard let inflated = Moss.inflate(opened.blob) else {
                throw AppProblem("The page came through blighted (corrupted in transit); not keeping it.")
            }
            content = inflated
        }
        guard Data(SHA256.hash(data: content)).slice(0, 8) == c.hash else {
            throw AppProblem("The page came through blighted (failed its hash check); not keeping it.")
        }
        // Tell the vault it can stop listening. Twice: if the only DONE is lost,
        // everyone queued behind us waits out the vault's whole timeout.
        let rid = ex.rid
        Task { @MainActor in
            await self.link.send(Moss.makeDone(rid))
            await snooze(self.nackSpacing)
            await self.link.send(Moss.makeDone(rid))
        }
        return Fetched(name: opened.name, content: content, elapsed: uptime() - started, spores: c.total)
    }
}
