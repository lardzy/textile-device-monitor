"""Persistent workflow execution domain.

The package deliberately has no import-time database or worker side effects.
Applications and Alembic only need to import :mod:`app.execution.models` so the
tables are registered on ``app.database.Base``.
"""

from app.execution.registry import node_registry

__all__ = ["node_registry"]
