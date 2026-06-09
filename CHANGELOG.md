# Changelog

All notable changes to this project will be documented in this file.

This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.26.1] - 2026-06-09

### 🐛 Bug Fixes

- *(deploy)* Skip reload-or-restart for Type=oneshot services

## [0.26.0] - 2026-06-05

### 🚀 Features

- Hooks system
- Improve reliability

### ⚡ Performance

- Ssh and deploy units cache

## [0.25.2] - 2026-03-31

### 🚀 Features

- Bash function to edit source

## [0.25.1] - 2026-03-21

### 🐛 Bug Fixes

- Restart on env change failing for first deployment

## [0.25.0] - 2026-03-21

### 🚀 Features

- Added restart-on-env-change deploy flag

## [0.24.0] - 2026-03-21

### 🚀 Features

- Add env secrets adapter and refactor adapter config

### ⚙️ Miscellaneous Tasks

- Add fujin-secrets-env to publish workflow

## [0.23.0] - 2026-03-21

### 🚀 Features

- Stream .env directly to server instead of bundling
- Add --bundle-dir option to preserve deployment bundle

## [0.22.2] - 2026-03-21

### 🐛 Bug Fixes

- Handle pre-existing group when creating app user

## [0.22.1] - 2026-03-21

### 🐛 Bug Fixes

- Use native ssh auth first when needed

## [0.22.0] - 2026-03-20

### 🚀 Features

- Ssh keys management
- Try system ssh interactive auth if using agent fails

### 🐛 Bug Fixes

- Falco templates bad config

## [0.21.4] - 2026-02-14

### 🚀 Features

- Add caddy to www-data group for convenience
- Improve falco templates

### 🚜 Refactor

- Simplify  command with positional kind argument
- Removing caddy group add

## [0.21.3] - 2026-02-12

### 🐛 Bug Fixes

- Rsync upload was triggering on >30k instead of 30MB

## [0.21.2] - 2026-02-08

### 🚀 Features

- Use verbose parameter during rollback

### 🐛 Bug Fixes

- Requirements hash debug logging

## [0.21.1] - 2026-02-08

### 🚀 Features

- Add --strict flag to rollback and combine version queries into single SSH call
- Log requirements hash in verbose mode

### 🐛 Bug Fixes

- Fail deploy with rollback
- Use is-failed instead of is-active for post-deploy service checks

## [0.21.0] - 2026-02-06

### 🚀 Features

- Ignore service file starting with _
- Better logging infrastructure
- *(deploy)* Add --no-rollback flag to disable automatic rollback
- *(deploy)* Add --full-restart flag for forced service restarts
- *(app)* Allow multiple service names for start/stop/restart/logs/status
- App command can now receive multiple names
- Added short name for verbose option

### 🐛 Bug Fixes

- *(deploy)* Always remove failed bundle even on Ctrl+C during rollback
- Improve SCP upload error message with actionable hints

### ⚡ Performance

- Improve installation speed

## [0.20.9] - 2026-02-03

### 🚀 Features

- *(deploy)* Add service unit names to template context
- *(deploy)* Add service unit names to template context
- *(app)* Add --force flag to restart command

## [0.20.7] - 2026-02-03

### 🚀 Features

- App command run reload-or-restart

## [0.20.6] - 2026-02-02

### 🚀 Features

- Improve installer to work with services supporting reload

## [0.20.5] - 2026-02-01

### 🐛 Bug Fixes

- Fallback from rsync to scp even in case of upoad error

## [0.20.4] - 2026-02-01

### 🐛 Bug Fixes

- Fallback on put when rsync unavailable

## [0.20.3] - 2026-02-01

### 🐛 Bug Fixes

- Previous bundle corruption due to hardlink

## [0.20.2] - 2026-02-01

### 🚀 Features

- Improve upload speed for larger file using rsync

### 🐛 Bug Fixes

- Reload caddy on timenout to avoid cli hanging

### Deps

- Upgrade dependencies

## [0.20.1] - 2026-01-31

### 🚀 Features

- Delete broken bundle after rollback

## [0.20.0] - 2026-01-31

### 🚀 Features

- Improve installer debug output
- Improve rollback prompt
- Append git hash to deployed version
- Auto rollback on app restart failure
- Added git based versionning as the default

### 🚜 Refactor

- Rely more on python tools in _installer
- Rename installer script

## [0.19.5] - 2026-01-25

### 🚀 Features

- Installer helpers act on all instances of replicas

