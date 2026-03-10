"""
cli/main.py

BandTracker command-line interface — command router.

Dispatches to subcommand handlers in cli/commands/.
Each handler registers itself via add_subparser() and
exposes a run(args) function that returns an exit code.
"""

from __future__ import annotations

import argparse
import sys

from cli.commands import init as cmd_init
from cli.commands import snapshot as cmd_snapshot
from cli.commands import learn_noise as cmd_learn_noise
from cli.commands import watch as cmd_watch
from cli.commands import reconcile as cmd_reconcile
from cli.commands import set_gb as cmd_set_gb
from cli.commands import restore as cmd_restore
from cli.commands import handoff as cmd_handoff
from cli.commands import release as cmd_release
from cli.commands import claim as cmd_claim
from cli.commands import attach as cmd_attach
from cli.commands import detach as cmd_detach
from cli.commands import attachments as cmd_attachments
from cli.commands import status as cmd_status
from cli.commands import log as cmd_log
from cli.commands import add_collaborator as cmd_add_collaborator
from cli.commands import remove_collaborator as cmd_remove_collaborator
from cli.commands import rename as cmd_rename


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bandtracker",
        description="Version control for GarageBand.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="bandtracker 0.1.0",
    )

    subparsers = parser.add_subparsers(
        title="commands",
        dest="command",
        metavar="<command>",
    )
    subparsers.required = True

    cmd_init.add_subparser(subparsers)
    cmd_snapshot.add_subparser(subparsers)
    cmd_learn_noise.add_subparser(subparsers)
    cmd_watch.add_subparser(subparsers)
    cmd_reconcile.add_subparser(subparsers)
    cmd_set_gb.add_subparser(subparsers)
    cmd_restore.add_subparser(subparsers)
    cmd_handoff.add_subparser(subparsers)
    cmd_release.add_subparser(subparsers)
    cmd_claim.add_subparser(subparsers)
    cmd_attach.add_subparser(subparsers)
    cmd_detach.add_subparser(subparsers)
    cmd_attachments.add_subparser(subparsers)
    cmd_status.add_subparser(subparsers)
    cmd_log.add_subparser(subparsers)
    cmd_add_collaborator.add_subparser(subparsers)
    cmd_remove_collaborator.add_subparser(subparsers)
    cmd_rename.add_subparser(subparsers)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
