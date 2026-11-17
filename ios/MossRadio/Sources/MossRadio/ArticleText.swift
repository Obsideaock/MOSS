import Foundation
import SwiftUI

// MOSS articles are Markdown with a few extras: [[Title|text]] links,
// ":::facts" and ":::table" blocks (tab-separated rows; a leading "!" marks
// a caption or header), and $$formulas$$. This turns them into blocks a
// SwiftUI view can show. Formulas are shown as their TeX source for now.

struct ArticleBlock: Identifiable {
    enum Kind {
        case heading(level: Int, text: String)
        case paragraph(String)
        case bullet(String)
        case numbered(Int, String)
        case facts(title: String, rows: [(String, String)])
        case table(caption: String, header: [String], rows: [[String]])
        case math(String)
    }
    let id: Int
    let kind: Kind
}

enum ArticleText {
    static func parse(_ markdown: String) -> [ArticleBlock] {
        var kinds: [ArticleBlock.Kind] = []
        var paragraph: [String] = []

        func flush() {
            if !paragraph.isEmpty {
                kinds.append(.paragraph(paragraph.joined(separator: " ")))
                paragraph = []
            }
        }

        let lines = markdown.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var i = 0
        while i < lines.count {
            let line = lines[i]
            let t = line.trimmingCharacters(in: .whitespaces)

            if t.hasPrefix(":::") && t != ":::" {
                flush()
                let head = String(t.dropFirst(3))
                let parts = head.split(separator: " ", maxSplits: 1)
                let kind = parts.first.map(String.init) ?? ""
                let caption = parts.count > 1 ? String(parts[1]) : ""
                var body: [String] = []
                i += 1
                while i < lines.count && lines[i].trimmingCharacters(in: .whitespaces) != ":::" {
                    body.append(lines[i])
                    i += 1
                }
                i += 1                                       // the closing :::
                kinds.append(block(kind: kind, caption: caption, body: body))
                continue
            }
            if t.isEmpty {
                flush()
                i += 1
                continue
            }
            if t.hasPrefix("$$") {
                flush()
                var formula = String(t.dropFirst(2))
                if formula.hasSuffix("$$") {
                    formula = String(formula.dropLast(2))
                } else {
                    i += 1
                    while i < lines.count {
                        let more = lines[i].trimmingCharacters(in: .whitespaces)
                        if more.hasSuffix("$$") {
                            formula += " " + String(more.dropLast(2))
                            break
                        }
                        formula += " " + more
                        i += 1
                    }
                }
                kinds.append(.math(formula.trimmingCharacters(in: .whitespaces)))
                i += 1
                continue
            }
            if t.hasPrefix("#") {
                flush()
                let level = t.prefix(while: { $0 == "#" }).count
                let text = t.dropFirst(level).trimmingCharacters(in: .whitespaces)
                kinds.append(.heading(level: min(level, 4), text: text))
                i += 1
                continue
            }
            if t.hasPrefix("- ") || t.hasPrefix("* ") {
                flush()
                kinds.append(.bullet(String(t.dropFirst(2))))
                i += 1
                continue
            }
            if let n = numberedItem(t) {
                flush()
                kinds.append(.numbered(n.0, n.1))
                i += 1
                continue
            }
            paragraph.append(t.hasPrefix("> ") ? String(t.dropFirst(2)) : t)
            i += 1
        }
        flush()
        return kinds.enumerated().map { ArticleBlock(id: $0.offset, kind: $0.element) }
    }

    private static func numberedItem(_ t: String) -> (Int, String)? {
        let digits = t.prefix(while: { $0.isNumber })
        guard !digits.isEmpty, digits.count <= 3 else { return nil }
        let rest = t.dropFirst(digits.count)
        guard rest.hasPrefix(". ") else { return nil }
        return (Int(digits) ?? 0, String(rest.dropFirst(2)))
    }

    private static func block(kind: String, caption: String, body: [String]) -> ArticleBlock.Kind {
        let rows = body.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        switch kind {
        case "facts":
            var pairs: [(String, String)] = []
            for r in rows where !r.hasPrefix("!") {
                let cells = r.components(separatedBy: "\t")
                pairs.append((cells.first ?? "", cells.dropFirst().joined(separator: " ")))
            }
            return .facts(title: caption, rows: pairs)
        case "table":
            var header: [String] = []
            var cells: [[String]] = []
            for r in rows {
                if r.hasPrefix("!") && header.isEmpty {
                    header = String(r.dropFirst()).components(separatedBy: "\t")
                } else {
                    cells.append(r.components(separatedBy: "\t"))
                }
            }
            return .table(caption: caption, header: header, rows: cells)
        default:
            return .paragraph(rows.joined(separator: " "))
        }
    }

    // ------------------------------------------------------------ inline text

    static let linkScheme = "mossarticle"

    /// [[Title|text]] → a tappable link to that article; $x$ → monospaced; the
    /// rest is ordinary inline Markdown (bold, italics, links).
    static func inline(_ text: String) -> AttributedString {
        var s = text
        s = replace(s, pattern: "\\[\\[([^\\]|]+)\\|([^\\]]+)\\]\\]") { groups in
            "[\(groups[2])](\(linkScheme):\(encode(groups[1])))"
        }
        s = replace(s, pattern: "\\[\\[([^\\]|]+)\\]\\]") { groups in
            "[\(groups[1])](\(linkScheme):\(encode(groups[1])))"
        }
        s = replace(s, pattern: "\\$([^$\\n]+)\\$") { groups in
            "`\(groups[1])`"
        }
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        if let a = try? AttributedString(markdown: s, options: options) {
            return a
        }
        return AttributedString(text)
    }

    /// The article title a mossarticle: link points at.
    static func title(from url: URL) -> String? {
        guard url.scheme == linkScheme else { return nil }
        let raw = url.absoluteString.dropFirst(linkScheme.count + 1)
        return String(raw).removingPercentEncoding
    }

    private static func encode(_ title: String) -> String {
        return title.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
    }

    private static func replace(_ text: String, pattern: String, with make: ([String]) -> String) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return text }
        let ns = text as NSString
        var out = ""
        var last = 0
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            var groups: [String] = []
            for g in 0..<m.numberOfRanges {
                let r = m.range(at: g)
                groups.append(r.location == NSNotFound ? "" : ns.substring(with: r))
            }
            out += make(groups)
            last = m.range.location + m.range.length
        }
        out += ns.substring(from: last)
        return out
    }
}
