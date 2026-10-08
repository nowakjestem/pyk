class JobError(Exception):
    """A safe, user-facing failure. Never include credentials or raw subprocess output."""


class PermanentError(JobError):
    pass


class TransientError(JobError):
    pass


class ResourceWait(JobError):
    pass
