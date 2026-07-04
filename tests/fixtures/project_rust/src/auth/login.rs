use std::collections::HashMap;
use crate::auth::jwt;
use crate::users::queries;
use crate::utils::crypto;
use crate::utils::errors::AppError;
use crate::utils::validation;

pub struct LoginResult {
    pub token: String,
    pub user_id: i64,
}

pub fn login(email: &str, password: &str) -> Result<LoginResult, AppError> {
    if email.is_empty() {
        return Err(AppError::new("email is required"));
    }
    if password.is_empty() {
        return Err(AppError::new("password is required"));
    }
    if !validation::validate_email(email) {
        return Err(AppError::new("invalid email format"));
    }

    let user = queries::get_user_by_email(email)?;

    if !crypto::compare_password(password, &user.password_hash) {
        return Err(AppError::new("invalid password"));
    }

    let mut payload = HashMap::new();
    payload.insert("user_id".to_string(), user.id.to_string());
    payload.insert("email".to_string(), user.email.clone());

    let token = jwt::sign_token(&payload)?;
    Ok(LoginResult { token, user_id: user.id })
}
