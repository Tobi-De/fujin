# fujin

> [!IMPORTANT]
> This tool is currently contains minimal features and is a work-in-progress

<!-- content:start -->

`fujin` is a simple deployment tool that helps you get your project up and running on a VPS in minutes. It manages your app processes using [systemd](https://systemd.io) and runs your apps behind [caddy](https://caddyserver.com).

[![Publish Package](https://github.com/falcopackages/fujin/actions/workflows/publish.yml/badge.svg)](https://github.com/falcopackages/fujin/actions/workflows/publish.yml)
[![PyPI - Version](https://img.shields.io/pypi/v/fujin-cli.svg)](https://pypi.org/project/fujin-cli)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/fujin-cli.svg)](https://pypi.org/project/fujin-cli)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/falcopackages/fujin/blob/main/LICENSE.txt)
[![Status](https://img.shields.io/pypi/status/fujin-cli.svg)](https://pypi.org/project/fujin-cli)

## Features

- 🚀 One-command server bootstrap
- 🔄 Rollback broken deployments
- 🔐 Zero configuration SSL certificates
- 🔁 Swappable proxy ([caddy](https://caddyserver.com), [nginx](https://nginx.org/en/) and `dummy` to disable proxy)
- 🛠️ Secrets injection from password managers ([Bitwarden](https://bitwarden.com/), [1Password](https://1password.com), etc.)
- 📝 Easily customizable `systemd` and `proxy` configurations
- 👨‍💻 Remote application management and log streaming
- 🐍 Supports packaged python apps and self-contained binaries

For more details, check out the [documentation📚](https://fujin.oluwatobi.dev/en/latest/).

## Why?

I wanted [kamal](https://kamal-deploy.org/) but without Docker, and I thought the idea was fun. At its core, this project automates versions of this [tutorial](https://www.digitalocean.com/community/tutorials/how-to-set-up-django-with-postgres-nginx-and-gunicorn-on-ubuntu). If you've been a Django beginner 
trying to get your app in production, you probably went through this.

I'm using `caddy` here by default instead of `nginx` because it's configurable via an API and it's is a no-brainer for SSL certificates. `Systemd` is the default on most Linux distributions and does a good enough job.

Fujin was initially planned to be a Python-only project, but the core concepts can be applied to any language that can produce a single distributable file (e.g., Go, Rust).

The goal is to automate deployment while leaving you in full control of your Linux box. It's not a CLI PaaS - it's simple and expects you to be able to SSH into your server and troubleshoot if necessary. For beginners, it makes the initial deployment easier while you get your hands dirty with Linux.
If you need a never-break, worry-free, set-it-and-forget-it setup that auto-scales and does all the magic, fujin probably isn't for you.

## Inspiration and alternatives

Fujin draws inspiration from the following tools for their developer experience. These are better alternatives if you need a more robust, set-and-forget solution

- [fly.io](https://fly.io/)
- [kamal](https://kamal-deploy.org/) (you probably can't just forget this one)

## License

`fujin` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.

<!-- content:end -->
