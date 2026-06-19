//! Request handler: validates inbound URLs via parse_url.

use crate::url::{parse_url, is_https, ParsedUrl};

pub struct Response {
    pub status: u16,
    pub body: String,
}

pub fn handle(raw: &str) -> Response {
    match parse_url(raw) {
        Err(e) => Response { status: 400, body: e },
        Ok(parsed) => {
            if !is_https(&parsed) {
                return Response { status: 426, body: "https required".into() };
            }
            Response { status: 200, body: format_ok(&parsed) }
        }
    }
}

fn format_ok(p: &ParsedUrl) -> String {
    format!("ok host={} path={}", p.host, p.path)
}
