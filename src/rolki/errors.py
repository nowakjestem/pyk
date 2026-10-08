class JobError(Exception):
    """A safe, user-facing failure. Never include credentials or raw subprocess output."""


class PermanentError(JobError):
    pass


class TransientError(JobError):
    def __init__(self, message: str, *, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


class ResourceWait(JobError):
    pass
