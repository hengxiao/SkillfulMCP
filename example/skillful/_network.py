"""
Thin orchestrator/worker wiring shared by the framework runners.

This is NOT part of the public Skillful* API — it's glue that the example
runners use to demonstrate multi-agent topologies. In real deployments, the
routing logic is whatever your app needs (direct delegation, queues, A2A
protocols); the Skillful* agent classes themselves know nothing about it.
"""

from __future__ import annotations

from typing import Protocol

from example.common import orchestrator_routing_tool_schema


class SupportsRouting(Protocol):
    agent_id: str

    def run(self, message: str) -> str: ...
    def bind_extra_tool(self, schema: dict, handler) -> None: ...


def wire_orchestrator(
    orchestrator: SupportsRouting,
    workers: dict[str, SupportsRouting],
    *,
    log: bool = True,
) -> None:
    """
    Attach a `route_to_agent` meta-tool to `orchestrator` that delegates into
    `workers` (keyed by agent_id) inside this Python process.
    """
    schema = orchestrator_routing_tool_schema(list(workers.keys()))

    def handler(_name: str, args: dict) -> dict:
        worker = workers.get(args.get("agent_id"))
        if not worker:
            return {"error": f"unknown agent {args.get('agent_id')!r}"}
        if log:
            print(f"\n  [Network] routing → {worker.agent_id}")
        return {"worker_response": worker.run(args["request_summary"])}

    orchestrator.bind_extra_tool(schema, handler)
