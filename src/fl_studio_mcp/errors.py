"""Exception types, importable without numpy/scipy so the basic tools start fast and work without them."""


class FLError(RuntimeError):
    """Something went wrong talking to FL Studio or with the arguments of an FL operation."""


class AudioError(RuntimeError):
    """An audio file or recording could not be analyzed."""
