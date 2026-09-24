"""Control-flow signals that must pass through processing-library error wrappers."""


class TaskControlSignal(RuntimeError):
    """Base class for cooperative task interruption, not a processing failure."""
