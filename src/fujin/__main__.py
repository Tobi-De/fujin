from gevent import monkey

monkey.patch_all()

import shlex
import sys
from pathlib import Path

import cappa

from fujin.commands.app import App
from fujin.commands.config import ConfigCMD
from fujin.commands.deploy import Deploy
from fujin.commands.docs import Docs
from fujin.commands.down import Down
from fujin.commands.init import Init
from fujin.commands.proxy import Proxy
from fujin.commands.prune import Prune
from fujin.commands.redeploy import Redeploy
from fujin.commands.rollback import Rollback
from fujin.commands.server import Server
from fujin.commands.up import Up
from fujin.commands.printenv import Printenv

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@cappa.command(help="Deployment of python web apps in a breeze :)")
class Fujin:
    subcommands: cappa.Subcommands[
        Init
        | Up
        | Deploy
        | Redeploy
        | App
        | Server
        | Proxy
        | ConfigCMD
        | Docs
        | Down
        | Rollback
        | Prune
        | Printenv
    ]


def main():
    alias_cmd = _parse_aliases()
    if alias_cmd:
        cappa.invoke(Fujin, argv=alias_cmd)
    else:
        cappa.invoke(Fujin)


def _parse_aliases() -> list[str] | None:
    fujin_toml = Path("fujin.toml")
    if not fujin_toml.exists():
        return
    data = tomllib.loads(fujin_toml.read_text())
    aliases: dict[str, str] = data.get("aliases")
    if not aliases:
        return
    if len(sys.argv) == 1:
        return
    if sys.argv[1] not in aliases:
        return
    extra_args = sys.argv[2:] if len(sys.argv) > 2 else []
    aliased_cmd = aliases.get(sys.argv[1])
    subcommand, args = aliased_cmd.split(" ", 1)
    return [subcommand, *extra_args, *shlex.split(args, posix=True)]


if __name__ == "__main__":
    main()
