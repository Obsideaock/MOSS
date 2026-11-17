import SwiftUI

struct VaultView: View {
    @EnvironmentObject var client: MossClient
    @EnvironmentObject var link: RadioLink

    @State private var checks: [CheckResult] = []
    @State private var confirmForget = false
    @State private var looking = false
    @State private var lookResult: String? = nil

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if let v = client.vault {
                        LabeledContent("Name", value: v.name)
                        LabeledContent("Fingerprint") {
                            Text(v.fingerprint)
                                .font(.body.monospaced())
                                .textSelection(.enabled)
                        }
                        LabeledContent("MOSS version", value: v.version ?? "not said")
                        LabeledContent("Trusted since", value: v.since.formatted(date: .abbreviated, time: .shortened))
                    } else {
                        Text("None yet. The first Seed Vault this phone hears is trusted and remembered.")
                            .foregroundStyle(.secondary)
                    }
                    if let other = client.otherVault {
                        Text("A Seed Vault with a different key also answered (\(other)). If your Pi was reset on purpose, forget the old vault below.")
                            .font(.callout)
                            .foregroundStyle(.orange)
                    }
                    Button {
                        looking = true
                        lookResult = nil
                        Task {
                            lookResult = await client.lookForVault()
                            looking = false
                        }
                    } label: {
                        HStack {
                            Label("Look for the Seed Vault", systemImage: "dot.radiowaves.left.and.right")
                            if looking {
                                Spacer()
                                ProgressView()
                            }
                        }
                    }
                    .disabled(looking || !link.isReady)
                    if let r = lookResult {
                        Text(r)
                            .font(.callout)
                    }
                    if client.vault != nil {
                        Button("Forget this Seed Vault", role: .destructive) {
                            confirmForget = true
                        }
                    }
                } header: {
                    Text("Trusted Seed Vault")
                } footer: {
                    Text("Check the fingerprint matches the one the Pi shows (moss vault-info, or the Seed Vault menu). Only that vault can answer your requests or send you pages.")
                }

                Section {
                    Button {
                        checks = SelfCheck.run()
                    } label: {
                        Label("Run the self-check", systemImage: "checkmark.seal")
                    }
                    if !checks.isEmpty {
                        let passed = checks.filter { $0.passed }.count
                        Text("\(passed) of \(checks.count) passed")
                            .font(.headline)
                            .foregroundStyle(passed == checks.count ? Color.green : Color.red)
                    }
                    ForEach(checks) { c in
                        HStack(alignment: .top) {
                            Image(systemName: c.passed ? "checkmark.circle.fill" : "xmark.octagon.fill")
                                .foregroundStyle(c.passed ? Color.green : Color.red)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.name)
                                if !c.detail.isEmpty {
                                    Text(c.detail)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                } header: {
                    Text("Self-check")
                } footer: {
                    Text("Replays messages made by the Python MOSS to prove this phone speaks it exactly: the encryption, the spores, the pings. Needs no radio.")
                }

                Section("Privacy") {
                    Text("Searches and articles are encrypted end to end: other people on the mesh can't see what you read. Range-test pings aren't encrypted, but carry only a test number. Locations from range tests stay on this phone unless you export them.")
                        .font(.callout)
                    Text("Your radio announces its name (and, if set to, its location) to the mesh by itself. In the MeshCore app, turn off sharing its location and don't use your real name for it.")
                        .font(.callout)
                }

                Section("About") {
                    LabeledContent("MOSS Radio", value: appVersion)
                    LabeledContent("Speaks", value: "MOSS protocol 4")
                    Link(destination: URL(string: "https://github.com/Obsideaock/MOSS")!) {
                        Label("MOSS on GitHub", systemImage: "link")
                    }
                    Text("Early release: MOSS's security hasn't been independently reviewed yet.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Seed Vault")
            .confirmationDialog("Forget this Seed Vault?", isPresented: $confirmForget, titleVisibility: .visible) {
                Button("Forget it", role: .destructive) {
                    client.forgetVault()
                    lookResult = nil
                }
            } message: {
                Text("The next Seed Vault heard will be trusted instead. Only do this if your vault was reset on purpose.")
            }
        }
    }
}
