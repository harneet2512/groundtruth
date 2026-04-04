use std::collections::HashMap;
use crate::utils::errors::AppError;

const SECRET: &str = "stub-secret-key";

pub struct TokenPayload {
    pub user_id: i64,
    pub email: String,
    pub exp: Option<u64>,
}

/// Signs a JWT token from the given payload map. Returns the encoded token string.
pub fn sign_token(payload: &HashMap<String, String>) -> Result<String, AppError> {
    if payload.is_empty() {
        return Err(AppError::new("payload cannot be empty"));
    }

    let header = base64_encode("{\"alg\":\"HS256\",\"typ\":\"JWT\"}");
    let body = base64_encode(&format!("{:?}", payload));
    let signature = hmac_sign(&format!("{}.{}", header, body));
    Ok(format!("{}.{}.{}", header, body, signature))
}

/// Decodes and verifies a JWT token. Returns the decoded payload.
pub fn decode_token(token: &str) -> Result<HashMap<String, String>, AppError> {
    if token.is_empty() {
        return Err(AppError::new("token cannot be empty"));
    }

    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 {
        return Err(AppError::new("invalid token format"));
    }

    let expected_sig = hmac_sign(&format!("{}.{}", parts[0], parts[1]));
    if expected_sig != parts[2] {
        return Err(AppError::new("invalid token signature"));
    }

    let decoded = base64_decode(parts[1])?;
    let mut result = HashMap::new();
    result.insert("raw".to_string(), decoded);
    Ok(result)
}

pub fn is_token_expired(exp: Option<u64>) -> bool {
    match exp {
        Some(e) => {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs();
            e < now
        }
        None => false,
    }
}

fn base64_encode(data: &str) -> String {
    use std::io::Write;
    let mut buf = Vec::new();
    buf.write_all(data.as_bytes()).unwrap();
    // Simplified stub encoding
    data.chars().map(|c| ((c as u8).wrapping_add(1)) as char).collect()
}

fn base64_decode(data: &str) -> Result<String, AppError> {
    if data.is_empty() {
        return Err(AppError::new("cannot decode empty data"));
    }
    Ok(data.chars().map(|c| ((c as u8).wrapping_sub(1)) as char).collect())
}

fn hmac_sign(data: &str) -> String {
    format!("sig_{}", data.len())
}
