import SwiftUI
import AppKit

struct Config: Codable {
    var gitlab: GitLabSettings = GitLabSettings()
    var refreshIntervalSeconds: Int = 60
    var repositories: [Repository] = []
}

struct GitLabSettings: Codable {
    var baseUrl: String = "https://gitlab.com"
    var accessToken: String = ""
}

struct Repository: Identifiable, Codable, Equatable {
    var id: String
    var name: String
    var projectPath: String?
    var branches: [BranchWatch]

    var path: String { projectPath ?? id }

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case projectPath
        case branches
        case branch
        case branchSelector
    }

    init(id: String, name: String, projectPath: String?, branches: [BranchWatch]) {
        self.id = id
        self.name = name
        self.projectPath = projectPath
        self.branches = branches
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        name = try container.decodeIfPresent(String.self, forKey: .name) ?? id
        projectPath = try container.decodeIfPresent(String.self, forKey: .projectPath)

        if let decodedBranches = try container.decodeIfPresent([BranchWatch].self, forKey: .branches),
           !decodedBranches.isEmpty {
            branches = decodedBranches
        } else if let selector = try container.decodeIfPresent(BranchSelector.self, forKey: .branchSelector) {
            branches = [BranchWatch(selector: selector)]
        } else {
            let branch = try container.decodeIfPresent(String.self, forKey: .branch) ?? "main"
            branches = [BranchWatch(selector: .fixed(branch))]
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(name, forKey: .name)
        try container.encodeIfPresent(projectPath, forKey: .projectPath)
        try container.encode(branches, forKey: .branches)
    }
}

struct BranchWatch: Identifiable, Codable, Equatable {
    var id: UUID = UUID()
    var selector: BranchSelector

    enum CodingKeys: String, CodingKey {
        case id
        case selector
    }

    init(id: UUID = UUID(), selector: BranchSelector) {
        self.id = id
        self.selector = selector
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(UUID.self, forKey: .id) ?? UUID()
        selector = try container.decode(BranchSelector.self, forKey: .selector)
    }
}

enum BranchSelector: Equatable, Codable {
    case fixed(String)
    case rule(prefix: String, format: String)
    case regex(String)

    enum CodingKeys: String, CodingKey { case type, value, prefix, format }

    var label: String {
        switch self {
        case .fixed(let branch): return branch
        case .rule(let prefix, let format): return "\(prefix)-... (\(format))"
        case .regex(let pattern): return pattern
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = try container.decodeIfPresent(String.self, forKey: .type) ?? "fixed"
        switch type {
        case "rule":
            self = .rule(
                prefix: try container.decodeIfPresent(String.self, forKey: .prefix) ?? "test",
                format: try container.decodeIfPresent(String.self, forKey: .format) ?? "yyyymmdd"
            )
        case "regex":
            self = .regex(try container.decodeIfPresent(String.self, forKey: .value) ?? "")
        default:
            self = .fixed(try container.decodeIfPresent(String.self, forKey: .value) ?? "main")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .fixed(let branch):
            try container.encode("fixed", forKey: .type)
            try container.encode(branch, forKey: .value)
        case .rule(let prefix, let format):
            try container.encode("rule", forKey: .type)
            try container.encode(prefix, forKey: .prefix)
            try container.encode(format, forKey: .format)
        case .regex(let pattern):
            try container.encode("regex", forKey: .type)
            try container.encode(pattern, forKey: .value)
        }
    }
}

struct GitLabProject: Identifiable, Decodable {
    let id: Int
    let name: String
    let pathWithNamespace: String
    let defaultBranch: String?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case pathWithNamespace = "path_with_namespace"
        case defaultBranch = "default_branch"
    }
}

struct GitLabBranch: Identifiable, Decodable {
    let name: String
    var id: String { name }
}

@MainActor
final class ConfigStore: ObservableObject {
    @Published var config = Config()
    @Published var searchText = ""
    @Published var projects: [GitLabProject] = []
    @Published var isSearching = false
    @Published var errorMessage = ""

    let configURL: URL

    init() {
        let path = ProcessInfo.processInfo.environment["STATUS_HUB_CONFIG_FILE"]
            ?? "runtime/config.json"
        self.configURL = URL(fileURLWithPath: NSString(string: path).expandingTildeInPath)
        load()
    }

    func load() {
        guard let data = try? Data(contentsOf: configURL),
              let decoded = try? JSONDecoder().decode(Config.self, from: data) else {
            return
        }
        config = decoded
    }

