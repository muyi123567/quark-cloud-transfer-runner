class QuarkCloudTransferError(RuntimeError):
    """Base error for this runtime asset."""


class ConfigError(QuarkCloudTransferError):
    """Configuration or credential input is invalid."""


class QuarkError(QuarkCloudTransferError):
    """Quark Web API failed."""


class QuarkCdnError(QuarkError):
    """A Quark CDN download failed in a way a fresh URL may repair.

    Connect/read timeouts, connection resets, truncated streams, and expired
    signed-URL responses all land here. Callers are expected to re-request a
    download URL for the same FID and retry with backoff.
    """


class DriveError(QuarkCloudTransferError):
    """Google Drive API failed."""


class TransferError(QuarkCloudTransferError):
    """The requested transfer cannot be completed safely."""
