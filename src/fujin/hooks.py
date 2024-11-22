from dataclasses import dataclass

from rich import print as rich_print

from fujin.connection import Connection

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


class Hook(StrEnum):
    PRE_DEPLOY = "pre_deploy"
    POST_DEPLOY = "post_deploy"
    PRE_BOOTSTRAP = "pre_bootstrap"
    POST_BOOTSTRAP = "post_bootstrap"
    PRE_TEARDOWN = "pre_teardown"
    POST_TEARDOWN = "post_teardown"


HooksDict = dict[Hook, dict]


@dataclass(frozen=True, slots=True)
class HookManager:
    app_name: str
    hooks: HooksDict
    conn: Connection

    def _run_hook(self, type_: Hook) -> None:
        if hooks := self.hooks.get(type_):
            for name, command in hooks.items():
                rich_print(f"[blue]Running {type_} hook {name} [/blue]")
                self.conn.run(command, pty=True)

    def pre_deploy(self) -> None:
        self._run_hook(Hook.PRE_DEPLOY)

    def post_deploy(self) -> None:
        self._run_hook(Hook.POST_DEPLOY)

    def pre_bootstrap(self) -> None:
        self._run_hook(Hook.PRE_BOOTSTRAP)

    def post_bootstrap(self) -> None:
        self._run_hook(Hook.POST_BOOTSTRAP)

    def pre_teardown(self) -> None:
        self._run_hook(Hook.PRE_TEARDOWN)

    def post_teardown(self) -> None:
        self._run_hook(Hook.POST_TEARDOWN)
