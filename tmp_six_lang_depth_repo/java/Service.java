interface Runner {
    String run(String value);
}

class BaseError extends RuntimeException {
    BaseError(String message) {
        super(message);
    }
}

class ServiceError extends BaseError {
    ServiceError(String message) {
        super(message);
    }
}

class UserService implements Runner {
    private int count = 0;

    public String run(String value) {
        return value;
    }

    public String validate(String value) {
        this.count = this.count + 1;
        if (value == null) {
            throw new ServiceError("missing");
        }
        return this.run(value);
    }
}

class EntryPoint {
    static String entry(String value) {
        UserService service = new UserService();
        return service.validate(value);
    }
}
