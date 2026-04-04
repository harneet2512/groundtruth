package utils;

public class AppError extends RuntimeException {
    private final int statusCode;

    public AppError(String message) {
        super(message);
        this.statusCode = 400;
    }

    public AppError(String message, int statusCode) {
        super(message);
        this.statusCode = statusCode;
    }

    public int getStatusCode() {
        return statusCode;
    }
}
