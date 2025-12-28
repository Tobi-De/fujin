#!/bin/bash
# Generate terminal screenshots for all fujin command help messages
# Requires: termshot (cargo install termshot)

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

    # Use termshot with proper syntax
    termshot --no-decoration --filename "$output" -- "uv run $command"

    echo "   ✓ Saved to $output"
}

# Main command
generate_screenshot "fujin-help" "fujin --help"

# Primary commands
generate_screenshot "init-help" "fujin init --help"
generate_screenshot "up-help" "fujin up --help"
generate_screenshot "deploy-help" "fujin deploy --help"
generate_screenshot "down-help" "fujin down --help"
generate_screenshot "rollback-help" "fujin rollback --help"
generate_screenshot "prune-help" "fujin prune --help"

# App command and subcommands
generate_screenshot "app-help" "fujin app --help"
generate_screenshot "app-info-help" "fujin app info --help"
generate_screenshot "app-start-help" "fujin app start --help"
generate_screenshot "app-stop-help" "fujin app stop --help"
generate_screenshot "app-restart-help" "fujin app restart --help"
generate_screenshot "app-logs-help" "fujin app logs --help"
generate_screenshot "app-shell-help" "fujin app shell --help"
generate_screenshot "app-cat-help" "fujin app cat --help"
generate_screenshot "app-history-help" "fujin app history --help"

# Server command and subcommands
generate_screenshot "server-help" "fujin server --help"
generate_screenshot "server-info-help" "fujin server info --help"
generate_screenshot "server-bootstrap-help" "fujin server bootstrap --help"
generate_screenshot "server-create-user-help" "fujin server create-user --help"
generate_screenshot "server-setup-ssh-help" "fujin server setup-ssh --help"

# New/undocumented commands
generate_screenshot "show-help" "fujin show --help"
generate_screenshot "audit-help" "fujin audit --help"
generate_screenshot "exec-help" "fujin exec --help"

# Utility commands
generate_screenshot "docs-help" "fujin docs --help"

echo ""
echo "✅ All help screenshots generated successfully!"
echo ""
echo "Screenshots saved in: $OUTPUT_DIR"
echo ""
echo "Next steps:"
echo "  1. Review generated images"
echo "  2. Update .rst files to use images"
echo "  3. Add to git: git add $OUTPUT_DIR"
