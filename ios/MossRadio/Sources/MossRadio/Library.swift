import Combine
import CryptoKit
import Foundation

/// Articles read over the radio, kept on the phone: they open instantly,
/// with no radio, and survive reinstalls.
struct SavedArticle: Codable, Identifiable, Hashable {
    var title: String
    var file: String
    var savedAt: Date
    var bytes: Int
    var spores: Int
    var seconds: Double
    var aliases: [String] = []

    var id: String { title }
}

@MainActor
final class Library: ObservableObject {
    @Published private(set) var articles: [SavedArticle] = []

    static let index = "articles.json"
    static let folder = "articles"

    init() {
        articles = Files.load([SavedArticle].self, from: Library.index) ?? []
        try? FileManager.default.createDirectory(at: Files.url(Library.folder), withIntermediateDirectories: true)
    }

    func find(_ title: String) -> SavedArticle? {
        let key = Library.key(title)
        return articles.first { Library.key($0.title) == key || $0.aliases.contains(where: { Library.key($0) == key }) }
    }

    func text(of a: SavedArticle) -> String? {
        guard let data = try? Data(contentsOf: Files.url(Library.folder).appendingPathComponent(a.file)) else { return nil }
        return String(decoding: data, as: UTF8.self)
    }

    @discardableResult
    func save(title: String, text: String, asked: String, spores: Int, seconds: Double) -> SavedArticle {
        let data = Data(text.utf8)
        let name = Data(SHA256.hash(data: Data(Library.key(title).utf8))).hex.prefix(20) + ".md"
        try? data.write(to: Files.url(Library.folder).appendingPathComponent(String(name)), options: .atomic)
        var aliases: [String] = []
        if Library.key(asked) != Library.key(title) { aliases.append(asked) }
        let a = SavedArticle(title: title, file: String(name), savedAt: Date(), bytes: data.count,
                             spores: spores, seconds: seconds, aliases: aliases)
        articles.removeAll { $0.title == title }
        articles.insert(a, at: 0)
        Files.save(articles, to: Library.index)
        return a
    }

    func delete(_ a: SavedArticle) {
        try? FileManager.default.removeItem(at: Files.url(Library.folder).appendingPathComponent(a.file))
        articles.removeAll { $0.title == a.title }
        Files.save(articles, to: Library.index)
    }

    static func key(_ title: String) -> String {
        return title.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            .replacingOccurrences(of: "_", with: " ")
    }
}
