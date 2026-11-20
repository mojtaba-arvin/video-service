"""Operator CLI for inspecting and replaying transactional workflow dispatch."""

import argparse
import json
import os
import socket

from video_streaming.workflows.service import get_workflow_repository


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stats", help="show bounded relay health counters")
    dead = commands.add_parser("dead", help="list dead-lettered dispatch rows")
    dead.add_argument("--limit", type=int, default=100)
    inspect = commands.add_parser("inspect", help="inspect one outbox row")
    inspect.add_argument("outbox_id")
    replay = commands.add_parser("replay", help="audited replay of one dead row")
    replay.add_argument("outbox_id")
    replay.add_argument("--operator-id", required=True)
    replay.add_argument("--reason", required=True)
    relay = commands.add_parser("relay-once", help="publish one due outbox batch")
    relay.add_argument("--owner", default=socket.gethostname())
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    repository = get_workflow_repository()
    try:
        if args.command == "stats":
            output = repository.get_outbox_metrics()
        elif args.command == "dead":
            output = repository.list_outbox("DEAD", args.limit)
        elif args.command == "inspect":
            output = repository.get_outbox_snapshot(args.outbox_id)
            if output is None:
                raise SystemExit("outbox row not found")
        elif args.command == "replay":
            replayed = repository.replay_dead_outbox(
                args.outbox_id,
                operator_id=args.operator_id,
                reason=args.reason,
            )
            if not replayed:
                raise SystemExit("outbox row is missing or not DEAD")
            output = repository.get_outbox_snapshot(args.outbox_id)
        else:
            # Imported only for explicit relay invocation; read-only inspection
            # does not require importing the Celery runtime.
            from video_streaming.celery import celery_app
            from video_streaming.workflows.relay import WorkflowOutboxRelay

            relay = WorkflowOutboxRelay(
                repository=repository,
                publisher=celery_app,
                owner=args.owner,
                queue=os.environ.get("WORKFLOW_OUTBOX_QUEUE") or None,
                lease_ttl_seconds=int(os.environ.get(
                    "WORKFLOW_OUTBOX_LEASE_TTL_SECONDS", "30"
                )),
                batch_size=int(os.environ.get(
                    "WORKFLOW_OUTBOX_BATCH_SIZE", "25"
                )),
                max_attempts=int(os.environ.get(
                    "WORKFLOW_OUTBOX_MAX_ATTEMPTS", "20"
                )),
            )
            output = relay.run_once()
        print(json.dumps(output, sort_keys=True, indent=2))
        return 0
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
