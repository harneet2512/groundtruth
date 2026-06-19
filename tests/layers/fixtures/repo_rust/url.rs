//! URL parsing utilities used by the request handler.

#[derive(Debug, PartialEq)]
pub struct ParsedUrl {
    pub scheme: String,
    pub host: String,
    pub path: String,
}

/// parse_url splits a URL string into its scheme/host/path components and
/// validates that the scheme and host are non-empty.
pub fn parse_url(raw: &str) -> Result<ParsedUrl, String> {
    if raw.is_empty() {
        return Err("parse_url: empty".to_string());
    }
    let (scheme, rest) = match raw.split_once("://") {
        Some((s, r)) => (s, r),
        None => return Err("parse_url: missing scheme".to_string()),
    };
    let (host, path) = match rest.split_once('/') {
        Some((h, p)) => (h, format!("/{}", p)),
        None => (rest, "/".to_string()),
    };
    if host.is_empty() {
        return Err("parse_url: missing host".to_string());
    }
    Ok(ParsedUrl {
        scheme: scheme.to_string(),
        host: host.to_string(),
        path,
    })
}

/// is_https returns true when the parsed scheme is exactly "https".
pub fn is_https(parsed: &ParsedUrl) -> bool {
    parsed.scheme == "https"
}
