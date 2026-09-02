class ElasticFetchError(RuntimeError):
    """
    Raised when Elastic searches fail or return partial data.
    Optionally carries any partial items that were collected so the UI can
    continue displaying what was retrieved.
    """

    def __init__(self, message: str, items=None):
        super().__init__(message)
        self.items = list(items) if items else []
