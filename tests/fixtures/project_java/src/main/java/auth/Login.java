package auth;

import java.util.Map;
import java.util.HashMap;
import users.User;
import users.Queries;
import utils.Crypto;
import utils.Validation;
import utils.AppError;

public class Login {

    public static Map<String, Object> login(String email, String password) {
        if (email == null || email.isEmpty()) {
            throw new AppError("email is required");
        }
        if (password == null || password.isEmpty()) {
            throw new AppError("password is required");
        }
        if (!Validation.validateEmail(email)) {
            throw new AppError("invalid email format");
        }

        User user = Queries.getUserByEmail(email);
        if (user == null) {
            throw new AppError("user not found");
        }

        if (!Crypto.comparePassword(password, user.getPasswordHash())) {
            throw new AppError("invalid password");
        }

        Map<String, Object> payload = new HashMap<>();
        payload.put("user_id", user.getId());
        payload.put("email", user.getEmail());

        String token = Jwt.signToken(payload);
        Map<String, Object> result = new HashMap<>();
        result.put("token", token);
        result.put("user", user);
        return result;
    }
}
