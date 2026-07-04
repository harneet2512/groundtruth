package auth;

import java.util.HashSet;
import java.util.Set;

public class Logout {
    private static final Set<String> blacklist = new HashSet<>();

    public static void logout(String token) {
        if (token == null || token.isEmpty()) {
            throw new IllegalArgumentException("token is required");
        }
        blacklist.add(token);
    }

    public static boolean isBlacklisted(String token) {
        if (token == null) {
            return false;
        }
        return blacklist.contains(token);
    }

    public static void clearBlacklist() {
        blacklist.clear();
    }
}