    func save() {
        do {
            try FileManager.default.createDirectory(at: configURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(config).write(to: configURL, options: .atomic)
            NSApplication.shared.terminate(nil)
        } catch {
            errorMessage = "保存失败：\(error.localizedDescription)"
        }
    }

    func searchProjects() {
        let baseUrl = cleanBaseUrl
        let token = config.gitlab.accessToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !baseUrl.isEmpty, !token.isEmpty else {
            errorMessage = "请先填写 GitLab 地址和 Access Token"
            return
        }
        isSearching = true
        errorMessage = ""
        Task {
            do {
                let result = try await GitLabAPI.fetchProjects(baseUrl: baseUrl, token: token, search: searchText)
                await MainActor.run {
                    self.projects = result
                    self.isSearching = false
                }
            } catch {
                await MainActor.run {
                    self.errorMessage = error.localizedDescription
                    self.isSearching = false
                }
            }
        }
    }

    func searchBranches(projectPath: String, term: String) async -> [String] {
        do {
            return try await GitLabAPI.fetchBranches(
                baseUrl: cleanBaseUrl,
                token: config.gitlab.accessToken,
                projectPath: projectPath,
                search: term.isEmpty ? nil : term
            ).map(\.name)
        } catch {
            return []
        }
    }

    func add(project: GitLabProject) {
        guard !config.repositories.contains(where: { $0.path == project.pathWithNamespace }) else { return }
        config.repositories.append(Repository(
            id: project.pathWithNamespace,
            name: project.name,
            projectPath: project.pathWithNamespace,
            branches: [BranchWatch(selector: .fixed(project.defaultBranch ?? "main"))]
        ))
    }

    var cleanBaseUrl: String {
        var value = config.gitlab.baseUrl.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") { value.removeLast() }
        return value
    }
}

enum GitLabAPI {
    static func fetchProjects(baseUrl: String, token: String, search: String) async throws -> [GitLabProject] {
        var components = URLComponents(string: "\(baseUrl)/api/v4/projects")!
        components.queryItems = [
            URLQueryItem(name: "membership", value: "true"),
            URLQueryItem(name: "order_by", value: "last_activity_at"),
            URLQueryItem(name: "sort", value: "desc"),
            URLQueryItem(name: "per_page", value: "20"),
            URLQueryItem(name: "search", value: search)
        ]
        return try await get(components.url!, token: token)
    }

    static func fetchBranches(baseUrl: String, token: String, projectPath: String, search: String?) async throws -> [GitLabBranch] {
        let encodedProject = projectPath.addingPercentEncoding(withAllowedCharacters: CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? projectPath
        var components = URLComponents(string: "\(baseUrl)/api/v4/projects/\(encodedProject)/repository/branches")!
        var query = [URLQueryItem(name: "per_page", value: "100")]
        if let search, !search.isEmpty {
            query.append(URLQueryItem(name: "search", value: search))
        }
        components.queryItems = query
        return try await get(components.url!, token: token)
    }

    private static func get<T: Decodable>(_ url: URL, token: String) async throws -> T {
        var request = URLRequest(url: url)
        request.setValue(token, forHTTPHeaderField: "PRIVATE-TOKEN")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw NSError(domain: "GitLab", code: -1, userInfo: [NSLocalizedDescriptionKey: "无效响应"])
        }
        if http.statusCode == 401 {
            throw NSError(domain: "GitLab", code: 401, userInfo: [NSLocalizedDescriptionKey: "Token 无效或缺少 read_api 权限"])
        }
        if http.statusCode >= 400 {
            throw NSError(domain: "GitLab", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: "GitLab API \(http.statusCode)"])
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}

struct ContentView: View {
    @StateObject var store = ConfigStore()

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HSplitView {
                leftPane
                    .frame(minWidth: 280, idealWidth: 320)
                rightPane
                    .frame(minWidth: 420)
            }
            Divider()
            footer
        }
        .frame(width: 820, height: 620)
    }

    private var header: some View {
        HStack {
            Text("GitLab 监控配置")
                .font(.title2)
                .fontWeight(.semibold)
            Spacer()
            Text("\(store.config.repositories.count) 个项目")
                .foregroundColor(.secondary)
        }
        .padding()
    }

    private var leftPane: some View {
        VStack(alignment: .leading, spacing: 12) {
            GroupBox("连接") {
                VStack(alignment: .leading, spacing: 8) {
                    TextField("https://gitlab.example.com", text: $store.config.gitlab.baseUrl)
                    SecureField("glpat-...", text: $store.config.gitlab.accessToken)
                    HStack {
                        TextField("60", value: $store.config.refreshIntervalSeconds, format: .number)
                            .frame(width: 80)
                        Text("秒刷新")
                            .foregroundColor(.secondary)
                    }
                }
                .textFieldStyle(.roundedBorder)
                .padding(.top, 4)
            }

            GroupBox("搜索项目") {
                VStack(spacing: 8) {
                    HStack {
                        TextField("项目名", text: $store.searchText)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit { store.searchProjects() }
                        Button {
                            store.searchProjects()
                        } label: {
                            if store.isSearching {
                                ProgressView().scaleEffect(0.55)
                            } else {
                                Image(systemName: "magnifyingglass")
                            }
                        }
                    }
                    if !store.errorMessage.isEmpty {
                        Text(store.errorMessage)
                            .font(.caption)
                            .foregroundColor(.red)
                            .lineLimit(2)
                    }
                    List(store.projects) { project in
                        Button {
                            store.add(project: project)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(project.name)
                                Text(project.pathWithNamespace)
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.top, 4)
            }
        }
        .padding()
    }

    private var rightPane: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("已选择")
                .font(.headline)
            if store.config.repositories.isEmpty {
                Spacer()
                Text("从左侧搜索并添加项目")
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity)
                Spacer()
            } else {
                List {
                    ForEach($store.config.repositories) { $repo in
                        RepositoryEditor(
                            repository: $repo,
                            remove: {
                                store.config.repositories.removeAll { $0.id == repo.id }
                            },
                            searchBranches: { term in
                                await store.searchBranches(projectPath: repo.path, term: term)
                            }
                        )
                    }
                }
            }
        }
        .padding()
    }

    private var footer: some View {
        HStack {
            Button("取消") {
                NSApplication.shared.terminate(nil)
            }
            Spacer()
            Button("保存") {
                store.save()
            }
            .buttonStyle(.borderedProminent)
        }
        .padding()
    }
}

