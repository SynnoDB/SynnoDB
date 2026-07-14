//! Parse a query's placeholder values off the request line.
//!
//! The wire format is set by the Python side (`HotpatchProc._write_control_message`
//! builds lines like `1 <req_id> "BRAND#23" 15`) and read by the C++
//! `args_parser.hpp` with `std::quoted` / `operator>>`. This is the Rust reader
//! of that same format, so the two engines accept byte-identical request lines.
//!
//! Three things it must reproduce:
//!   * strings arrive double-quoted, with `""` as an embedded quote (std::quoted);
//!   * numbers arrive bare, whitespace-separated;
//!   * an IN-list arrives as `('a', 'b')`, with `''` as an embedded quote, and a
//!     NULL element as the literal string `<<NULL>>`.

use crate::{Error, Result};

pub struct ArgScanner<'a> {
    rest: &'a str,
    query_id: &'a str,
}

impl<'a> ArgScanner<'a> {
    pub fn new(line: &'a str, query_id: &'a str) -> Self {
        Self { rest: line, query_id }
    }

    fn skip_ws(&mut self) {
        self.rest = self.rest.trim_start();
    }

    fn fail<T>(&self, what: &str) -> Result<T> {
        Err(Error::new(format!(
            "Q{}: failed to parse {what}",
            self.query_id
        )))
    }

    /// A `std::quoted` string: `"..."` with `""` as an embedded quote. An
    /// unquoted token is accepted too, so a bare value still parses.
    pub fn string(&mut self, name: &str) -> Result<String> {
        self.skip_ws();
        let mut chars = self.rest.chars();
        match chars.next() {
            None => self.fail(name),
            Some('"') => {
                let mut out = String::new();
                let mut it = chars.peekable();
                loop {
                    match it.next() {
                        None => return self.fail(name),
                        Some('"') => {
                            if it.peek() == Some(&'"') {
                                it.next();
                                out.push('"');
                                continue;
                            }
                            break;
                        }
                        Some(c) => out.push(c),
                    }
                }
                let consumed = self.rest.len() - it.collect::<String>().len();
                self.rest = &self.rest[consumed..];
                Ok(out)
            }
            Some(_) => {
                let end = self.rest.find(char::is_whitespace).unwrap_or(self.rest.len());
                let tok = &self.rest[..end];
                self.rest = &self.rest[end..];
                Ok(tok.to_string())
            }
        }
    }

    pub fn int(&mut self, name: &str) -> Result<i64> {
        self.skip_ws();
        let end = self.rest.find(char::is_whitespace).unwrap_or(self.rest.len());
        let tok = &self.rest[..end];
        match tok.parse::<i64>() {
            Ok(v) => {
                self.rest = &self.rest[end..];
                Ok(v)
            }
            Err(_) => self.fail(name),
        }
    }

    pub fn float(&mut self, name: &str) -> Result<f64> {
        self.skip_ws();
        let end = self.rest.find(char::is_whitespace).unwrap_or(self.rest.len());
        let tok = &self.rest[..end];
        match tok.parse::<f64>() {
            Ok(v) => {
                self.rest = &self.rest[end..];
                Ok(v)
            }
            Err(_) => self.fail(name),
        }
    }

    /// `('val1', 'val2', ...)`. NULL elements arrive as the literal `<<NULL>>`.
    pub fn in_list(&mut self, name: &str) -> Result<Vec<String>> {
        self.skip_ws();
        if !self.rest.starts_with('(') {
            return self.fail(name);
        }
        let mut it = self.rest[1..].chars().peekable();
        let mut out = Vec::new();
        let mut consumed = 1usize;

        loop {
            while let Some(c) = it.peek() {
                if c.is_whitespace() || *c == ',' {
                    consumed += c.len_utf8();
                    it.next();
                } else {
                    break;
                }
            }
            match it.peek() {
                None => return self.fail(name),
                Some(')') => {
                    consumed += 1;
                    it.next();
                    break;
                }
                Some('\'') => {
                    consumed += 1;
                    it.next();
                    let mut value = String::new();
                    loop {
                        match it.next() {
                            None => return self.fail(name),
                            Some('\'') => {
                                consumed += 1;
                                if it.peek() == Some(&'\'') {
                                    it.next();
                                    consumed += 1;
                                    value.push('\'');
                                    continue;
                                }
                                break;
                            }
                            Some(c) => {
                                consumed += c.len_utf8();
                                value.push(c);
                            }
                        }
                    }
                    out.push(value);
                }
                Some(_) => {
                    let mut value = String::new();
                    while let Some(c) = it.peek() {
                        if *c == ',' || *c == ')' {
                            break;
                        }
                        value.push(*c);
                        consumed += c.len_utf8();
                        it.next();
                    }
                    out.push(value.trim().to_string());
                }
            }
        }

        self.rest = &self.rest[consumed..];
        Ok(out)
    }
}

/// The literal a NULL takes inside an IN-list on the wire.
pub const NULL_SENTINEL: &str = "<<NULL>>";
