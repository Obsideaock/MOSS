import CoreBluetooth
import CryptoKit
import Foundation

// The MeshCore companion protocol, as spoken over Bluetooth: each Bluetooth
// write is one command, each notification one reply or push. Ported from the
// meshcore Python library the laptop side of MOSS uses (stock firmware only).

enum MC {
    // The Nordic UART service MeshCore radios expose.
    static let service = CBUUID(string: "6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
    static let rxCharacteristic = CBUUID(string: "6E400002-B5A3-F393-E0A9-E50E24DCCA9E")   // we write
    static let txCharacteristic = CBUUID(string: "6E400003-B5A3-F393-E0A9-E50E24DCCA9E")   // it notifies

    static let mossDataType: UInt16 = 0xFF01

    // Reply and push codes.
    static let ok: UInt8 = 0
    static let error: UInt8 = 1
    static let selfInfo: UInt8 = 5
    static let msgSent: UInt8 = 6
    static let contactMsg: UInt8 = 7
    static let channelMsg: UInt8 = 8
    static let noMoreMessages: UInt8 = 10
    static let deviceInfo: UInt8 = 13
    static let contactMsgV3: UInt8 = 16
    static let channelMsgV3: UInt8 = 17
    static let channelInfo: UInt8 = 18
    static let channelData: UInt8 = 27
    static let messagesWaiting: UInt8 = 0x83
    static let logData: UInt8 = 0x88

    /// Everything a "give me the next message" can answer with.
    static let syncReplies: Set<UInt8> = [contactMsg, channelMsg, contactMsgV3, channelMsgV3,
                                          channelData, noMoreMessages, error]

    // ------------------------------------------------------------ commands

    static func appStart() -> Data {
        var d = Data([0x01, 0x03])
        d.append(Data("      ".utf8))        // reserved
        d.append(Data("moss".utf8))          // which app is talking
        return d
    }

    static func deviceQuery() -> Data {
        return Data([0x16, 0x03])
    }

    static func getChannel(_ index: Int) -> Data {
        return Data([0x1F, UInt8(index)])
    }

    static func setChannel(_ index: Int, name: String, secret: Data) -> Data {
        var d = Data([0x20, UInt8(index)])
        var nameBytes = Data(name.utf8Prefix(32).utf8)
        while nameBytes.count < 32 { nameBytes.append(0) }
        d.append(nameBytes)
        d.append(secret)
        return d
    }

    static func syncNextMessage() -> Data {
        return Data([0x0A])
    }

    /// One MOSS datagram onto a channel. zeroHop: a direct route with an empty
    /// path, so only radios in range hear it (no repeaters); otherwise flooded.
    static func sendChannelData(channel: Int, payload: Data, zeroHop: Bool) -> Data {
        var d = Data([0x3E, UInt8(channel)])
        d.append(zeroHop ? 0x00 : 0xFF)       // path length: 0 = direct, nobody repeats; 0xFF = flood
        d.appendU16(mossDataType)
        d.append(payload)
        return d
    }

    /// An ordinary chat message on a channel.
    static func sendChannelText(channel: Int, text: String) -> Data {
        var d = Data([0x03, 0x00, UInt8(channel)])
        d.appendU32(UInt32(Date().timeIntervalSince1970))
        d.append(Data(text.utf8Prefix(140).utf8))
        return d
    }

    /// The key MeshCore derives from a hashtag channel's name ("#moss").
    static func hashtagSecret(_ name: String) -> Data {
        return Data(SHA256.hash(data: Data(name.utf8))).slice(0, 16)
    }
}

// ---------------------------------------------------------------- replies

struct RadioInfo {
    var name: String
    var txPower: Int
    var maxTxPower: Int
    var frequency: Double       // MHz
    var bandwidth: Double       // kHz
    var spreadingFactor: Int
    var codingRate: Int

    var summary: String {
        let f = String(format: "%.3f", frequency)
        let bw = bandwidth == bandwidth.rounded() ? String(format: "%.0f", bandwidth) : String(format: "%.1f", bandwidth)
        return "\(f) MHz · BW \(bw) kHz · SF\(spreadingFactor) · CR\(codingRate) · \(txPower) dBm"
    }
}

struct ChannelSlot {
    var index: Int
    var name: String
    var secret: Data
}

/// One frame from the radio, decoded.
enum Frame {
    case ok
    case error(code: Int)
    case selfInfo(RadioInfo)
    case deviceInfo(maxChannels: Int, model: String, version: String)
    case channelInfo(ChannelSlot)
    /// A binary datagram heard on a channel. hops: 0xFF = came by direct route.
    case channelData(channel: Int, snr: Double, hops: UInt8, dataType: UInt16, payload: Data)
    case channelText(channel: Int, snr: Double?, hops: UInt8, text: String)
    case otherMessage
    case noMoreMessages
    case messagesWaiting
    /// The radio heard a packet (any packet): its signal.
    case log(snr: Double, rssi: Int)
    case other(code: UInt8)

