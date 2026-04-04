use std::collections::HashMap;
use gt_fixture_auth::auth::jwt;
use gt_fixture_auth::auth::login;
use gt_fixture_auth::auth::verify;
use gt_fixture_auth::utils::errors::AppError;

#[test]
fn test_sign_token_valid() {
    let mut payload = HashMap::new();
    payload.insert("user_id".to_string(), "1".to_string());
    payload.insert("email".to_string(), "test@example.com".to_string());
    let token = jwt::sign_token(&payload).unwrap();
    assert!(!token.is_empty());
    assert_eq!(token.matches('.').count(), 2);
}

#[test]
fn test_sign_token_empty_payload_fails() {
    let payload = HashMap::new();
    let result = jwt::sign_token(&payload);
    assert!(result.is_err());
}

#[test]
fn test_decode_token_roundtrip() {
    let mut payload = HashMap::new();
    payload.insert("user_id".to_string(), "42".to_string());
    let token = jwt::sign_token(&payload).unwrap();
    let decoded = jwt::decode_token(&token).unwrap();
    assert!(decoded.contains_key("raw"));
}

#[test]
fn test_decode_token_invalid() {
    let result = jwt::decode_token("invalid-token");
    assert!(result.is_err());
}

#[test]
fn test_is_token_expired_none() {
    assert!(!jwt::is_token_expired(None));
}

#[test]
fn test_is_token_expired_past() {
    assert!(jwt::is_token_expired(Some(0)));
}