## [0.19.4] - 2026-01-24

### 🚀 Features

- Added back showenv command
- Added app helpers function in .appenv
- Improve installer script to use uv full path

## [0.19.3] - 2026-01-22

### 🚀 Features

- Improve falco templates
- Added server upgrade command

### 📚 Documentation

- Update with server upgrade

## [0.19.2] - 2026-01-22

### 🐛 Bug Fixes

- Pass host correctly to all commands

## [0.19.1] - 2026-01-21

### 🐛 Bug Fixes

- Server exec

## [0.19.0] - 2026-01-20

### 🚀 Features

- Scale now run apply to the server
- Add caddy to user group during deployment

### 🚜 Refactor

- Rename fj alt cli to fa (fujin app)
- Split back exec to app exec and server exec

## [0.18.0] - 2026-01-18

### 🚀 Features

- Add fj entry point as shortcut to fujin app
- Show warning on unresolved variables and remove useless cmds
- Fetch logs on services failing to start

### 🚜 Refactor

- Rename info command to status
- Remove bundle upload retry
- Unify systemd units discovery
- Rewrite to use systemd units and Caddyfile directly

### 🧪 Testing

- Fix integration tests

## [0.17.2] - 2026-01-11

### 🐛 Bug Fixes

- Broken generated config with / instead /*

## [0.17.1] - 2026-01-10

### 🐛 Bug Fixes

- Creating backup without --backup option

## [0.17.0] - 2026-01-09

### 🚀 Features

- Added migrate command
- Rewrite config for better webserver flexibility

## [0.16.0] - 2025-12-28

### 🚀 Features

- Rework timer config with more options
- Added operation logging and deployment history
- Added multi server support
- Added show command in favor or printenv
- Improved error messages
- Added ssh-setup helper
- Logs filtering options
- Migrate to zipapp

### 🐛 Bug Fixes

- Failed auth when key not in agent

### 🚜 Refactor

- Move third party plugins into separate packages
- Move templates eject into it own command
- Move custom context from process to host
- Merge app exec and server exec into exec command
- Consistent styling and help messages improvements

### 📚 Documentation

- Update documentation de reflect latest changes

### 🎨 Styling

- Deployment summary

### 🧪 Testing

- Rewrote from scratch

## [0.14.1] - 2025-12-08

### 🚀 Features

- Added special case to print caddy config

## [0.13.2] - 2025-11-29

### 🐛 Bug Fixes

- Hanging interactive shell on 3.14

## [0.13.1] - 2025-11-28

### 🐛 Bug Fixes

- Broken cli because of missing gevent

## [0.13.0] - 2025-11-27

### 🚀 Features

- [**breaking**] Refactor to ejectable defaults, enhanced config, and new docs

## [0.12.2] - 2025-11-16

### 🐛 Bug Fixes

- Dummy proxy

### 🚜 Refactor

- Use custom caddy server name
- Less files

## [0.12.1] - 2025-03-12

### 🚀 Features

- Add process name to systemd service
- Add fujin version info
- Set system as the default secrets adapter
- Add system secret reader

### 🐛 Bug Fixes

- Force .venv removal on deploy
- Env content parse logic

### 🚜 Refactor

- Rename env_content to env

### 📚 Documentation

- Document integration with ci ci platforms
- Apply a more consistent writing style

### ⚙️ Miscellaneous Tasks

- Specify source package to avoid failing build backend

## [0.10.0] - 2024-11-24

### 🚀 Features

- Add doppler support to secrets

## [0.9.1] - 2024-11-23

### 🚜 Refactor

- Drop configurable proxy manager

### 📚 Documentation

- Add links to template systemd service files

### ⚡ Performance

- Run systemd commands concurrently using gevent

## [0.9.0] - 2024-11-23

### 🚀 Features

- Env content can be define directly in toml

### 🚜 Refactor

- Avoid running secret adapter if no secret placeholder is found

## [0.8.0] - 2024-11-23

### 🚀 Features

- Rewrite hooks (#30)

## [0.7.1] - 2024-11-23

### 🐛 Bug Fixes

- Broken .venv folder can fail deploy

## [0.7.0] - 2024-11-22

### 🚀 Features

- Inject secrets via bitwarden and 1password (#29)
- Add certbot_email configuration for nginx

### 🚜 Refactor

- Move requirements copy to transfer_files

## [0.6.0] - 2024-11-19

<!-- generated by git-cliff -->
