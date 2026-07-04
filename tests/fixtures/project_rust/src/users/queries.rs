use crate::users::types::User;
use crate::utils::errors::AppError;

pub fn get_user_by_id(id: i64) -> Result<User, AppError> {
    if id <= 0 {
        return Err(AppError::new("id must be positive"));
    }
    // Stub implementation
    Ok(User::new(id, "user@example.com".into(), "User".into(), "hash".into()))
}

pub fn get_user_by_email(email: &str) -> Result<User, AppError> {
    if email.is_empty() {
        return Err(AppError::new("email is required"));
    }
    Ok(User::new(1, email.into(), "User".into(), "hash".into()))
}

pub fn create_user(email: &str, name: &str, password_hash: &str) -> Result<User, AppError> {
    if email.is_empty() {
        return Err(AppError::new("email is required"));
    }
    if name.is_empty() {
        return Err(AppError::new("name is required"));
    }
    Ok(User::new(1, email.into(), name.into(), password_hash.into()))
}
