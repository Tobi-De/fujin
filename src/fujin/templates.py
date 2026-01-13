from __future__ import annotations

# Systemd Templates for user-created services (fujin new service)
NEW_SERVICE_TEMPLATE = """# Systemd service for {name}
# Learn more: https://www.freedesktop.org/software/systemd/man/systemd.service.html

[Unit]
Description={{app_name}} {name}
After=network.target
# Add dependencies if needed:
# Requires=postgresql.service
# After=postgresql.service

[Service]
Type=simple
User={{user}}
WorkingDirectory={{app_dir}}
EnvironmentFile={{app_dir}}/.env

# Main command - adjust to match your application
ExecStart={{app_dir}}/.venv/bin/python -m myapp.{name}

# Restart policy
Restart=on-failure
RestartSec=5s

# Resource limits (uncomment to enable)
# MemoryMax=512M
# CPUQuota=50%

# Security hardening (uncomment to enable)
# NoNewPrivileges=true
# PrivateTmp=true
# ProtectSystem=strict
# ProtectHome=true

[Install]
WantedBy=multi-user.target
"""


# Template for user-created timers (fujin new timer)
NEW_TIMER_SERVICE_TEMPLATE = """# Systemd service for {name} (scheduled task)
# Learn more: https://www.freedesktop.org/software/systemd/man/systemd.service.html

[Unit]
Description={{app_name}} {name} task

[Service]
Type=oneshot
User={{user}}
WorkingDirectory={{app_dir}}
EnvironmentFile={{app_dir}}/.env

# Main command - this runs when triggered by the timer
ExecStart={{app_dir}}/.venv/bin/python -m myapp.{name}

# Resource limits (uncomment to enable)
# MemoryMax=512M
# CPUQuota=50%
"""

NEW_TIMER_TEMPLATE = """# Systemd timer for {name}
# Learn more: https://www.freedesktop.org/software/systemd/man/systemd.timer.html

[Unit]
Description={{app_name}} {name} timer

[Timer]
# Schedule - runs once per day at midnight
OnCalendar=daily

# Alternative schedule examples:
# OnCalendar=hourly          # Every hour
# OnCalendar=weekly          # Every Monday at 00:00
# OnCalendar=*-*-* 04:00:00  # Every day at 4am
# OnCalendar=Mon *-*-* 12:00:00  # Every Monday at noon

# Run on boot if missed while system was off
Persistent=true

# Delay after boot (alternative to OnCalendar)
# OnBootSec=15min

# Run X time after the service last finished
# OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
"""

# Template for user-created dropins (fujin new dropin)
NEW_DROPIN_TEMPLATE = """# Systemd dropin configuration
# Learn more: https://www.freedesktop.org/software/systemd/man/systemd.unit.html
# Dropins allow you to override or extend service settings without modifying the original file

[Service]
# Resource Limits
# MemoryMax=512M          # Maximum memory usage
# MemoryHigh=384M         # Soft memory limit (triggers throttling)
# CPUQuota=50%            # Limit to 50% of one CPU core
# TasksMax=100            # Maximum number of processes/threads

# Security Hardening
# NoNewPrivileges=true    # Prevent privilege escalation
# PrivateTmp=true         # Isolated /tmp directory
# ProtectSystem=strict    # Read-only /usr, /boot, /efi
# ProtectHome=true        # Make /home inaccessible
# ProtectKernelTunables=true
# ProtectControlGroups=true
# RestrictRealtime=true

# Network Restrictions
# PrivateNetwork=true     # Disable network access
# IPAddressDeny=any       # Block all IP addresses
# IPAddressAllow=127.0.0.1/8  # Allow localhost only

# File System Access
# ReadWritePaths=/var/lib/myapp  # Grant write access to specific paths
# ReadOnlyPaths=/etc/myapp       # Read-only access
# InaccessiblePaths=/home        # Hide specific paths

# More options: https://www.freedesktop.org/software/systemd/man/systemd.exec.html
"""

# Caddyfile Templates
CADDYFILE_HEADER = """# Caddyfile for {app_name}
# Learn more: https://caddyserver.com/docs/caddyfile

{domain} {{
"""

CADDY_HANDLE_STATIC = """
    # Serve static files
    handle {path} {{
        root * {root}
        file_server
    }}
"""

CADDY_HANDLE_PROXY = """
    # Proxy to {name} service
    handle {path} {{
{extra_directives}        reverse_proxy {upstream}
    }}
"""
