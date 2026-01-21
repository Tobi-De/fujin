set dotenv-load := true

# List all available commands
_default:
    @just --list --unsorted

# Run a command in the environment
run *ARGS:
    uv run {{ ARGS }}

# recreate vm
recreate-vm:
    vagrant destroy
    vagrant up

# SSH into vm
ssh:
    vagrant ssh

# Run uv command in the django example project
djuv *ARGS:
    #!/usr/bin/env bash
    cd examples/django/bookstore
    uv --project bookstore {{ ARGS }}

# Generate django project requirements:
dj-requirements:
    just djuv pip compile pyproject.toml -o requirements.txt

# Run fujin command in the django example project
fujin *ARGS:
    #!/usr/bin/env bash
    cd examples/django/bookstore
    ../../../.venv/bin/python -m fujin {{ ARGS }}

download-pocketbase:
    #!/usr/bin/env bash
    set -euo pipefail
    curl -L -o pocketbase_0.34.2_linux_arm64.zip "https://github.com/pocketbase/pocketbase/releases/download/v0.34.2/pocketbase_0.34.2_linux_amd64.zip"
    unzip pocketbase_0.34.2_linux_arm64.zip -d ./examples/golang/pocketbase/
    rm pocketbase_0.34.2_linux_arm64.zip
    rm ./examples/golang/pocketbase/LICENSE.md
    rm ./examples/golang/pocketbase/CHANGELOG.md
    chmod +x ./examples/golang/pocketbase/pocketbase

# -------------------------------------------------------------------------
# Maintenance
#---------------------------------------------------------------------------

@fmt:
    just --fmt --unstable
    uvx ruff format
    uvx prek run -a pyproject-fmt

@lint:
    uvx mypy .

@docs-serve:
    uv run --group docs sphinx-autobuild docs docs/_build/html --port 8002 --watch src/fujin

@docs-requirements:
    uv export --no-hashes --group docs --format requirements-txt > docs/requirements.txt

# Generate help screenshots for documentation (requires: cargo install termshot)
@docs-screenshots:
    #!/usr/bin/env bash
    set -e
    OUTPUT_DIR="docs/_static/images/help"
    mkdir -p "$OUTPUT_DIR"
    echo "Generating help screenshots for fujin documentation..."
    echo ""
    # Check if termshot is installed
    if ! command -v termshot &> /dev/null; then
        echo "❌ Error: termshot is not installed"
        echo ""
        echo "Install with: cargo install termshot"
        exit 1
    fi
    # Function to generate screenshot
    generate_screenshot() {
        local name="$1"
        local command="$2"
        local output="$OUTPUT_DIR/${name}.png"
        echo "📸 Generating ${name}.png..."
        termshot --no-decoration --no-shadow --filename "$output" -- "uv run $command"
        echo "   ✓ Saved to $output"
    }
    # Main command
    generate_screenshot "fujin-help" "fujin --help"
    # Primary commands
    generate_screenshot "init-help" "fujin init --help"
    generate_screenshot "new-help" "fujin new --help"
    generate_screenshot "up-help" "fujin up --help"
    generate_screenshot "deploy-help" "fujin deploy --help"
    generate_screenshot "down-help" "fujin down --help"
    generate_screenshot "rollback-help" "fujin rollback --help"
    generate_screenshot "prune-help" "fujin prune --help"
    generate_screenshot "migrate-help" "fujin migrate --help"
    generate_screenshot "audit-help" "fujin audit --help"
    # App command and subcommands
    generate_screenshot "app-help" "fujin app --help"
    generate_screenshot "app-status-help" "fujin app status --help"
    generate_screenshot "app-start-help" "fujin app start --help"
    generate_screenshot "app-stop-help" "fujin app stop --help"
    generate_screenshot "app-restart-help" "fujin app restart --help"
    generate_screenshot "app-logs-help" "fujin app logs --help"
    generate_screenshot "app-shell-help" "fujin app shell --help"
    generate_screenshot "app-cat-help" "fujin app cat --help"
    generate_screenshot "app-exec-help" "fujin app exec --help"
    generate_screenshot "app-scale-help" "fujin app scale --help"
    # Server command and subcommands
    generate_screenshot "server-help" "fujin server --help"
    generate_screenshot "server-status-help" "fujin server status --help"
    generate_screenshot "server-bootstrap-help" "fujin server bootstrap --help"
    generate_screenshot "server-create-user-help" "fujin server create-user --help"
    generate_screenshot "server-setup-ssh-help" "fujin server setup-ssh --help"
    generate_screenshot "server-exec-help" "fujin server exec --help"
    # Shortcut command
    generate_screenshot "fa-help" "fa --help"
    echo ""
    echo "✅ All help screenshots generated successfully!"
    echo ""
    echo "Screenshots saved in: $OUTPUT_DIR"
    echo ""
    echo "Next steps:"
    echo "  1. Review generated images"
    echo "  2. Verify .rst files reference correct images"
    echo "  3. Add to git: git add $OUTPUT_DIR"

@test *ARGS:
    uv run pytest --ignore=tests/integration -sv {{ ARGS }}

@test-integration *ARGS:
    uv run pytest tests/integration {{ ARGS }}

# -------------------------------------------------------------------------
# RELEASE UTILITIES
#---------------------------------------------------------------------------

# Generate changelog
@logchanges *ARGS:
    uvx git-cliff --output CHANGELOG.md {{ ARGS }}

# Sync plugin package versions to match core package version
@sync-plugin-versions:
    #!/usr/bin/env bash
    set -euo pipefail
    CORE_VERSION=$(grep '^version = ' pyproject.toml | head -1 | sed 's/version = "\(.*\)"/\1/')
    echo "Syncing plugin versions to $CORE_VERSION..."
    for plugin in bitwarden 1password doppler; do
        pyproject="plugins/fujin-secrets-${plugin}/pyproject.toml"
        if [ -f "$pyproject" ]; then
            echo "  - fujin-secrets-${plugin}"
            sed -i "s/^version = .*/version = \"$CORE_VERSION\"/" "$pyproject"
            sed -i "s/\"fujin-cli>=.*\"/\"fujin-cli>=$CORE_VERSION\"/" "$pyproject"
        fi
    done
    echo "Done!"

# Bump project version and update changelog
bumpver VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    uvx bump-my-version bump {{ VERSION }}
    just sync-plugin-versions
    just fmt || true
    just logchanges
    [ -z "$(git status --porcelain)" ] && { echo "No changes to commit."; git push && git push --tags; exit 0; }
    version="$(uvx bump-my-version show current_version)"
    git add -A
    git commit -m "Generate changelog for version ${version}"
    git tag -f "v${version}"
    git push && git push --tags

# Build a binary distribution of the project using pyapp
build-bin:
    #!/usr/bin/env bash
    current_version=$(uvx bump-my-version show current_version)
    uv build
    export PYAPP_UV_ENABLED="1"
    export PYAPP_PYTHON_VERSION="3.12"
    export PYAPP_FULL_ISOLATION="1"
    export PYAPP_EXPOSE_METADATA="1"
    export PYAPP_PROJECT_NAME="fujin"
    export PYAPP_PROJECT_VERSION="${current_version}"
    export PYAPP_PROJECT_PATH="${PWD}/dist/fujin_cli-${current_version}-py3-none-any.whl"
    export PYAPP_DISTRIBUTION_EMBED="1"
    export RUST_BACKTRACE="full"
    cargo install pyapp --force --root dist
    mv dist/bin/pyapp "dist/bin/fujin_cli-${current_version}"
