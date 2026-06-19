package utils;

import java.util.regex.Pattern;

public class Validation {
    private static final Pattern EMAIL_PATTERN = Pattern.compile("^[\\w.+-]+@[\\w.-]+\\.[a-zA-Z]{2,}$");
    private static final int MIN_PASSWORD_LENGTH = 8;

    public static boolean validateEmail(String email) {
        if (email == null || email.isEmpty()) {
            return false;
        }
        return EMAIL_PATTERN.matcher(email).matches();
    }

    public static boolean validatePassword(String password) {
        if (password == null) {
            return false;
        }
        return password.length() >= MIN_PASSWORD_LENGTH;
    }
}
