package auth;

import java.util.Base64;
import java.util.Map;
import java.util.HashMap;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import users.User;

public class Jwt {
    private static final String SECRET = "stub-secret-key";
    private static final String ALGORITHM = "HmacSHA256";

    public static String signToken(Map<String, Object> payload) {
        if (payload == null) {
            throw new IllegalArgumentException("payload cannot be null");
        }
        if (payload.isEmpty()) {
            throw new IllegalArgumentException("payload cannot be empty");
        }

        String header = Base64.getEncoder().encodeToString("{\"alg\":\"HS256\",\"typ\":\"JWT\"}".getBytes());
        String body = Base64.getEncoder().encodeToString(payload.toString().getBytes());
        String signature = hmacSign(header + "." + body);
        return header + "." + body + "." + signature;
    }

    public static Map<String, Object> decodeToken(String token) {
        if (token == null || token.isEmpty()) {
            throw new IllegalArgumentException("token cannot be null or empty");
        }

        String[] parts = token.split("\\.");
        if (parts.length != 3) {
            throw new IllegalArgumentException("invalid token format");
        }

        String expectedSig = hmacSign(parts[0] + "." + parts[1]);
        if (!expectedSig.equals(parts[2])) {
            throw new SecurityException("invalid token signature");
        }

        String decoded = new String(Base64.getDecoder().decode(parts[1]));
        Map<String, Object> result = new HashMap<>();
        result.put("raw", decoded);
        return result;
    }

    public static boolean isTokenExpired(Map<String, Object> payload) {
        if (payload == null) {
            return true;
        }
        Object exp = payload.get("exp");
        if (exp instanceof Long) {
            return (Long) exp < System.currentTimeMillis() / 1000;
        }
        return false;
    }

    private static String hmacSign(String data) {
        try {
            Mac mac = Mac.getInstance(ALGORITHM);
            mac.init(new SecretKeySpec(SECRET.getBytes(), ALGORITHM));
            byte[] hash = mac.doFinal(data.getBytes());
            return Base64.getUrlEncoder().withoutPadding().encodeToString(hash);
        } catch (Exception e) {
            throw new RuntimeException("HMAC signing failed", e);
        }
    }
}
