"""Bus d'événements local au service goal.

Rapatrié depuis modules/events (monolithe). Le bus est intra-processus :
les événements publiés ici ne quittent pas le service goal. Si le core
doit un jour les recevoir, on les relaiera en HTTP (phase 3), jamais
par import de code.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger("goal.events")

# Types d'événements du domaine goal
GOAL_CREATED = "goal.created"
PLAN_CREATED = "plan.created"
TASK_CREATED = "task.created"
PROJECT_CREATED = "project.created"

# Types d'événements du domaine agents/tools (ajoutés phase 4)
AGENT_CREATED = "agent.created"
AGENT_PROMOTED = "agent.promoted"
AGENT_REGISTERED = "agent.registered"
AGENT_EXECUTION_STARTED = "agent.execution_started"
AGENT_EXECUTION_COMPLETED = "agent.execution_completed"
TOOL_CREATED = "tool.created"
TOOL_REGISTERED = "tool.registered"
TOOL_EXECUTION_STARTED = "tool.execution_started"
TOOL_EXECUTION_COMPLETED = "tool.execution_completed"


@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any]
    source: str = "goal"
    event_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


Handler = Callable[[Event], Awaitable[Any] | Any]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._background_tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_type: str, handler: Handler) -> None:
        if handler in self._subscribers[event_type]:
            return
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> bool:
        handlers = self._subscribers.get(event_type)
        if not handlers or handler not in handlers:
            return False
        handlers.remove(handler)
        if not handlers:
            self._subscribers.pop(event_type, None)
        return True

    async def _dispatch(self, event: Event, handler: Handler) -> Any:
        result = handler(event)
        if inspect.isawaitable(result):
            return await result
        return result

    async def publish(self, event: Event) -> None:
        handlers = list(self._subscribers.get(event.type, []))
        if event.type != "*":
            for handler in self._subscribers.get("*", []):
                if handler not in handlers:
                    handlers.append(handler)

        logger.info(
            "event_published type=%s source=%s event_id=%s handlers=%s",
            event.type,
            event.source,
            event.event_id,
            len(handlers),
        )
        if not handlers:
            return

        results = await asyncio.gather(
            *(self._dispatch(event, handler) for handler in handlers),
            return_exceptions=True,
        )
        for handler, result in zip(handlers, results):
            if isinstance(result, BaseException):
                logger.error(
                    "event_handler_failed type=%s handler=%s error=%r",
                    event.type,
                    getattr(handler, "__name__", str(handler)),
                    result,
                    exc_info=result,
                )

    def publish_background(self, event: Event) -> None:
        """Publie depuis du code synchrone sans coupler producteur et abonnés."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.publish(event))
            return
        task = loop.create_task(self.publish(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_background_failure)

    @staticmethod
    def _log_background_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("event_background_publish_failed error=%r", error, exc_info=error)


event_bus = EventBus()
