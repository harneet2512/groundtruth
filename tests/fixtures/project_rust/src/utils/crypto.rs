use crate::utils::errors::AppError;

pub fn hash_password(password: &str) -> Result<String, AppError> {
    if password.is_empty() {
        return Err(AppError::new("password cannot be empty"));
    }
    // Stub: simple hash
    Ok(format!("hashed_{}", password.len()))
}

pub fn compare_password(password: &str, stored_hash: &str) -> bool {
    if password.is_empty() || stored_hash.is_empty() {
        return false;
    }
    let expected = format!("hashed_{}", password.len());
    expected == stored_hash
}
