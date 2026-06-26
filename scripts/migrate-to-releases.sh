#!/usr/bin/env bash
# Migration script: convert fujin-deployed apps from the old .install/ structure
# to the new release-based atomic deployment structure.
#
# Run on the server for each deployed app:
#   sudo bash migrate-to-releases.sh myapp
#
# What it does:
#   1. Reads current version from .install/.version
#   2. Creates releases/ and shared/ directories
#   3. Moves .install/ contents into releases/{version}/
#   4. Moves .env to shared/
#   5. Creates current -> releases/{version} symlink
#   6. Moves .versions/ to app_dir root
#   7. Updates systemd unit files to reference current/ and shared/
#   8. Reloads systemd and restarts services

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <app_name>"
    echo "Example: $0 myapp"
    exit 1
fi

APP_NAME="$1"
APP_DIR="/opt/fujin/${APP_NAME}"
INSTALL_DIR="${APP_DIR}/.install"

if [ ! -d "$INSTALL_DIR" ]; then
    echo "Error: ${INSTALL_DIR} not found. Is this app deployed with fujin?"
    exit 1
fi

if [ -d "${APP_DIR}/releases" ] || [ -L "${APP_DIR}/current" ]; then
    echo "Error: ${APP_DIR} already appears to be using the new structure (releases/ or current exists)."
    echo "Migration has already been applied or the app was deployed with a newer fujin version."
    exit 1
fi

# Read current version
CURRENT_VERSION=$(cat "${INSTALL_DIR}/.version" 2>/dev/null || echo "")
if [ -z "$CURRENT_VERSION" ]; then
    echo "Error: Could not read version from ${INSTALL_DIR}/.version"
    exit 1
fi
echo "Migrating ${APP_NAME} (version ${CURRENT_VERSION})..."

# Create new directories
RELEASE_DIR="${APP_DIR}/releases/${CURRENT_VERSION}"
SHARED_DIR="${APP_DIR}/shared"

mkdir -p "${APP_DIR}/releases"
mkdir -p "$SHARED_DIR"

# Stop services before migration
echo "Stopping services..."
systemctl stop "${APP_NAME}-*.service" 2>/dev/null || true

# Move .env to shared/ (if it exists and is not already there)
if [ -f "${INSTALL_DIR}/.env" ]; then
    echo "Moving .env to shared/"
    cp "${INSTALL_DIR}/.env" "${SHARED_DIR}/.env"
    chmod 640 "${SHARED_DIR}/.env"
fi

# Move .install/ contents to releases/{version}/
echo "Moving .install/ to releases/${CURRENT_VERSION}/"
if [ -d "$RELEASE_DIR" ]; then
    echo "Warning: ${RELEASE_DIR} already exists, removing..."
    rm -rf "$RELEASE_DIR"
fi
mv "$INSTALL_DIR" "$RELEASE_DIR"

# Remove old .versions/ directory if it was inside .install/
if [ -d "${RELEASE_DIR}/.versions" ]; then
    echo "Removing old .versions/ directory (no longer needed)..."
    rm -rf "${RELEASE_DIR}/.versions"
fi

# Create current symlink
echo "Creating current -> releases/${CURRENT_VERSION} symlink..."
ln -sfn "releases/${CURRENT_VERSION}" "${APP_DIR}/current"

# Update .appenv to source from shared/.env instead of release/.env or install/.env
APPENV="${RELEASE_DIR}/.appenv"
if [ -f "$APPENV" ]; then
    echo "Updating .appenv to reference shared/.env..."
    sed -i "s|source ${INSTALL_DIR}/.env|source ${SHARED_DIR}/.env|g" "$APPENV"
    sed -i "s|source ${RELEASE_DIR}/.env|source ${SHARED_DIR}/.env|g" "$APPENV"
fi

# Update systemd unit files to use new paths
echo "Updating systemd unit files..."
for unit_file in /etc/systemd/system/"${APP_NAME}"*.service; do
    if [ -f "$unit_file" ]; then
        echo "  Updating ${unit_file}..."
        # Replace .install/ paths with current/ and shared/
        sed -i "s|${APP_DIR}/\.install/\.venv|${APP_DIR}/current/.venv|g" "$unit_file"
        sed -i "s|${APP_DIR}/\.install/\([^/]\)|${APP_DIR}/current/\1|g" "$unit_file"
        sed -i "s|EnvironmentFile=${APP_DIR}/\.install/\.env|EnvironmentFile=${SHARED_DIR}/.env|g" "$unit_file"
        sed -i "s|EnvironmentFile=-${APP_DIR}/\.install/\.env|EnvironmentFile=-${SHARED_DIR}/.env|g" "$unit_file"
    fi
done

for socket_file in /etc/systemd/system/"${APP_NAME}"*.socket; do
    if [ -f "$socket_file" ]; then
        echo "  Updating ${socket_file}..."
        sed -i "s|${APP_DIR}/\.install/|${APP_DIR}/current/|g" "$socket_file"
    fi
done

# Reload systemd
echo "Reloading systemd..."
systemctl daemon-reload

# Restart services
echo "Starting services..."
systemctl restart "${APP_NAME}-*.service" 2>/dev/null || systemctl start "${APP_NAME}-*.service" 2>/dev/null || true

echo ""
echo "Migration complete!"
echo "  App directory: ${APP_DIR}"
echo "  Current version: ${CURRENT_VERSION}"
echo "  New structure:"
echo "    ${APP_DIR}/current -> releases/${CURRENT_VERSION}/"
echo "    ${APP_DIR}/releases/${CURRENT_VERSION}/"
echo "    ${APP_DIR}/shared/.env"
echo ""
echo "  Verify with: systemctl status ${APP_NAME}-*.service"
echo "  Next deploy will use the new structure automatically."
