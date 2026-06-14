class BaseError(Exception):
    pass


class ServiceError(BaseError):
    pass


class BaseService:
    def run(self, value):
        return value


class UserService(BaseService):
    def __init__(self):
        self.count = 0

    def validate(self, value):
        self.count = self.count + 1
        if value is None:
            raise ServiceError("missing")
        return self.run(value)


def entry(value):
    service = UserService()
    return service.validate(value)
