# fujin

[![PyPI - Version](https://img.shields.io/pypi/v/fujin-cli.svg)](https://pypi.org/project/fujin-cli)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/fujin-cli.svg)](https://pypi.org/project/fujin-cli)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/falcopackages/fujin/blob/main/LICENSE.txt)
[![Status](https://img.shields.io/pypi/status/fujin-cli.svg)](https://pypi.org/project/fujin-cli)
-----

> [!IMPORTANT]
> This package currently contains minimal features and is a work-in-progress

<!-- content:start -->

`fujin` is a simple deployment tool that helps you get your project up and running on a VPS in a few minutes. It manages your app processes using `systemd` and runs your apps behind [caddy](https://caddyserver.com/). For Python projects, 
it expects your app to be a packaged Python application ideally with a CLI entry point defined. For other languages, you need to provide a self-contained single executable file with all necessary dependencies.
The main job of `fujin` is to bootstrap your server (installing caddy, etc.), copy the files onto the server with a structure that supports rollback, and automatically generate configs for systemd and caddy that you can manually edit if needed.

Check out the [documentation📚](https://fujin.oluwatobi.dev/en/latest/) for installation, features, and usage guides.

## Why?

I wanted [kamal](https://kamal-deploy.org/) but without Docker, and I thought the idea was fun. At its core, this project automates versions of this [tutorial](https://www.digitalocean.com/community/tutorials/how-to-set-up-django-with-postgres-nginx-and-gunicorn-on-ubuntu). If you've been a Django beginner 
trying to get your app in production, you probably went through this. I'm using caddy instead of nginx because it's configurable via an API and it's is a no-brainer for SSL certificates. Systemd is the default on most Linux distributions and does a good enough job.

Fujin was initially planned to be a Python-only project, but the core concepts can be applied to any language that can produce a single distributable file (e.g., Go, Rust). I wanted to recreate kamal's nice local-to-remote app management API, but I'm skipping Docker to keep things simple. 
I'm currently rocking SQLite in production for my side projects and ths setup is enough for my use case.

The goal is to automate deployment while leaving you in full control of your Linux box. It's not a CLI PaaS - it's simple and expects you to be able to SSH into your server and troubleshoot if necessary. For beginners, it makes the initial deployment easier while you get your hands dirty with Linux.
If you need a never-break, worry-free, set-it-and-forget-it setup that auto-scales and does all the magic, fujin probably isn't for you.

## Inspiration and alternatives

Fujin draws inspiration from the following tools for their developer experience. These are better alternatives if you need a more robust, set-and-forget solution

- [fly.io](https://fly.io/)
- [kamal](https://kamal-deploy.org/) (you probably can't just forget this one)

## License

`fujin` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.

<!-- content:end -->