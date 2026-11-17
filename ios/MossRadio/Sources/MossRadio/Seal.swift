import CryptoKit
import Foundation

// Private reading (moss/seal.py in the Python project), with Apple's CryptoKit:
// X25519, HKDF-SHA256 and ChaCha20-Poly1305, the HPKE pattern (RFC 9180).
//
//   SREQUEST  10 | rid 2 | our one-time public key 32 | sealed(kind, len, query, padding) | tag 16
//   SCAPSULE  11 | rid 2 | vault one-time public key 32 | sealed(total, wire len, body len, flags, hash) | tag 16
//   SERROR    12 | rid 2 | sealed(message) | tag 16
//   spores    ordinary CHUNKs carrying the encrypted body; its tag rides at the end
//
// Every sealed request is exactly 163 bytes, so its length says nothing.

struct SealError: Error, CustomStringConvertible {
    var description: String
    init(_ d: String) { description = d }
}

enum Seal {
    static let suite = Data("MOSS-seal-v1 X25519 HKDF-SHA256 ChaCha20-Poly1305".utf8)
    static let tagSize = 16
    static let requestHead = 1 + 2 + 32
    static let requestRoom = Moss.maxDatagram - requestHead - tagSize   // 112
    static let maxQuery = requestRoom - 2                                // 110 bytes of search or title

    static func nonce(_ n: UInt8) -> Data {
        var d = Data(count: 11)
        d.append(n)
        return d
    }
    static let nonceRequest = nonce(0)
    static let nonceError = nonce(1)
    static let nonceCapsule = nonce(2)
    static let nonceBody = nonce(3)

    static func hkdf(_ secret: Data, salt: Data, label: String) -> SymmetricKey {
        var info = suite
        info.append(Data(" \(label)".utf8))
        return HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: secret), salt: salt,
                                      info: info, outputByteCount: 32)
    }

    static func seal(_ key: SymmetricKey, nonce: Data, aad: Data, _ plain: Data) throws -> Data {
        let box = try ChaChaPoly.seal(plain, using: key, nonce: ChaChaPoly.Nonce(data: nonce),
                                      authenticating: aad)
        var out = Data(box.ciphertext)
        out.append(Data(box.tag))
        return out
    }

    static func open(_ key: SymmetricKey, nonce: Data, aad: Data, _ sealed: Data) throws -> Data {
        guard sealed.count >= tagSize else { throw SealError("too short") }
        let cipher = sealed.slice(0, sealed.count - tagSize)
        let tag = sealed.slice(sealed.count - tagSize)
        do {
            let box = try ChaChaPoly.SealedBox(nonce: ChaChaPoly.Nonce(data: nonce), ciphertext: cipher, tag: tag)
            return try ChaChaPoly.open(box, using: key, authenticating: aad)
        } catch {
            throw SealError("failed authentication")
        }
    }

    static func dh(_ priv: Curve25519.KeyAgreement.PrivateKey, _ pubRaw: Data) throws -> Data {
        guard pubRaw.count == 32 else { throw SealError("bad public key") }
        do {
            let pub = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: pubRaw)
            let shared = try priv.sharedSecretFromKeyAgreement(with: pub)
            return shared.withUnsafeBytes { Data($0) }
        } catch {
            throw SealError("bad key exchange")
        }
    }

    /// Short enough to read aloud or print on a sticker: 3F9A-12C4-77E0-5B21.
    static func fingerprint(x: Data, ed: Data) -> String {
        var input = Data("MOSS seed vault".utf8)
        input.append(x)
        input.append(ed)
        let hex = Data(SHA256.hash(data: input)).hex.prefix(16).uppercased()
        var groups: [String] = []
        var i = hex.startIndex
        while i < hex.endIndex {
            let j = hex.index(i, offsetBy: 4)
            groups.append(String(hex[i..<j]))
            i = j
        }
        return groups.joined(separator: "-")
    }

    struct Hello {
        var x: Data
        var ed: Data
        var name: String
        var version: String?
        var protocolLevel: Int?
        var fingerprint: String { Seal.fingerprint(x: x, ed: ed) }
    }

    /// HELLO 8 | x25519 public 32 | ed25519 public 32 | signature 64 | name [0 "moss/version/level"]
    /// nil unless the signature shows the two keys belong together. (Whether it's
    /// *your* vault is the fingerprint's job.)
    static func verifyHello(_ raw: Data) -> Hello? {
        guard raw.count >= 129, raw.at(0) == Moss.hello else { return nil }
        let x = raw.slice(1, 33), ed = raw.slice(33, 65), sig = raw.slice(65, 129)
        guard let key = try? Curve25519.Signing.PublicKey(rawRepresentation: ed) else { return nil }
        var signed = Data("MOSS hello".utf8)
        signed.append(x)
        signed.append(ed)
        guard key.isValidSignature(sig, for: signed) else { return nil }
        let tail = raw.slice(129)
        let parts = tail.split(separator: 0, maxSplits: 1, omittingEmptySubsequences: false)
        let name = parts.first.map { String(decoding: $0, as: UTF8.self) } ?? ""
        var hello = Hello(x: x, ed: ed, name: name, version: nil, protocolLevel: nil)
        if parts.count > 1 {
            let tag = String(decoding: parts[1], as: UTF8.self)     // "moss/0.6.0/3"
            let bits = tag.split(separator: "/")
            if bits.count == 3 && bits[0] == "moss" {
                hello.version = String(bits[1])
                hello.protocolLevel = Int(bits[2])
            }
        }
        return hello
    }
}

