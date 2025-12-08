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

@test *ARGS:
    uv run pytest --ignore=tests/integration -sv {{ ARGS }}

@test-integration *ARGS:
    uv run pytest tests/integration {{ ARGS }}

# Update inline snapshots
@test-fix:
    just test --inline-snapshot=fix

# Review inline snapshots
@test-review:
    just test --inline-snapshot=review

# -------------------------------------------------------------------------
# RELEASE UTILITIES
#---------------------------------------------------------------------------

# Generate changelog
@logchanges *ARGS:
    uvx git-cliff --output CHANGELOG.md {{ ARGS }}

# Bump project version and update changelog
bumpver VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    uvx bump-my-version bump {{ VERSION }}
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
