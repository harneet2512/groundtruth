//! Stdout structured logger.

pub struct Logger {
    pub level: String,
}

impl Logger {
    pub fn new(level: &str) -> Self {
        Logger { level: level.to_string() }
    }

    pub fn info(&self, msg: &str) {
        println!("[{}] {}", self.level, msg);
    }

    pub fn error(&self, msg: &str) {
        eprintln!("[{}] error: {}", self.level, msg);
    }
}
