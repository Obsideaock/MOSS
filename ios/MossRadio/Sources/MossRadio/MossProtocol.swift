import Foundation

// MOSS's datagrams (moss/protocol.py in the Python project). Every datagram
// fits one 163-byte MeshCore channel message. Requests and replies are
// encrypted (Seal.swift); spores, repairs and pings are framed here.

enum Moss {
    static let maxDatagram = 163
    static let chunkHeader = 5                       // type, transfer id 2, index 2
    static let chunkBytes = maxDatagram - chunkHeader // 158 bytes of page per spore
    static let nackRoom = (maxDatagram - 4) / 2      // spore numbers per repair request

    // Datagram types.
    static let manifest: UInt8 = 1
    static let chunk: UInt8 = 2
    static let nack: UInt8 = 3
    static let done: UInt8 = 4
    static let request: UInt8 = 5
    static let status: UInt8 = 6
    static let error: UInt8 = 7
    static let hello: UInt8 = 8
    static let whois: UInt8 = 9
    static let sealedRequest: UInt8 = 10
    static let sealedCapsule: UInt8 = 11
    static let sealedError: UInt8 = 12
    static let ping: UInt8 = 17
    static let pong: UInt8 = 18

    static let flagZlib: UInt8 = 1
    static let pingZeroHop: UInt8 = 1
    static let signalUnknown: Int8 = 127

    // Request kinds.
    static let kindSearch: UInt8 = 1
    static let kindArticle: UInt8 = 2
    static let kindSpecs: UInt8 = 5
    static let kindSearchAll: UInt8 = 6

    // Vault status pulses.
    static let stateQueued: UInt8 = 1
    static let stateFetching: UInt8 = 2
    static let stateSending: UInt8 = 3

    /// The transfer/request id every MOSS datagram carries in bytes 1-2.
    static func peekID(_ raw: Data) -> UInt16? {
        return raw.count >= 3 ? raw.u16(1) : nil
    }

    static func makeWhois(_ rid: UInt16) -> Data {
        var d = Data([Moss.whois])
        d.appendU16(rid)
        return d
    }

    static func makeNack(_ tid: UInt16, missing: [Int]) -> Data {
        let chosen = Array(missing.prefix(nackRoom))
        var d = Data([Moss.nack])
        d.appendU16(tid)
        d.append(UInt8(chosen.count))
        for i in chosen {
            d.appendU16(UInt16(i))
        }
        return d
    }

    static func makeDone(_ tid: UInt16) -> Data {
        var d = Data([Moss.done])
        d.appendU16(tid)
        return d
    }

    /// (index, payload) of a spore.
    static func parseChunk(_ raw: Data) -> (index: Int, payload: Data)? {
        guard raw.count >= chunkHeader else { return nil }
        return (Int(raw.u16(3)), raw.slice(chunkHeader))
    }

    /// (state, value) of a status pulse: queue position, or seconds left.
    static func parseStatus(_ raw: Data) -> (state: UInt8, value: Int)? {
        guard raw.count >= 6 else { return nil }
        return (raw.at(3), Int(raw.u16(4)))
    }

    // ------------------------------------------------------------ range test

    static func makePing(runID: UInt16, seq: UInt16, zeroHop: Bool) -> Data {
        var d = Data([Moss.ping])
        d.appendU16(runID)
        d.appendU16(seq)
        d.append(zeroHop ? pingZeroHop : 0)
        return d
    }

    struct Pong {
        var runID: UInt16
        var seq: UInt16
        var snr: Double?        // how the vault heard our ping
        var rssi: Int?
        var hops: UInt8         // the path the ping arrived with (0xFF: direct route)
        var flags: UInt8
        var name: String
    }

    static func parsePong(_ raw: Data) -> Pong? {
        guard raw.count >= 9, raw.at(0) == Moss.pong else { return nil }
        let snr = raw.i8(5)
        let rssi = raw.i8(6)
        return Pong(runID: raw.u16(1), seq: raw.u16(3),
                    snr: snr == signalUnknown ? nil : Double(snr) / 4.0,
                    rssi: rssi == signalUnknown ? nil : Int(rssi),
                    hops: raw.at(7), flags: raw.at(8),
                    name: String(decoding: raw.slice(9), as: UTF8.self))
    }

    // ------------------------------------------------------------ page bodies

    /// zlib.decompress: a 2-byte header, raw deflate, a 4-byte checksum.
    /// (The page's own SHA-256 check comes afterwards, so a bad checksum is caught there.)
    static func inflate(_ data: Data) -> Data? {
        guard data.count > 6 else { return nil }
        let raw = data.slice(2, data.count - 4)
        do {
            let out = try (raw as NSData).decompressed(using: .zlib)
            return out as Data
        } catch {
            return nil
        }
    }

    struct SearchResult: Identifiable, Hashable {
        var title: String
        var blurb: String
        var id: String { title }
    }

    /// A combined search answer: {"wiki": [[title, blurb], ...], "community": [...], "more": bool}
    static func decodeSearchAll(_ data: Data) -> (wiki: [SearchResult], community: [String])? {
        guard let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any] else { return nil }
        var wiki: [SearchResult] = []
        for item in (dict["wiki"] as? [Any]) ?? [] {
            if let pair = item as? [Any], let t = pair.first as? String {
                wiki.append(SearchResult(title: t, blurb: (pair.count > 1 ? pair[1] as? String : nil) ?? ""))
            }
        }
        var community: [String] = []
        for item in (dict["community"] as? [Any]) ?? [] {
            if let page = item as? [String: Any], let t = page["t"] as? String {
                community.append(t)
            }
        }
        return (wiki, community)
    }

    /// An older vault's search answer: one "title<TAB>blurb" per line.
    static func decodeResults(_ data: Data) -> [SearchResult] {
        var out: [SearchResult] = []
        for line in String(decoding: data, as: UTF8.self).split(separator: "\n") {
            let parts = line.split(separator: "\t", maxSplits: 1, omittingEmptySubsequences: false)
            let title = parts.first.map { String($0).trimmingCharacters(in: .whitespaces) } ?? ""
            if title.isEmpty { continue }
            let blurb = parts.count > 1 ? String(parts[1]).trimmingCharacters(in: .whitespaces) : ""
            out.append(SearchResult(title: title, blurb: blurb))
        }
        return out
    }
}
