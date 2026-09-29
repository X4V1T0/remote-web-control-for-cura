"""API error type shared by the HTTP layer and the services."""


class ApiError(Exception):
    """An error that is reported to the client as {"error": {"code", "message"}}."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
