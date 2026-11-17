import Combine
import CoreBluetooth
import Foundation

// The Bluetooth link to a MeshCore radio running the stock companion firmware.
// It connects, finds (or creates) the #moss channel, keeps asking the radio
// for queued messages, and hands every MOSS datagram heard on #moss to
// whoever is listening. Everything runs on the main thread: Core Bluetooth
// is told to call back on the main queue, so there are no races to manage.

struct AppProblem: Error, CustomStringConvertible, LocalizedError {
    var description: String
    init(_ d: String) { description = d }
    var errorDescription: String? { description }
}

struct FoundRadio: Identifiable {
    let id: UUID
    var name: String
    var rssi: Int
    let peripheral: CBPeripheral
}

/// One MOSS datagram heard on #moss, with how it arrived.
struct Datagram {
    let payload: Data
    let snr: Double
    let rssi: Int?
    let hops: UInt8
    let at: Double          // uptime()
}

/// A line in the "heard on #moss" feed.
struct HeardItem: Identifiable {
    let id = UUID()
    let at: Date
    let what: String
    let snr: Double?
    let rssi: Int?
    let hops: UInt8?
}

/// A tiny async lock: one radio command in flight at a time.
final class CommandGate {
    private var busy = false
    private var waiting: [CheckedContinuation<Void, Never>] = []

    func lock() async {
        if !busy {
            busy = true
            return
        }
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            waiting.append(c)
        }
    }

    func unlock() {
        if waiting.isEmpty {
            busy = false
        } else {
            waiting.removeFirst().resume()
        }
    }
}

@MainActor
final class RadioLink: NSObject, ObservableObject {
    enum Phase: Equatable {
        case bluetoothOff, idle, scanning, connecting, preparing, ready
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var problem: String? = nil
    @Published private(set) var radios: [FoundRadio] = []
    @Published private(set) var radioName = ""
    @Published private(set) var info: RadioInfo? = nil
    @Published private(set) var model = ""
    @Published private(set) var firmware = ""
    @Published private(set) var slot: Int? = nil
    @Published private(set) var heard: [HeardItem] = []
    @Published private(set) var notes: [String] = []
    @Published private(set) var refusedSends = 0

    let channelName = "#moss"
    private let lastRadioKey = "lastRadioID"

    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var rxCharacteristic: CBCharacteristic?
    private var txCharacteristic: CBCharacteristic?
    private var wantConnected = false

    private struct Pending {
        let id: Int
        let expect: Set<UInt8>
        let continuation: CheckedContinuation<Frame?, Never>
    }
    private var pending: Pending?
    private var nextCommandID = 0
    private let gate = CommandGate()
    private var pollTask: Task<Void, Never>?
    private var draining = false
    private var lastLog: (snr: Double, rssi: Int, at: Double)?
    private var listeners: [UUID: @MainActor (Datagram) -> Void] = [:]

    override init() {
        super.init()
        central = CBCentralManager(delegate: self, queue: nil)     // call back on the main queue
    }

    var isReady: Bool { phase == .ready }

    var lastRadioID: UUID? {
        guard let s = UserDefaults.standard.string(forKey: lastRadioKey) else { return nil }
        return UUID(uuidString: s)
    }

    // ------------------------------------------------------------ listening

    /// Every MOSS datagram heard on #moss goes to `handler` until `stopListening`.
    func listen(_ handler: @escaping @MainActor (Datagram) -> Void) -> UUID {
        let id = UUID()
        listeners[id] = handler
        return id
    }

    func stopListening(_ id: UUID) {
        listeners[id] = nil
    }

    // ------------------------------------------------------------ finding and connecting

    func scan() {
        guard central.state == .poweredOn else {
            problem = "Bluetooth is off, or MOSS Radio isn't allowed to use it."
            return
        }
        radios = []
        problem = nil
        if phase == .idle { phase = .scanning }
        central.scanForPeripherals(withServices: nil, options: nil)
        Task { @MainActor in
            await snooze(15)
            if self.phase == .scanning { self.stopScan() }
        }
    }

    func stopScan() {
        central.stopScan()
        if phase == .scanning { phase = .idle }
    }

    func connect(_ radio: FoundRadio) {
        stopScan()
        UserDefaults.standard.set(radio.id.uuidString, forKey: lastRadioKey)
        open(radio.peripheral)
    }

    func disconnect() {
        wantConnected = false
        if let p = peripheral {
            central.cancelPeripheralConnection(p)
        }
        teardown()
        phase = central.state == .poweredOn ? .idle : .bluetoothOff
    }

    /// Stop using this radio, and don't reconnect to it automatically.
    func forgetRadio() {
        disconnect()
        peripheral = nil
        radioName = ""
        info = nil
        UserDefaults.standard.removeObject(forKey: lastRadioKey)
    }

