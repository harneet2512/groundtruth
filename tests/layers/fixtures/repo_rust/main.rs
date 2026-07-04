//! Application entry. Wires config + logger + handler.

mod url;
mod handler;
mod config;
mod logger;

use config::load_config;
use logger::Logger;

pub fn run() {
    let cfg = load_config();
    let log = Logger::new(&cfg.log_level);
    log.info(&format!("starting on port {}", cfg.port));
}
