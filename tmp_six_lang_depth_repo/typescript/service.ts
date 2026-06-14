interface Runner {
  run(value: string): string;
}

class BaseError extends Error {}

class ServiceError extends BaseError {}

class UserService implements Runner {
  private count = 0;

  run(value: string): string {
    return value;
  }

  validate(value: string | null): string {
    this.count = this.count + 1;
    if (value === null) {
      throw new ServiceError("missing");
    }
    return this.run(value);
  }
}

export function entry(value: string | null): string {
  const service = new UserService();
  return service.validate(value);
}
