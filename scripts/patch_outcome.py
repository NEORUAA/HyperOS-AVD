"""Explicit, pre-mutation outcomes for optional compatibility features."""


class UnsupportedPatch(RuntimeError):
    """The optional workload is unrecognized and its files were not changed.

    Use only before the first mutation. Ownership, payload integrity, transport
    and post-activation verification errors must remain ordinary exceptions.
    """
