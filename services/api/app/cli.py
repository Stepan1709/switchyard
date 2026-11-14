"""Command line tools: `python -m app.cli create-admin --email you@example.com`."""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.errors import AppError
from app.modules.auth.schemas import AdminCreate
from app.modules.auth.service import AuthService


async def create_admin(email: str, password: str, role: str) -> None:
    settings = Settings(service_name="cli")
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            admin = await AuthService(session, settings).create_admin(
                AdminCreate(email=email, password=password, role=role)  # type: ignore[arg-type]
            )
            print(f"created {admin.role} {admin.email} ({admin.id})")
    finally:
        await engine.dispose()


def _dlq(args: argparse.Namespace) -> int:
    from aio_pika.exceptions import AMQPError, ChannelNotFoundEntity

    from app import dlq

    settings = Settings(service_name="cli")
    try:
        if args.dlq_command == "list":
            items = asyncio.run(
                dlq.list_dead_letters(settings, args.queue, args.limit, args.show_payload)
            )
            print(dlq.format_lines(items) if items else "(the dead-letter queue is empty)")
        else:
            moved = asyncio.run(dlq.requeue_dead_letters(settings, args.queue, args.limit))
            print(f"requeued {moved} message(s) to {args.queue}")
    except ChannelNotFoundEntity:
        print(f"error: there is no dead-letter queue for {args.queue!r}", file=sys.stderr)
        return 1
    except (AMQPError, OSError) as exc:
        print(f"error: cannot talk to the broker: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin", help="create an administrator")
    p.add_argument("--email", required=True)
    p.add_argument("--role", choices=["admin", "viewer"], default="admin")
    p.add_argument("--password-file", help="read the password from a file instead of a prompt")
    dlq = sub.add_parser("dlq", help="inspect and requeue dead-lettered messages")
    dlq_sub = dlq.add_subparsers(dest="dlq_command", required=True)
    for name, text in (("list", "show failed messages and why"), ("requeue", "put them back")):
        d = dlq_sub.add_parser(name, help=text)
        d.add_argument("--queue", required=True, help="work queue name, e.g. worker.request_log")
        d.add_argument("--limit", type=int, default=100)
        if name == "list":
            d.add_argument("--show-payload", action="store_true", help="also print message bodies")
    args = parser.parse_args(argv)

    if args.command == "dlq":
        return _dlq(args)

    if args.password_file:
        with open(args.password_file, encoding="utf-8") as fh:
            password = fh.read().strip()
    else:
        # getpass: nothing is echoed and the password never appears in shell history.
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("passwords do not match", file=sys.stderr)
            return 1
    try:
        asyncio.run(create_admin(args.email, password, args.role))
    except AppError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
