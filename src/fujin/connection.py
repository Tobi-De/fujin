from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING

import cappa
from fabric import Connection
from invoke import Responder
from invoke.exceptions import UnexpectedExit
from paramiko.ssh_exception import AuthenticationException
from paramiko.ssh_exception import NoValidConnectionsError
from paramiko.ssh_exception import SSHException

if TYPE_CHECKING:
    from fujin.config import HostConfig


def _get_watchers(host: HostConfig) -> list[Responder]:
    if not host.password:
        return []
    return [
        Responder(
            pattern=r"\[sudo\] password:",
            response=f"{host.password}\n",
        ),
        Responder(
            pattern=rf"\[sudo\] password for {host.user}:",
            response=f"{host.password}\n",
        ),
    ]


@contextmanager
def host_connection(host: HostConfig) -> Connection:
    connect_kwargs = None
    if host.key_filename:
        connect_kwargs = {"key_filename": str(host.key_filename)}
    elif host.password:
        connect_kwargs = {"password": host.password}
    conn = Connection(
        host.ip,
        user=host.user,
        port=host.ssh_port,
        connect_kwargs=connect_kwargs,
    )
    try:
        conn.run = partial(
            conn.run,
            env={
                "PATH": f"/home/{host.user}/.cargo/bin:/home/{host.user}/.local/bin:$PATH"
            },
            watchers=_get_watchers(host),
        )
        yield conn
    except AuthenticationException as e:
        msg = f"Authentication failed for {host.user}@{host.ip} -p {host.ssh_port}.\n"
        if host.key_filename:
            msg += f"An SSH key was provided at {host.key_filename.resolve()}. Please verify its validity and correctness."
        elif host.password:
            msg += f"A password was provided through the environment variable {host.password_env}. Please ensure it is correct for the user {host.user}."
        else:
            msg += "No password or SSH key was provided. Ensure your current host has SSH access to the target host."
        raise cappa.Exit(msg, code=1) from e
    except (UnexpectedExit, NoValidConnectionsError) as e:
        raise cappa.Exit(str(e), code=1) from e
    except SSHException as e:
        raise cappa.Exit(
            f"{e}, possible causes: incorrect user, or either you or the server may be offline",
            code=1,
        ) from e
    finally:
        conn.close()
