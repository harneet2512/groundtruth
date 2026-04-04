package auth;

import java.util.Map;

public class Verify {

    public static Map<String, Object> verifyToken(String token) {
        if (token == null || token.isEmpty()) {
            throw new IllegalArgumentException("token is required");
        }

        Map<String, Object> payload = Jwt.decodeToken(token);
        if (Jwt.isTokenExpired(payload)) {
            throw new SecurityException("token has expired");
        }

        return payload;
    }
}
