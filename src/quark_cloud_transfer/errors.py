class QuarkCloudTransferError(RuntimeError):
    """Base error for this runtime asset."""


class ConfigError(QuarkCloudTransferError):
    """Configuration or credential input is invalid."""


class QuarkError(QuarkCloudTransferError):
    """Quark Web API failed."""


class DriveError(QuarkCloudTransferError):
    """Google Drive API failed."""


class TransferError(QuarkCloudTransferError):
    """The requested transfer cannot be completed safely."""
