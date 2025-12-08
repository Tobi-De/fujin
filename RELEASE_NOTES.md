# Fujin Release Highlights

- **BREAKING**: Deployment now uses a bundle format (not just distfile). Fujin produces a full bundle containing the distfile, systemd units, scripts, and metadata. The server expects this bundle; old distfile-only deploys are incompatible.
    - Example: `fujin deploy` now uploads a tarball bundle, not just your app binary/wheel.
    - Bundle contents: `distfile`, systemd unit files, install/uninstall scripts, version info, etc.
    - Rollbacks and teardown (`fujin down`) now operate on bundles.

-  SSH backend switched from Paramiko to a custom implementation using `ssh2-python` for faster, more reliable connections.

- `fujin app start|stop|restart|logs <name>` now resolves all related systemd units (e.g., `web` includes `web.service`, `web.socket`, `web.timer` if present).
    - Example: `fujin app start web` starts both service and socket.
    - Example: `fujin app logs worker` shows logs for service and timer.
    - To target only one unit: `fujin app start web.service`

- New: `fujin app cat <name>` shows the content of systemd unit files on the server.
    - Example: `fujin app cat web` shows both service and socket files if present.

- You can pass custom variables to templates via `context` in `fujin.toml`:
    - Example:
      ```toml
      [processes.web]
      command = "uvicorn app:app"
      context = { port = "8000", env = "prod" }
      ```
      Use in template: `{{ context.port }}`

- Error messages for unknown services now list all valid options, including sockets/timers if configured.

