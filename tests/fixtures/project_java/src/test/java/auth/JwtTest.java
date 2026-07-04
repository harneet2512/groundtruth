package auth;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
import java.util.Map;
import java.util.HashMap;

class JwtTest {

    @Test
    void testSignTokenReturnsValidFormat() {
        Map<String, Object> payload = new HashMap<>();
        payload.put("user_id", 1);
        payload.put("email", "test@example.com");
        String token = Jwt.signToken(payload);
        assertNotNull(token);
        assertEquals(3, token.split("\\.").length);
    }

    @Test
    void testSignTokenNullPayloadThrows() {
        assertThrows(IllegalArgumentException.class, () -> Jwt.signToken(null));
    }

    @Test
    void testSignTokenEmptyPayloadThrows() {
        assertThrows(IllegalArgumentException.class, () -> Jwt.signToken(new HashMap<>()));
    }

    @Test
    void testDecodeTokenRoundTrip() {
        Map<String, Object> payload = new HashMap<>();
        payload.put("user_id", 42);
        String token = Jwt.signToken(payload);
        Map<String, Object> decoded = Jwt.decodeToken(token);
        assertNotNull(decoded);
        assertTrue(decoded.containsKey("raw"));
    }

    @Test
    void testDecodeTokenInvalidThrows() {
        assertThrows(IllegalArgumentException.class, () -> Jwt.decodeToken("invalid"));
    }

    @Test
    void testDecodeTokenNullThrows() {
        assertThrows(IllegalArgumentException.class, () -> Jwt.decodeToken(null));
    }

    @Test
    void testIsTokenExpiredWithFuture() {
        Map<String, Object> payload = new HashMap<>();
        payload.put("exp", System.currentTimeMillis() / 1000 + 3600);
        assertFalse(Jwt.isTokenExpired(payload));
    }

    @Test
    void testIsTokenExpiredWithPast() {
        Map<String, Object> payload = new HashMap<>();
        payload.put("exp", 0L);
        assertTrue(Jwt.isTokenExpired(payload));
    }
}
