use crate::auth::verify;
use crate::utils::errors::AppError;

pub fn auth_middleware(token: &str) -> Result<(), AppError> {
    if token.is_empty() {
        return Err(AppError::with_status("authentication required", 401));
    }

    let _payload = verify::verify_token(token)?;
    Ok(())
}
