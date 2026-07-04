//! Runtime configuration loader.

use std::env;

pub struct Config {
    pub port: u16,
    pub log_level: String,
}

pub fn load_config() -> Config {
    Config {
        port: env::var("PORT").ok().and_then(|s| s.parse().ok()).unwrap_or(8080),
        log_level: env::var("LOG_LEVEL").unwrap_or_else(|_| "info".to_string()),
    }
}
