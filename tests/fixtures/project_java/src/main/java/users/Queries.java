package users;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import utils.AppError;

public class Queries {
    private static final Map<Integer, User> USERS = new HashMap<>();
    private static final Map<String, User> USERS_BY_EMAIL = new HashMap<>();

    public static User getUserById(int id) {
        if (id <= 0) {
            throw new IllegalArgumentException("id must be positive");
        }
        User user = USERS.get(id);
        if (user == null) {
            throw new AppError("user not found: " + id);
        }
        return user;
    }

    public static User getUserByEmail(String email) {
        if (email == null || email.isEmpty()) {
            throw new IllegalArgumentException("email is required");
        }
        return USERS_BY_EMAIL.get(email);
    }

    public static User createUser(String email, String name, String passwordHash) {
        if (email == null || email.isEmpty()) {
            throw new IllegalArgumentException("email is required");
        }
        if (name == null || name.isEmpty()) {
            throw new IllegalArgumentException("name is required");
        }
        int id = USERS.size() + 1;
        User user = new User(id, email, name, passwordHash);
        USERS.put(id, user);
        USERS_BY_EMAIL.put(email, user);
        return user;
    }
}
