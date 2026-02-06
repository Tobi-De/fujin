from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import cappa

from fujin.commands import BaseCommand


@cappa.command(
    help="Launch the web UI dashboard for your application",
)
@dataclass
class Ui(BaseCommand):
    port: Annotated[
        int,
        cappa.Arg(
            short="-p",
            long="--port",
            help="Port to serve the UI on",
        ),
    ] = 8642

    def __call__(self):
        try:
            import uvicorn
        except ImportError:
            self.output.error(
                "The 'ui' command requires extra dependencies.\n"
                "Install them with: uv pip install starlette uvicorn sse-starlette"
            )
            raise cappa.Exit(code=1) from None

        from fujin.web import create_app

        app = create_app(config=self.config, host_name=self.host)

        self.output.info(f"Starting Fujin UI for [bold]{self.config.app_name}[/bold]")
        self.output.output(f"  → http://localhost:{self.port}")
        self.output.output(
            f"  → Host: {self.selected_host.user}@{self.selected_host.address}"
        )
        print()

        uvicorn.run(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