struct RepositoryEditor: View {
    @Binding var repository: Repository
    let remove: () -> Void
    let searchBranches: (String) async -> [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                VStack(alignment: .leading) {
                    Text(repository.name).fontWeight(.semibold)
                    Text(repository.path).font(.caption).foregroundColor(.secondary)
                }
                Spacer()
                Button(action: remove) {
                    Image(systemName: "xmark.circle")
                }
                .buttonStyle(.plain)
            }
            ForEach($repository.branches) { $watch in
                HStack(alignment: .top, spacing: 8) {
                    BranchEditor(watch: $watch, searchBranches: searchBranches)
                    Button {
                        let id = watch.id
                        repository.branches.removeAll { $0.id == id }
                    } label: {
                        Image(systemName: "minus.circle")
                    }
                    .buttonStyle(.plain)
                    .disabled(repository.branches.count <= 1)
                    .help(repository.branches.count <= 1 ? "至少保留一个分支" : "删除分支")
                }
            }
            Button {
                repository.branches.append(BranchWatch(selector: .fixed("main")))
            } label: {
                Label("添加分支", systemImage: "plus.circle")
            }
            .buttonStyle(.plain)
        }
        .padding(.vertical, 6)
    }
}

struct BranchEditor: View {
    @Binding var watch: BranchWatch
    let searchBranches: (String) async -> [String]
    @State private var mode = "fixed"
    @State private var fixed = "main"
    @State private var prefix = "test"
    @State private var format = "yyyymmdd"
    @State private var regex = "^test-\\d{8}$"
    @State private var branches: [String] = []
    @State private var isLoaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Picker("", selection: $mode) {
                Text("固定分支").tag("fixed")
                Text("动态匹配最新").tag("rule")
                Text("自定义正则").tag("regex")
            }
            .pickerStyle(.segmented)
            .onChange(of: mode) { _ in publish() }

            switch mode {
            case "rule":
                HStack {
                    TextField("前缀", text: $prefix)
                        .frame(width: 100)
                    Picker("", selection: $format) {
                        Text("YYYYMMDD").tag("yyyymmdd")
                        Text("YYYY-MM-DD").tag("yyyymmddDashed")
                        Text("YYYY.MM.DD").tag("yyyymmddDotted")
                        Text("YYYYMMDD-尾缀").tag("yyyymmddWithTail")
                    }
                    .frame(width: 170)
                }
                .onChange(of: prefix) { _ in publish() }
                .onChange(of: format) { _ in publish() }
            case "regex":
                TextField("^test-\\d{8}$", text: $regex)
                    .font(.system(.body, design: .monospaced))
                    .onChange(of: regex) { _ in publish() }
            default:
                HStack {
                    TextField("main", text: $fixed)
                        .onSubmit { publish() }
                        .onChange(of: fixed) { _ in publish() }
                    Button("加载分支") {
                        Task { branches = await searchBranches(fixed) }
                    }
                }
                if !branches.isEmpty {
                    Picker("", selection: $fixed) {
                        ForEach(branches, id: \.self) { Text($0).tag($0) }
                    }
                    .onChange(of: fixed) { _ in publish() }
                }
            }
        }
        .textFieldStyle(.roundedBorder)
        .onAppear {
            guard !isLoaded else { return }
            isLoaded = true
            load()
        }
    }

    private func load() {
        switch watch.selector {
        case .fixed(let value):
            mode = "fixed"; fixed = value
        case .rule(let value, let fmt):
            mode = "rule"; prefix = value; format = fmt
        case .regex(let value):
            mode = "regex"; regex = value
        }
    }

    private func publish() {
        switch mode {
        case "rule":
            watch.selector = .rule(prefix: prefix, format: format)
        case "regex":
            watch.selector = .regex(regex)
        default:
            watch.selector = .fixed(fixed)
        }
    }
}

@main
struct GitLabConfiguratorApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .windowStyle(.titleBar)
    }
}