/// Our side of one sealed request.
final class ReaderSession {
    let rid: UInt16
    let vaultX: Data
    let ephemeralPublic: Data
    let request: Data
    private let staticSecret: Data
    private let requestKey: SymmetricKey
    private var ephemeral: Curve25519.KeyAgreement.PrivateKey?
    private(set) var replyKey: SymmetricKey?

    /// ephemeral and padding are only fixed by the self-check (to compare with
    /// the Python vectors); normally both are fresh random bytes.
    init(vaultX: Data, rid: UInt16, kind: UInt8, query: String,
         ephemeral fixedKey: Curve25519.KeyAgreement.PrivateKey? = nil,
         paddingByte: UInt8? = nil) throws {
        // Everything is worked out in local constants first, then stored.
        let eph = fixedKey ?? Curve25519.KeyAgreement.PrivateKey()
        let ephPub = eph.publicKey.rawRepresentation
        let secret = try Seal.dh(eph, vaultX)
        var head = Data([Moss.sealedRequest])
        head.appendU16(rid)
        head.append(ephPub)
        var salt = ephPub
        salt.append(vaultX)
        let key = Seal.hkdf(secret, salt: salt, label: "request")
        let q = Data(query.trimmingCharacters(in: .whitespacesAndNewlines).utf8Prefix(Seal.maxQuery).utf8)
        var plain = Data([kind, UInt8(q.count)])
        plain.append(q)
        let room = Seal.requestRoom - plain.count
        if let b = paddingByte {
            plain.append(Data(repeating: b, count: room))
        } else {
            plain.append(Data.random(room))             // every request is the same size
        }
        let sealedPart = try Seal.seal(key, nonce: Seal.nonceRequest, aad: head, plain)
        var req = head
        req.append(sealedPart)

        self.rid = rid
        self.vaultX = vaultX
        self.ephemeral = eph
        self.ephemeralPublic = ephPub
        self.staticSecret = secret
        self.requestKey = key
        self.request = req
        self.replyKey = nil
    }

    func openError(_ raw: Data) throws -> String {
        guard raw.count >= 3 + Seal.tagSize else { throw SealError("too short") }
        let plain = try Seal.open(requestKey, nonce: Seal.nonceError, aad: raw.slice(0, 3), raw.slice(3))
        return String(decoding: plain, as: UTF8.self)
    }

    struct Capsule {
        var total: Int          // spores
        var wireLength: Int
        var bodyLength: Int
        var flags: UInt8
        var hash: Data          // first 8 bytes of SHA-256 of the page
    }

    /// The vault's answer: how many spores, how long, and the key they're under.
    func openCapsule(_ raw: Data) throws -> Capsule {
        guard raw.count >= 35 + 19 + Seal.tagSize, raw.at(0) == Moss.sealedCapsule else {
            throw SealError("not a capsule")
        }
        guard let eph = ephemeral else { throw SealError("already opened") }
        let head = raw.slice(0, 35)
        let vaultEph = raw.slice(3, 35)
        let second = try Seal.dh(eph, vaultEph)
        var secret = staticSecret
        secret.append(second)
        var salt = ephemeralPublic
        salt.append(vaultEph)
        salt.append(vaultX)
        let key = Seal.hkdf(secret, salt: salt, label: "reply")
        let info = try Seal.open(key, nonce: Seal.nonceCapsule, aad: head, raw.slice(35))
        guard info.count >= 19 else { throw SealError("short capsule") }
        replyKey = key
        ephemeral = nil                                 // done with the one-time key
        return Capsule(total: Int(info.u16(0)), wireLength: Int(info.u32(2)),
                       bodyLength: Int(info.u32(6)), flags: info.at(10), hash: info.slice(11, 19))
    }

    /// The whole page, once every spore is in: (name, page bytes).
    func openBody(_ wire: Data, bodyLength: Int) throws -> (name: String, blob: Data) {
        guard let key = replyKey else { throw SealError("no capsule yet") }
        let body = try Seal.open(key, nonce: Seal.nonceBody, aad: Data(), wire).slice(0, bodyLength)
        guard body.count >= 2 else { throw SealError("short body") }
        let n = Int(body.u16(0))
        return (String(decoding: body.slice(2, 2 + n), as: UTF8.self), body.slice(2 + n))
    }
}

/// Which Seed Vault this phone trusts. The first one heard is trusted and
/// remembered (like SSH); one that later shows a different key is refused.
struct TrustedVault: Codable {
    var x: String               // hex
    var ed: String
    var fingerprint: String
    var name: String
    var since: Date
    var version: String?
    var protocolLevel: Int?     // 4 and up answer range-test pings
}

final class Keyring {
    static let file = "vault.json"
    private(set) var vault: TrustedVault?

    init() {
        vault = Files.load(TrustedVault.self, from: Keyring.file)
    }

    var xPub: Data? { vault.map { Data(hex: $0.x) } }

    enum Offer { case trusted, known, mismatch }

    func offer(_ hello: Seal.Hello) -> Offer {
        if let v = vault {
            if v.fingerprint == hello.fingerprint {
                if hello.version != nil && (hello.version != v.version || hello.protocolLevel != v.protocolLevel) {
                    vault?.version = hello.version
                    vault?.protocolLevel = hello.protocolLevel
                    Files.save(vault, to: Keyring.file)
                }
                return .known
            }
            return .mismatch
        }
        vault = TrustedVault(x: hello.x.hex, ed: hello.ed.hex, fingerprint: hello.fingerprint,
                             name: hello.name, since: Date(), version: hello.version,
                             protocolLevel: hello.protocolLevel)
        Files.save(vault, to: Keyring.file)
        return .trusted
    }

    func forget() {
        vault = nil
        try? FileManager.default.removeItem(at: Files.url(Keyring.file))
    }
}