    func reconnectLast() {
        guard peripheral == nil || phase == .idle, let id = lastRadioID else { return }
        guard let p = central.retrievePeripherals(withIdentifiers: [id]).first else { return }
        open(p)
    }

    private func open(_ p: CBPeripheral) {
        peripheral = p
        p.delegate = self
        wantConnected = true
        problem = nil
        radioName = p.name ?? "MeshCore radio"
        phase = .connecting
        central.connect(p, options: nil)    // waits for as long as it takes the radio to appear
    }

    private func teardown() {
        pollTask?.cancel()
        pollTask = nil
        rxCharacteristic = nil
        txCharacteristic = nil
        slot = nil
        if let p = pending {
            pending = nil
            p.continuation.resume(returning: nil)
        }
    }

    private func note(_ text: String) {
        notes.append(text)
        if notes.count > 40 { notes.removeFirst(notes.count - 40) }
    }

    private func fail(_ text: String) {
        problem = text
        note(text)
        if let p = peripheral {
            central.cancelPeripheralConnection(p)
        }
        wantConnected = false
        teardown()
        phase = .idle
    }

    // ------------------------------------------------------------ after connecting

    private func prepare() async {
        phase = .preparing
        var described: RadioInfo? = nil
        // The radio's first answer can be slow over Bluetooth, especially just after pairing.
        for wait in [5.0, 8.0, 10.0] {
            if case .selfInfo(let i)? = await command(MC.appStart(), expect: [MC.selfInfo, MC.error], timeout: wait) {
                described = i
                break
            }
            if peripheral?.state != .connected { return }
        }
        guard let i = described else {
            fail("The radio connected but didn't answer. Is it running the MeshCore companion firmware (not repeater or room server)?")
            return
        }
        info = i
        if !i.name.isEmpty { radioName = i.name }
        var maxChannels = 8
        if case .deviceInfo(let m, let mdl, let ver)? = await command(MC.deviceQuery(), expect: [MC.deviceInfo, MC.error], timeout: 5) {
            if m > 0 { maxChannels = m }
            model = mdl
            firmware = ver
        }
        do {
            slot = try await findOrCreateChannel(maxChannels)
        } catch {
            fail("\(error)")
            return
        }
        phase = .ready
        note("connected to \(radioName); \(channelName) is in slot \(slot ?? -1)")
        startPolling()
    }

    /// The #moss hashtag channel: used if it's there, created in a free slot if
    /// not. Never slot 0 (Public) and never a slot in use.
    private func findOrCreateChannel(_ maxChannels: Int) async throws -> Int {
        let secret = MC.hashtagSecret(channelName)
        var free: Int? = nil
        for idx in 0..<maxChannels {
            guard case .channelInfo(let s)? = await command(MC.getChannel(idx), expect: [MC.channelInfo, MC.error], timeout: 4) else {
                continue
            }
            if s.name == channelName && s.secret == secret {
                return idx
            }
            if s.name == channelName {
                note("slot \(idx) is a private channel that's also called \(channelName); leaving it alone")
            }
            let blank = !s.secret.contains(where: { $0 != 0 })
            if free == nil && idx != 0 && s.name.isEmpty && blank {
                free = idx
            }
        }
        guard let f = free else {
            throw AppProblem("All \(maxChannels) channel slots on this radio are in use. Remove one in the MeshCore app (or add \(channelName) there) and connect again.")
        }
        let ack = await command(MC.setChannel(f, name: channelName, secret: secret), expect: [MC.ok, MC.error], timeout: 5)
        guard case .ok? = ack else {
            throw AppProblem("The radio refused to create \(channelName) in slot \(f).")
        }
        guard case .channelInfo(let check)? = await command(MC.getChannel(f), expect: [MC.channelInfo, MC.error], timeout: 4),
              check.name == channelName, check.secret == secret else {
            throw AppProblem("Created \(channelName) in slot \(f), but reading it back didn't match.")
        }
        note("created the \(channelName) channel in slot \(f)")
        return f
    }

