class BaseError extends Error {}

class ServiceError extends BaseError {}

class BaseService {
  run(value) {
    return value;
  }
}

class UserService extends BaseService {
  constructor() {
    super();
    this.count = 0;
  }

  validate(value) {
    this.count = this.count + 1;
    if (value == null) {
      throw new ServiceError("missing");
    }
    return this.run(value);
  }
}

export function entry(value) {
  const service = new UserService();
  return service.validate(value);
}
