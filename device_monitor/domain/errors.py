class ValidationError(ValueError):
    """Некорректные данные от клиента - отдаётся как 400, а не как 500."""


class ConflictError(ValidationError):
    """Операция противоречит текущему состоянию: дубль имени, непустая группа."""
