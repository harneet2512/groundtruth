use std::fmt;

#[derive(Debug)]
pub struct AppError {
    pub message: String,
    pub status_code: u16,
}

impl AppError {
    pub fn new(message: &str) -> Self {
        AppError {
            message: message.to_string(),
            status_code: 400,
        }
    }

    pub fn with_status(message: &str, status_code: u16) -> Self {
        AppError {
            message: message.to_string(),
            status_code,
        }
    }
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "AppError({}): {}", self.status_code, self.message)
    }
}

impl std::error::Error for AppError {}
