import subprocess
from dataclasses import dataclass
from pathlib import Path

import cappa
from rich import print as rich_print

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


class Hook(StrEnum):
    PRE_BUILD = "pre_build"
    PRE_DEPLOY = "pre_deploy"
    POST_DEPLOY = "post_deploy"


HooksDict = dict[Hook, str]


@dataclass(slots=True)
class HookManager:
    app_name: str
    hooks: HooksDict
    local_config_dir: Path

    def __post_init__(self):
        if self.hooks:
            return
        hooks_folder = self.local_config_dir / "hooks"
        if not hooks_folder.exists():
            return
        self.hooks = {
            h.value: f"./{hooks_folder / h.value}"  # noqa
            for h in Hook
            if (hooks_folder / h.value).exists()  # noqa
        }

    def _run_hook(self, type_: Hook) -> None:
        if cmd := self.hooks.get(type_):
            rich_print(f"[blue]Running {type_} hook[/blue]")
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                raise cappa.Exit(result.stderr)
            rich_print(result.stdout)

    def pre_build(self) -> None:
        self._run_hook(Hook.PRE_BUILD)

    def pre_deploy(self) -> None:
        self._run_hook(Hook.PRE_DEPLOY)

    def post_deploy(self) -> None:
        self._run_hook(Hook.POST_DEPLOY)