    /// The radio queues what it hears; nothing arrives unless we ask for it.
    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                await snooze(1.5)
                guard let self = self else { return }
                if self.phase == .ready {
                    await self.drain()
                }
            }
        }
    }

    private func drain() async {
        if draining { return }
        draining = true
        defer { draining = false }
        for _ in 0..<64 {
            guard phase == .ready else { return }
            guard let f = await command(MC.syncNextMessage(), expect: MC.syncReplies, timeout: 4) else { return }
            if f.code == MC.noMoreMessages || f.code == MC.error { return }
        }
    }

    // ------------------------------------------------------------ commands

    /// Write one command and wait for the first reply of an expected kind.
    /// nil: no reply in time, or the link went away.
    func command(_ data: Data, expect: Set<UInt8>, timeout: Double = 5) async -> Frame? {
        await gate.lock()
        defer { gate.unlock() }
        guard let p = peripheral, let rx = rxCharacteristic, p.state == .connected else { return nil }
        nextCommandID += 1
        let id = nextCommandID
        let frame: Frame? = await withCheckedContinuation { (c: CheckedContinuation<Frame?, Never>) in
            self.pending = Pending(id: id, expect: expect, continuation: c)
            p.writeValue(data, for: rx, type: .withResponse)
            Task { @MainActor in
                await snooze(timeout)
                self.finishPending(id: id, with: nil)
            }
        }
        return frame
    }

    private func finishPending(id: Int, with frame: Frame?) {
        guard let p = pending, p.id == id else { return }
        pending = nil
        p.continuation.resume(returning: frame)
    }

    /// Send one MOSS datagram on #moss. zeroHop: only radios in direct range
    /// hear it (no repeaters). True if the radio took it.
    @discardableResult
    func send(_ datagram: Data, zeroHop: Bool = false) async -> Bool {
        for attempt in 1...3 {
            guard phase == .ready, let s = slot else { return false }
            let reply = await command(MC.sendChannelData(channel: s, payload: datagram, zeroHop: zeroHop),
                                      expect: [MC.ok, MC.error], timeout: 5)
            if case .ok? = reply { return true }
            await snooze(0.4 * Double(attempt))
        }
        refusedSends += 1
        note("the radio refused to send a message 3 times; it counts as lost")
        return false
    }

    /// An ordinary chat message on #moss (what the laptop's radio check listens for).
    @discardableResult
    func sendText(_ text: String) async -> Bool {
        guard phase == .ready, let s = slot else { return false }
        let reply = await command(MC.sendChannelText(channel: s, text: text), expect: [MC.ok, MC.error], timeout: 5)
        if case .ok? = reply { return true }
        return false
    }

    // ------------------------------------------------------------ incoming

    private func received(_ value: Data?) {
        guard let data = value, let frame = Frame.parse(data) else { return }
        switch frame {
        case .messagesWaiting:
            Task { @MainActor in await self.drain() }
        case .log(let snr, let rssi):
            lastLog = (snr, rssi, uptime())
        case .channelData(let channel, let snr, let hops, let dataType, let payload):
            if channel == slot && dataType == MC.mossDataType && !payload.isEmpty {
                deliver(Datagram(payload: payload, snr: snr, rssi: rssiFor(snr), hops: hops, at: uptime()))
            }
        case .channelText(let channel, let snr, let hops, let text):
            if channel == slot {
                addHeard("chat: \(text)", snr: snr, rssi: snr.flatMap { rssiFor($0) }, hops: hops)
            }
        default:
            break
        }
        if let p = pending, p.expect.contains(frame.code) {
            finishPending(id: p.id, with: frame)
        }
    }

    /// RSSI only comes in the radio's packet log, just before the message itself:
    /// take it when the log entry is recent and has the same SNR.
    private func rssiFor(_ snr: Double) -> Int? {
        guard let l = lastLog, uptime() - l.at < 5, abs(l.snr - snr) < 0.01 else { return nil }
        return l.rssi
    }

    private func deliver(_ d: Datagram) {
        addHeard(describe(d.payload), snr: d.snr, rssi: d.rssi, hops: d.hops)
        for handler in Array(listeners.values) {
            handler(d)
        }
    }

    private func describe(_ p: Data) -> String {
        switch p.at(0) {
        case Moss.hello:
            return "Seed Vault hello" + (Seal.verifyHello(p).map { ": \($0.name)" } ?? "")
        case Moss.ping: return "ping (someone's range test)"
        case Moss.pong: return "pong from " + (Moss.parsePong(p)?.name ?? "a Seed Vault")
        case Moss.chunk: return "spore"
        case Moss.sealedCapsule, Moss.manifest: return "capsule (a page starting)"
        case Moss.sealedRequest, Moss.request: return "request"
        case Moss.nack: return "repair request"
        case Moss.status: return "Seed Vault status"
        default: return "MOSS message (type \(p.at(0)))"
        }
    }

    private func addHeard(_ what: String, snr: Double?, rssi: Int?, hops: UInt8?) {
        heard.insert(HeardItem(at: Date(), what: what, snr: snr, rssi: rssi, hops: hops), at: 0)
        if heard.count > 80 { heard.removeLast(heard.count - 80) }
    }

    // ------------------------------------------------------------ Core Bluetooth events (main queue)

    fileprivate func centralChanged(_ state: CBManagerState) {
        switch state {
        case .poweredOn:
            if phase == .bluetoothOff { phase = .idle }
            problem = nil
            reconnectLast()
        case .poweredOff:
            teardown()
            phase = .bluetoothOff
            problem = "Bluetooth is off."
        case .unauthorized:
            phase = .bluetoothOff
            problem = "MOSS Radio isn't allowed to use Bluetooth. Turn it on in Settings → MOSS Radio."
        default:
            break
        }
    }

    fileprivate func discovered(_ p: CBPeripheral, name: String, services: [CBUUID], rssi: Int) {
        guard name.hasPrefix("MeshCore") || services.contains(MC.service) else { return }
        let shown = name.isEmpty ? "MeshCore radio" : name
        if let i = radios.firstIndex(where: { $0.id == p.identifier }) {
            radios[i].rssi = rssi
            radios[i].name = shown
        } else {
            radios.append(FoundRadio(id: p.identifier, name: shown, rssi: rssi, peripheral: p))
        }
        radios.sort { $0.rssi > $1.rssi }
    }

    fileprivate func connected(_ p: CBPeripheral) {
        guard p.identifier == peripheral?.identifier else { return }
        note("Bluetooth connected; looking for the radio's serial service")
        p.discoverServices([MC.service])
    }

    fileprivate func failedToConnect(_ p: CBPeripheral, _ error: Error?) {
        fail("Couldn't connect to \(radioName): \(error?.localizedDescription ?? "unknown error"). Is the MeshCore phone app disconnected from it? A radio takes one Bluetooth connection at a time.")
    }

    fileprivate func disconnected(_ p: CBPeripheral, _ error: Error?) {
        guard p.identifier == peripheral?.identifier else { return }
        teardown()
        if wantConnected {
            phase = .connecting
            if let e = error {
                note("connection lost (\(e.localizedDescription)); reconnecting when the radio is back")
            }
            central.connect(p, options: nil)
        } else {
            phase = central.state == .poweredOn ? .idle : .bluetoothOff
        }
    }

    fileprivate func foundServices(_ p: CBPeripheral, _ error: Error?) {
        guard let service = p.services?.first(where: { $0.uuid == MC.service }) else {
            fail("\(radioName) doesn't offer MeshCore's Bluetooth serial service. Is it running the companion firmware with Bluetooth?")
            return
        }
        p.discoverCharacteristics([MC.rxCharacteristic, MC.txCharacteristic], for: service)
    }

    fileprivate func foundCharacteristics(_ p: CBPeripheral, _ service: CBService, _ error: Error?) {
        for c in service.characteristics ?? [] {
            if c.uuid == MC.rxCharacteristic { rxCharacteristic = c }
            if c.uuid == MC.txCharacteristic { txCharacteristic = c }
        }
        guard let tx = txCharacteristic, rxCharacteristic != nil else {
            fail("\(radioName)'s serial service is missing a part MOSS needs.")
            return
        }
        // Subscribing is what makes iOS ask for the radio's PIN the first time.
        p.setNotifyValue(true, for: tx)
    }

    fileprivate func notifyChanged(_ p: CBPeripheral, _ c: CBCharacteristic, _ error: Error?) {
        if let e = error {
            fail("Pairing with \(radioName) didn't work (\(e.localizedDescription)). Enter the PIN from the MeshCore app (or the radio's screen) when iOS asks. If iOS never asks: Settings → Bluetooth → \(radioName) → ⓘ → Forget This Device, then connect again.")
            return
        }
        if c.isNotifying && phase == .connecting {
            Task { @MainActor in await self.prepare() }
        }
    }

    fileprivate func incoming(_ value: Data?) {
        received(value)
    }
}

extension RadioLink: CBCentralManagerDelegate {
    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let state = central.state
        MainActor.assumeIsolated { self.centralChanged(state) }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                                    advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let name = (advertisementData[CBAdvertisementDataLocalNameKey] as? String) ?? peripheral.name ?? ""
        let services = (advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID]) ?? []
        let rssi = RSSI.intValue
        MainActor.assumeIsolated { self.discovered(peripheral, name: name, services: services, rssi: rssi) }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        MainActor.assumeIsolated { self.connected(peripheral) }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        MainActor.assumeIsolated { self.failedToConnect(peripheral, error) }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        MainActor.assumeIsolated { self.disconnected(peripheral, error) }
    }
}

extension RadioLink: CBPeripheralDelegate {
    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        MainActor.assumeIsolated { self.foundServices(peripheral, error) }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        MainActor.assumeIsolated { self.foundCharacteristics(peripheral, service, error) }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        MainActor.assumeIsolated { self.notifyChanged(peripheral, characteristic, error) }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        let value = characteristic.value
        MainActor.assumeIsolated { self.incoming(value) }
    }
}
