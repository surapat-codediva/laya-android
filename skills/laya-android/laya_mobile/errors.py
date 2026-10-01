"""Typed errors. Library code raises these; the CLI turns them into exit 2.
Handoff (3) and stale (4) are expected outcomes, returned as results, not raised."""


class LayaAndroidError(Exception):
    """Usage, device or runtime error -- CLI exit 2."""


class UsageError(LayaAndroidError):
    pass


class AdbError(LayaAndroidError):
    pass


class DeviceError(AdbError):
    """No device, or several and none chosen."""


class ObservationError(LayaAndroidError):
    """The screen could not be read (locked, secure window, dump failed)."""