    var code: UInt8 {
        switch self {
        case .ok: return MC.ok
        case .error: return MC.error
        case .selfInfo: return MC.selfInfo
        case .deviceInfo: return MC.deviceInfo
        case .channelInfo: return MC.channelInfo
        case .channelData: return MC.channelData
        case .channelText: return MC.channelMsgV3
        case .otherMessage: return MC.contactMsg
        case .noMoreMessages: return MC.noMoreMessages
        case .messagesWaiting: return MC.messagesWaiting
        case .log: return MC.logData
        case .other(let c): return c
        }
    }

    static func parse(_ d: Data) -> Frame? {
        guard d.count >= 1 else { return nil }
        let code = d.at(0)
        switch code {
        case MC.ok:
            return .ok
        case MC.error:
            return .error(code: d.count > 1 ? Int(d.at(1)) : 0)
        case MC.selfInfo:
            // type, tx power, max tx power, public key 32, lat 4, lon 4, multi-acks,
            // location policy, telemetry, manual-add, freq 4, bw 4, sf, cr, name
            guard d.count >= 58 else { return .other(code: code) }
            let name = String(decoding: d.slice(58), as: UTF8.self)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\0"))
            let info = RadioInfo(name: name,
                                 txPower: Int(d.at(2)),
                                 maxTxPower: Int(d.at(3)),
                                 frequency: Double(d.u32(48)) / 1000.0,
                                 bandwidth: Double(d.u32(52)) / 1000.0,
                                 spreadingFactor: Int(d.at(56)),
                                 codingRate: Int(d.at(57)))
            return .selfInfo(info)
        case MC.deviceInfo:
            guard d.count >= 2 else { return .other(code: code) }
            let fw = Int(d.at(1))
            if fw >= 3 && d.count >= 80 {
                let model = cString(d.slice(20, 60))
                let version = cString(d.slice(60, 80))
                return .deviceInfo(maxChannels: Int(d.at(3)), model: model, version: version)
            }
            return .deviceInfo(maxChannels: 8, model: "", version: "")
        case MC.channelInfo:
            guard d.count >= 50 else { return .other(code: code) }
            return .channelInfo(ChannelSlot(index: Int(d.at(1)), name: cString(d.slice(2, 34)),
                                            secret: d.slice(34, 50)))
        case MC.channelData:
            // snr, reserved 2, channel, path length, data type 2, data length, payload
            guard d.count >= 9 else { return .other(code: code) }
            let length = Int(d.at(8))
            return .channelData(channel: Int(d.at(4)), snr: Double(d.i8(1)) / 4.0, hops: d.at(5),
                                dataType: d.u16(6), payload: d.slice(9, 9 + length))
        case MC.channelMsgV3:
            // snr, reserved 2, channel, path length, text type, timestamp 4, text
            guard d.count >= 11 else { return .other(code: code) }
            return .channelText(channel: Int(d.at(4)), snr: Double(d.i8(1)) / 4.0, hops: d.at(5),
                                text: String(decoding: d.slice(11), as: UTF8.self))
        case MC.channelMsg:
            // channel, path length, text type, timestamp 4, text
            guard d.count >= 8 else { return .other(code: code) }
            return .channelText(channel: Int(d.at(1)), snr: nil, hops: d.at(2),
                                text: cString(d.slice(8)))
        case MC.contactMsg, MC.contactMsgV3:
            return .otherMessage
        case MC.noMoreMessages:
            return .noMoreMessages
        case MC.messagesWaiting:
            return .messagesWaiting
        case MC.logData:
            guard d.count >= 3 else { return .other(code: code) }
            return .log(snr: Double(d.i8(1)) / 4.0, rssi: Int(d.i8(2)))
        default:
            return .other(code: code)
        }
    }

    private static func cString(_ d: Data) -> String {
        let cut = d.firstIndex(of: 0).map { d.slice(0, $0 - d.startIndex) } ?? d
        return String(decoding: cut, as: UTF8.self)
    }
}

/// How a hop count reads to a person. 0xFF means it came by direct route
/// (our zero-hop sends); 0 means flooded but heard straight from the sender.
func describeHops(_ hops: UInt8?) -> String {
    guard let h = hops else { return "?" }
    if h == 0xFF || h == 0 { return "direct" }
    let n = Int(h & 0x3F)
    return n == 1 ? "1 repeater" : "\(n) repeaters"
}
