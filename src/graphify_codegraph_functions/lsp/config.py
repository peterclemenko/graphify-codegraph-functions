import os

class LSPConfigurationError(Exception):
    """Raised when there is an error in configuring the LSP, such as a missing binary or environment."""
    pass


class LSPCommunicationError(Exception):
    """Raised when there is an error in JSON-RPC communication or process management."""
    pass


SUPPORTED_ECOSYSTEMS: dict[str, dict[str, any]] = {
    "typescript": {
        "binaries": ["vtsls", "typescript-language-server"],
        "manifests": ["tsconfig.json", "package.json"],
        "extensions": [".ts", ".tsx"],
        "language_id": "typescript",
        "args": ["--stdio"]
    },
    "javascript": {
        "binaries": ["vtsls", "typescript-language-server", "quick-lint-js"],
        "manifests": ["package.json", "jsconfig.json"],
        "extensions": [".js", ".jsx"],
        "language_id": "javascript",
        "args": ["--stdio"]
    },
    "html": {
        "binaries": ["html-languageserver", "vscode-html-language-server"],
        "manifests": ["package.json", "index.html"],
        "extensions": [".html"],
        "language_id": "html",
        "args": ["--stdio"]
    },
    "css": {
        "binaries": ["css-languageserver", "vscode-css-language-server"],
        "manifests": ["package.json"],
        "extensions": [".css"],
        "language_id": "css",
        "args": ["--stdio"]
    },
    "scss": {
        "binaries": ["css-languageserver", "vscode-css-language-server"],
        "manifests": ["package.json"],
        "extensions": [".scss"],
        "language_id": "scss",
        "args": ["--stdio"]
    },
    "less": {
        "binaries": ["css-languageserver", "vscode-css-language-server"],
        "manifests": ["package.json"],
        "extensions": [".less"],
        "language_id": "less",
        "args": ["--stdio"]
    },
    "vue": {
        "binaries": ["vue-language-server", "vlar"],
        "manifests": ["package.json", "nuxt.config.js", "nuxt.config.ts"],
        "extensions": [".vue"],
        "language_id": "vue",
        "args": ["--stdio"]
    },
    "angular": {
        "binaries": ["angular-language-server", "ng-ls"],
        "manifests": ["angular.json", "package.json"],
        "extensions": [".ts", ".component.html"],
        "language_id": "angular",
        "args": ["--stdio"]
    },
    "svelte": {
        "binaries": ["svelte-language-server", "svelteserver"],
        "manifests": ["svelte.config.js", "package.json"],
        "extensions": [".svelte"],
        "language_id": "svelte",
        "args": ["--stdio"]
    },
    "astro": {
        "binaries": ["astro-ls", "astro-language-server"],
        "manifests": ["astro.config.mjs", "package.json"],
        "extensions": [".astro"],
        "language_id": "astro",
        "args": ["--stdio"]
    },
    "swift": {
        "binaries": ["sourcekit-lsp"],
        "manifests": ["Package.swift"],
        "extensions": [".swift"],
        "language_id": "swift",
        "args": ["--stdio"]
    },
    "objective-c": {
        "binaries": ["clangd", "sourcekit-lsp"],
        "manifests": ["Makefile", "Info.plist"],
        "extensions": [".m", ".h"],
        "language_id": "objective-c",
        "args": ["--stdio"]
    },
    "kotlin": {
        "binaries": ["kotlin-language-server"],
        "manifests": ["build.gradle.kts", "build.gradle", "settings.gradle.kts"],
        "extensions": [".kt", ".kts"],
        "language_id": "kotlin",
        "args": ["--stdio"]
    },
    "java": {
        "binaries": ["jdtls", "java-lsp-server"],
        "manifests": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "extensions": [".java"],
        "language_id": "java",
        "args": ["--stdio"]
    },
    "dart": {
        "binaries": ["dart"],
        "manifests": ["pubspec.yaml"],
        "extensions": [".dart"],
        "language_id": "dart",
        "args": ["language-server"]
    },
    "flutter": {
        "binaries": ["dart", "flutter"],
        "manifests": ["pubspec.yaml"],
        "extensions": [".dart"],
        "language_id": "dart",
        "args": ["language-server"]
    },
    "rust": {
        "binaries": ["rust-analyzer"],
        "manifests": ["Cargo.toml"],
        "extensions": [".rs"],
        "language_id": "rust",
        "args": ["--stdio"]
    },
    "go": {
        "binaries": ["gopls"],
        "manifests": ["go.mod"],
        "extensions": [".go"],
        "language_id": "go",
        "args": ["-mode=stdio"]
    },
    "cpp": {
        "binaries": ["clangd"],
        "manifests": ["CMakeLists.txt", "compile_commands.json"],
        "extensions": [".cpp", ".cc", ".hpp", ".h"],
        "language_id": "cpp",
        "args": ["--stdio"]
    },
    "c": {
        "binaries": ["clangd"],
        "manifests": ["Makefile", "compile_commands.json"],
        "extensions": [".c", ".h"],
        "language_id": "c",
        "args": ["--stdio"]
    },
    "csharp": {
        "binaries": ["csharp-ls", "omnisharp"],
        "manifests": ["*.csproj", "*.sln"],
        "extensions": [".cs"],
        "language_id": "csharp",
        "args": ["--stdio"]
    },
    "fsharp": {
        "binaries": ["fsautocomplete"],
        "manifests": ["*.fsproj"],
        "extensions": [".fs", ".fsx"],
        "language_id": "fsharp",
        "args": ["--stdio"]
    },
    "scala": {
        "binaries": ["metals"],
        "manifests": ["build.sbt"],
        "extensions": [".scala", ".sc"],
        "language_id": "scala",
        "args": ["--stdio"]
    },
    "haskell": {
        "binaries": ["haskell-language-server", "hls"],
        "manifests": ["stack.yaml", "cabal.project", "package.yaml"],
        "extensions": [".hs", ".lhs"],
        "language_id": "haskell",
        "args": ["--lsp"]
    },
    "ocaml": {
        "binaries": ["ocamllsp", "ocaml-lsp-server"],
        "manifests": ["dune-project"],
        "extensions": [".ml", ".mli"],
        "language_id": "ocaml",
        "args": ["--stdio"]
    },
    "terraform": {
        "binaries": ["terraform-ls", "terraform-lsp"],
        "manifests": ["main.tf", ".terraform"],
        "extensions": [".tf"],
        "language_id": "terraform",
        "args": ["serve"]
    },
    "dockerfile": {
        "binaries": ["dockerfile-language-server", "dockerfile-ls"],
        "manifests": ["Dockerfile"],
        "extensions": ["Dockerfile", ".dockerfile"],
        "language_id": "dockerfile",
        "args": ["--stdio"]
    },
    "yaml": {
        "binaries": ["yaml-language-server"],
        "manifests": ["k8s.yaml", ".yamllint"],
        "extensions": [".yaml", ".yml"],
        "language_id": "yaml",
        "args": ["--stdio"]
    },
    "ansible": {
        "binaries": ["ansible-language-server"],
        "manifests": ["ansible.cfg", "playbooks"],
        "extensions": [".yml", ".yaml"],
        "language_id": "ansible",
        "args": ["--stdio"]
    },
    "pulumi": {
        "binaries": ["pulumi-lsp"],
        "manifests": ["Pulumi.yaml"],
        "extensions": [".yaml", ".yml"],
        "language_id": "pulumi",
        "args": ["--stdio"]
    },
    "python": {
        "binaries": ["pyright-langserver", "pyright", "pylsp", "python-lsp-server", "jedi-language-server"],
        "manifests": ["pyproject.toml", "requirements.txt", "setup.py"],
        "extensions": [".py"],
        "language_id": "python",
        "args": ["--stdio"]
    },
    "r": {
        "binaries": ["R"],
        "manifests": ["description", ".Rprofile"],
        "extensions": [".R", ".r"],
        "language_id": "r",
        "args": ["--slave", "-e", "languageserver::run()"]
    },
    "julia": {
        "binaries": ["julia"],
        "manifests": ["Project.toml"],
        "extensions": [".jl"],
        "language_id": "julia",
        "args": ["--startup-file=no", "--history-file=no", "-e", "using LanguageServer; run()"]
    },
    "lua": {
        "binaries": ["lua-language-server"],
        "manifests": [".luacheckrc", "stylua.toml"],
        "extensions": [".lua"],
        "language_id": "lua",
        "args": ["--stdio"]
    },
    "sql": {
        "binaries": ["sqls", "sql-language-server"],
        "manifests": [".sqls.yml"],
        "extensions": [".sql", ".cypher"],
        "language_id": "sql",
        "args": ["--stdio"]
    },
    "bash": {
        "binaries": ["bash-language-server"],
        "manifests": [".sh", ".bash"],
        "extensions": [".sh", ".bash"],
        "language_id": "bash",
        "args": ["start"]
    },
    "perl": {
        "binaries": ["perl-navigator", "pls"],
        "manifests": ["cpanfile", "Makefile.PL"],
        "extensions": [".pl", ".pm", ".t"],
        "language_id": "perl",
        "args": ["--stdio"]
    },
    "php": {
        "binaries": ["intelephense", "php-actor"],
        "manifests": ["composer.json"],
        "extensions": [".php"],
        "language_id": "php",
        "args": ["--stdio"]
    },
    "xml": {
        "binaries": ["lemminx"],
        "manifests": ["pom.xml"],
        "extensions": [".xml"],
        "language_id": "xml",
        "args": ["--stdio"]
    },
    "json": {
        "binaries": ["json-languageserver", "vscode-json-language-server"],
        "manifests": ["package.json"],
        "extensions": [".json"],
        "language_id": "json",
        "args": ["--stdio"]
    },
    "markdown": {
        "binaries": ["marksman", "markdown-oxide"],
        "manifests": ["README.md"],
        "extensions": [".md", ".markdown"],
        "language_id": "markdown",
        "args": ["server"]
    },
    "nix": {
        "binaries": ["nil", "rnix-lsp"],
        "manifests": ["flake.nix", "default.nix"],
        "extensions": [".nix"],
        "language_id": "nix",
        "args": ["--stdio"]
    }
}

try:
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    if os.path.exists(config_path):
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            custom_servers = config_data.get("lsp_servers")
            if isinstance(custom_servers, dict):
                for lang, spec in custom_servers.items():
                    if isinstance(spec, dict):
                        SUPPORTED_ECOSYSTEMS[lang] = {
                            "binaries": spec.get("binaries", []),
                            "manifests": spec.get("manifests", []),
                            "extensions": spec.get("extensions", []),
                            "language_id": spec.get("language_id", lang),
                            "args": spec.get("args", ["--stdio"])
                        }
except Exception:
    pass

