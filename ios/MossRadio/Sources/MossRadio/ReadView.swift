import SwiftUI

enum ReadRoute: Hashable {
    case article(String)
}

struct ReadView: View {
    @EnvironmentObject var client: MossClient
    @EnvironmentObject var library: Library
    @EnvironmentObject var link: RadioLink

    @State private var query = ""
    @State private var results: [Moss.SearchResult] = []
    @State private var community: [String] = []
    @State private var searched = ""
    @State private var progress: TransferProgress? = nil
    @State private var busy = false
    @State private var failure: String? = nil
    @State private var path: [ReadRoute] = []

    var body: some View {
        NavigationStack(path: $path) {
            List {
                Section {
                    HStack {
                        TextField("Search Wikipedia", text: $query)
                            .submitLabel(.search)
                            .onSubmit { search() }
                            .autocorrectionDisabled()
                        Button("Search") { search() }
                            .disabled(busy || query.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    if !link.isReady {
                        Text("Saved articles open without the radio. To search or fetch new ones, connect in the Radio tab.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    if let n = client.notice {
                        Text(n)
                            .font(.callout)
                            .foregroundStyle(.orange)
                    }
                }

                if busy, let p = progress {
                    Section("Searching over the radio") {
                        TransferPanel(progress: p)
                    }
                }
                if let f = failure {
                    Section {
                        Text(f)
                            .foregroundStyle(.red)
                    }
                }

                if !results.isEmpty || !searched.isEmpty {
                    Section("Wikipedia") {
                        if results.isEmpty {
                            Text("Nothing found for \"\(searched)\".")
                                .foregroundStyle(.secondary)
                        }
                        ForEach(results) { r in
                            NavigationLink(value: ReadRoute.article(r.title)) {
                                VStack(alignment: .leading, spacing: 3) {
                                    HStack {
                                        Text(r.title)
                                            .font(.headline)
                                        if library.find(r.title) != nil {
                                            Image(systemName: "checkmark.circle.fill")
                                                .foregroundStyle(mossGreen)
                                                .font(.caption)
                                        }
                                    }
                                    if !r.blurb.isEmpty {
                                        Text(r.blurb)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(3)
                                    }
                                }
                            }
                        }
                    }
                }
                if !community.isEmpty {
                    Section {
                        ForEach(community, id: \.self) { t in
                            Text(t)
                        }
                    } header: {
                        Text("Community pages")
                    } footer: {
                        Text("Opening community pages comes in a later version of the app; read them in the laptop reader for now.")
                    }
                }

                Section("Saved on this phone") {
                    if library.articles.isEmpty {
                        Text("Articles you read are kept here, and open instantly with no radio.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(library.articles) { a in
                        NavigationLink(value: ReadRoute.article(a.title)) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(a.title)
                                Text("\(a.spores) spores · \(Int(a.seconds)) s over the radio · \(a.savedAt.formatted(date: .abbreviated, time: .omitted))")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .onDelete { offsets in
                        let doomed = offsets.map { library.articles[$0] }
                        for a in doomed {
                            library.delete(a)
                        }
                    }
                }
            }
            .navigationTitle("Read")
            .navigationDestination(for: ReadRoute.self) { route in
                switch route {
                case .article(let title):
                    ArticleScreen(title: title) { next in
                        path.append(.article(next))
                    }
                }
            }
        }
    }

    private func search() {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, !busy else { return }
        busy = true
        failure = nil
        progress = TransferProgress()
        Task {
            do {
                let found = try await client.search(q) { p in
                    progress = p
                }
                results = found.wiki
                community = found.community
                searched = q
            } catch {
                failure = "\(error)"
            }
            busy = false
        }
    }
}

/// Where a transfer is: the stage, and a grid of spores filling in.
struct TransferPanel: View {
    let progress: TransferProgress

    var stageText: String {
        switch progress.stage {
        case "requesting":
            return progress.message.isEmpty ? "Asking the Seed Vault (try \(max(progress.attempt, 1)))…" : progress.message.capitalizedFirst + "…"
        case "queued": return "In line at the Seed Vault (position \(progress.queuePosition))"
        case "fetching": return "The Seed Vault is getting it ready…"
        case "receiving": return "Spores arriving"
        case "repairing": return "Asking again for the missing spores"
        case "verifying": return "Checking it arrived whole"
        default: return progress.stage
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                ProgressView()
                Text(stageText)
            }
            if progress.total > 0 {
                SporeGrid(total: progress.total, have: progress.have)
                HStack {
                    Text("\(progress.have.count) of \(progress.total) spores")
                    Spacer()
                    if let eta = progress.eta {
                        Text("about \(Int(eta.rounded())) s left")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Text(String(format: "%.0f s so far", progress.elapsed))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }
}

struct SporeGrid: View {
    let total: Int
    let have: Set<Int>

    var body: some View {
        if total > 600 {
            ProgressView(value: Double(have.count), total: Double(total))
        } else {
            let perRow = min(max(total, 1), 20)
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(minimum: 4, maximum: 16), spacing: 2), count: perRow),
                      spacing: 2) {
                ForEach(0..<total, id: \.self) { i in
                    RoundedRectangle(cornerRadius: 2)
                        .fill(have.contains(i) ? mossGreen : Color.gray.opacity(0.25))
                        .aspectRatio(1, contentMode: .fit)
                }
            }
        }
    }
}

extension String {
    var capitalizedFirst: String {
        guard let f = first else { return self }
        return f.uppercased() + dropFirst()
    }
}

struct ArticleScreen: View {
    let title: String
    let open: (String) -> Void

    @EnvironmentObject var client: MossClient
    @EnvironmentObject var library: Library
    @State private var text: String? = nil
    @State private var shownTitle: String? = nil
    @State private var saved: SavedArticle? = nil
    @State private var progress = TransferProgress()
    @State private var loading = false
    @State private var failure: String? = nil

    var body: some View {
        Group {
            if let t = text {
                ArticleBody(markdown: t, open: open, footer: footer)
            } else if loading {
                List {
                    Section("Fetching over the radio") {
                        TransferPanel(progress: progress)
                    }
                }
            } else if let f = failure {
                VStack(spacing: 16) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.largeTitle)
                        .foregroundStyle(.orange)
                    Text(f)
                        .multilineTextAlignment(.center)
                    Button("Try again") { load(refresh: true) }
                        .buttonStyle(.borderedProminent)
                }
                .padding()
            } else {
                ProgressView()
            }
        }
        .navigationTitle(shownTitle ?? title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if text != nil && !loading {
                Button {
                    load(refresh: true)
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
            }
        }
        .task {
            if text == nil && !loading { load(refresh: false) }
        }
    }

    private var footer: String {
        guard let s = saved else { return "" }
        return "Saved on this phone · \(s.spores) spores, \(Int(s.seconds)) s over the radio · \(s.savedAt.formatted(date: .abbreviated, time: .shortened))"
    }

    private func load(refresh: Bool) {
        if !refresh, let a = library.find(title), let t = library.text(of: a) {
            text = t
            shownTitle = a.title
            saved = a
            return
        }
        guard !loading else { return }
        loading = true
        failure = nil
        progress = TransferProgress()
        Task {
            do {
                let got = try await client.article(title) { p in
                    progress = p
                }
                let a = library.save(title: got.title, text: got.text, asked: title, spores: got.spores, seconds: got.seconds)
                saved = a
                shownTitle = got.title
                text = got.text
            } catch {
                failure = "\(error)"
            }
            loading = false
        }
    }
}

struct ArticleBody: View {
    let blocks: [ArticleBlock]
    let open: (String) -> Void
    let footer: String

    init(markdown: String, open: @escaping (String) -> Void, footer: String) {
        self.blocks = ArticleText.parse(markdown)
        self.open = open
        self.footer = footer
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                ForEach(blocks) { b in
                    view(for: b)
                }
                if !footer.isEmpty {
                    Text(footer)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.top, 16)
                }
            }
            .padding()
        }
        .environment(\.openURL, OpenURLAction { url in
            if let t = ArticleText.title(from: url) {
                open(t)
                return .handled
            }
            return .systemAction
        })
    }

    private func headingFont(_ level: Int) -> Font {
        if level <= 1 { return Font.title.bold() }
        if level == 2 { return Font.title2.bold() }
        return Font.headline
    }

    @ViewBuilder
    private func view(for b: ArticleBlock) -> some View {
        switch b.kind {
        case .heading(let level, let text):
            Text(ArticleText.inline(text))
                .font(headingFont(level))
                .padding(.top, level <= 2 ? 8 : 4)
        case .paragraph(let text):
            Text(ArticleText.inline(text))
        case .bullet(let text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("•")
                Text(ArticleText.inline(text))
            }
        case .numbered(let n, let text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("\(n).")
                    .monospacedDigit()
                Text(ArticleText.inline(text))
            }
        case .math(let formula):
            Text(formula)
                .font(.body.monospaced())
                .frame(maxWidth: .infinity)
                .padding(10)
                .background(Color.gray.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
        case .facts(let title, let rows):
            VStack(alignment: .leading, spacing: 6) {
                if !title.isEmpty {
                    Text(title)
                        .font(.headline)
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { item in
                    HStack(alignment: .firstTextBaseline) {
                        Text(item.element.0)
                            .font(.callout.bold())
                            .frame(width: 120, alignment: .leading)
                        Text(ArticleText.inline(item.element.1))
                            .font(.callout)
                    }
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(mossGreen.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
        case .table(let caption, let header, let rows):
            VStack(alignment: .leading, spacing: 6) {
                if !caption.isEmpty {
                    Text(caption)
                        .font(.caption.bold())
                }
                ScrollView(.horizontal) {
                    Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 6) {
                        if !header.isEmpty {
                            GridRow {
                                ForEach(Array(header.enumerated()), id: \.offset) { cell in
                                    Text(cell.element)
                                        .font(.caption.bold())
                                }
                            }
                            Divider()
                        }
                        ForEach(Array(rows.enumerated()), id: \.offset) { row in
                            GridRow {
                                ForEach(Array(row.element.enumerated()), id: \.offset) { cell in
                                    Text(ArticleText.inline(cell.element))
                                        .font(.caption)
                                }
                            }
                        }
                    }
                    .padding(10)
                }
                .background(Color.gray.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
            }
        }
    }
}
