use std::collections::HashMap;
use crate::auth::jwt;
use crate::utils::errors::AppError;

pub fn verify_token(token: &str) -> Result<HashMap<String, String>, AppError> {
    if token.is_empty() {
        return Err(AppError::new("token is required"));
    }

    let payload = jwt::decode_token(token)?;
    // Check expiry if present
    if jwt::is_token_expired(None) {
        return Err(AppError::new("token has expired"));
    }

    Ok(payload)
}
