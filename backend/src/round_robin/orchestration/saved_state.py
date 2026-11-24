"""Narrow typed boundary for unreadable saved Step data."""

import json


class SavedStateUnreadable(RuntimeError):
    """Saved Step JSON cannot be decoded; its private data stays local."""


def read_saved_steps(session, statement):
    try:
        return list(session.scalars(statement))
    except (json.JSONDecodeError, RecursionError):
        raise SavedStateUnreadable("Saved step data could not be decoded") from None


def read_saved_step(session, statement):
    try:
        return session.scalar(statement)
    except (json.JSONDecodeError, RecursionError):
        raise SavedStateUnreadable("Saved step data could not be decoded") from None
