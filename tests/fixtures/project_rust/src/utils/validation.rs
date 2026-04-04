pub fn validate_email(email: &str) -> bool {
    if email.is_empty() {
        return false;
    }
    email.contains('@') && email.contains('.')
}

pub fn validate_password(password: &str) -> bool {
    if password.is_empty() {
        return false;
    }
    password.len() >= 8
}
