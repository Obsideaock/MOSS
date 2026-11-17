import Foundation

// Little helpers for reading and writing the byte layouts MeshCore and MOSS use.
// Everything is little-endian. Data slices keep their parent's indices, so
// every read goes through `at(_:)` (counted from the start) and every cut
// through `slice(_:_:)`, which returns a fresh zero-based copy.

extension Data {
    init(hex: String) {
        var out = Data()
        out.reserveCapacity(hex.count / 2)
        var high: UInt8? = nil
        for scalar in hex.unicodeScalars {
            guard let nibble = UInt8(String(scalar), radix: 16) else { continue }
            if let h = high {
                out.append(h << 4 | nibble)
                high = nil
            } else {
                high = nibble
            }
        }
        self = out
    }

    var hex: String {
        var s = ""
        s.reserveCapacity(count * 2)
        for b in self {
            s += String(format: "%02x", b)
        }
        return s
    }

    /// The byte `i` places from the start (works on slices too).
    func at(_ i: Int) -> UInt8 {
        return self[self.startIndex + i]
    }

    func u16(_ i: Int) -> UInt16 {
        return UInt16(at(i)) | (UInt16(at(i + 1)) << 8)
    }

    func u32(_ i: Int) -> UInt32 {
        var v: UInt32 = 0
        for k in 0..<4 {
            v |= UInt32(at(i + k)) << (8 * UInt32(k))
        }
        return v
    }

    func i8(_ i: Int) -> Int8 {
        return Int8(bitPattern: at(i))
    }

    /// A fresh copy of bytes [from, to) counted from the start; `to` nil = to the end.
    func slice(_ from: Int, _ to: Int? = nil) -> Data {
        let end = Swift.min(to ?? count, count)
        if from >= end { return Data() }
        return Data(self[(self.startIndex + from)..<(self.startIndex + end)])
    }

    mutating func appendU16(_ v: UInt16) {
        append(UInt8(v & 0xFF))
        append(UInt8(v >> 8))
    }

    mutating func appendU32(_ v: UInt32) {
        for k in 0..<4 {
            append(UInt8((v >> (8 * UInt32(k))) & 0xFF))
        }
    }

    static func random(_ n: Int) -> Data {
        var out = Data(count: n)
        for i in 0..<n {
            out[i] = UInt8.random(in: 0...255)
        }
        return out
    }
}

extension String {
    /// This string cut to at most `limit` bytes of UTF-8, never splitting a character.
    func utf8Prefix(_ limit: Int) -> String {
        if utf8.count <= limit { return self }
        var out = ""
        var used = 0
        for ch in self {
            let size = String(ch).utf8.count
            if used + size > limit { break }
            out.append(ch)
            used += size
        }
        return out
    }
}

/// Seconds that only ever go forward (for timeouts), like Python's time.monotonic().
func uptime() -> Double {
    return ProcessInfo.processInfo.systemUptime
}

/// Sleep without throwing (a cancelled sleep just returns early).
func snooze(_ seconds: Double) async {
    if seconds <= 0 { return }
    try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
}

/// Where the app keeps its files: they survive reinstalls (the bundle ID stays the same).
enum Files {
    static var documents: URL {
        return FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    static func url(_ name: String) -> URL {
        return documents.appendingPathComponent(name)
    }

    static func save<T: Encodable>(_ value: T, to name: String) {
        do {
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .iso8601
            let data = try encoder.encode(value)
            try data.write(to: url(name), options: .atomic)
        } catch {
            print("couldn't save \(name): \(error)")
        }
    }

    static func load<T: Decodable>(_ type: T.Type, from name: String) -> T? {
        guard let data = try? Data(contentsOf: url(name)) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(type, from: data)
    }
}
