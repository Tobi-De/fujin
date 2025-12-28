import shlex
import sys
import logging
from pathlib import Path
from typing import Annotated

import cappa
import fujin

from fujin.commands.app import App
from fujin.commands.config import ConfigCMD
from fujin.commands.deploy import Deploy
from fujin.commands.docs import Docs
from fujin.commands.down import Down
from fujin.commands.init import Init
from fujin.commands.prune import Prune
from fujin.commands.rollback import Rollback
from fujin.commands.server import Server
from fujin.commands.up import Up
from fujin.commands.printenv import Printenv

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _setup_logging(verbose):
    verbose = int(verbose)
    if verbose == 0:
        level = logging.WARN
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(message)s")
    logging.getLogger("markdown_it").setLevel(logging.WARNING)
    return verbose


@cappa.command(help="Deployment of python web apps in a breeze :)")
class Fujin:
    subcommands: cappa.Subcommands[
        Init
        | Up
        | Deploy
        | App
        | Server
        | ConfigCMD
        | Docs
        | Down
        | Rollback
        | Prune
        | Printenv
    ]
    verbose: Annotated[
        int,
        cappa.Arg(
            short=None,
            long="--verbose",
            help="Enable verbose logging",
            action=_setup_logging,
            choices=[0, 1, 2],
        ),
    ] = 0


def main():
    alias_cmd = _parse_aliases()
    if alias_cmd:
        cappa.invoke(Fujin, argv=alias_cmd, version=fujin.__version__)
    else:
        cappa.invoke(Fujin, version=fujin.__version__)


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
