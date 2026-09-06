//! Shared local-IPC framing for the session-0 service ⇄ user-session tray pipes.
//!
//! The agent runs as a LocalSystem service in **session 0** (no visible desktop), while
//! the tray helper (`kenny-agent tray`) runs in the interactive user session. They talk
//! over local **named pipes** — one for screen capture ([`crate::screencap_ipc`],
//! ADR-0018) and one for launching remote-help apps ([`crate::session_launch_ipc`],
//! ADR-0021). Both use the same wire framing defined here.
//!
//! Framing is a single length-prefixed blob: a `u32` little-endian length, then that many
//! payload bytes. The helpers are platform-neutral so they can be unit-tested on Linux;
//! the pipe servers/clients that drive them are Windows-only.

use std::io::{self, Read, Write};

/// Upper bound on a single frame's payload. The largest real payload on these pipes is a
/// full-desktop PNG screenshot ([`crate::screencap_ipc`]); 128 MiB is generous headroom
/// above that while still ruling out a peer's bogus length prefix (up to `u32::MAX`, ~4
/// GiB) forcing a huge upfront `vec![0u8; len]` allocation before a single payload byte
/// is validated.
#[cfg_attr(not(windows), allow(dead_code))]
const MAX_FRAME_LEN: usize = 128 * 1024 * 1024;

/// Read a length-prefixed frame (`u32` LE length + payload) from `r`.
///
/// Off Windows the pipe server/client are compiled out, so the only callers are the unit
/// tests; `allow(dead_code)` keeps the portable `cargo build` clean.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn read_frame<R: Read>(r: &mut R) -> io::Result<Vec<u8>> {
    let mut len_buf = [0u8; 4];
    r.read_exact(&mut len_buf)?;
    let len = u32::from_le_bytes(len_buf) as usize;
    if len > MAX_FRAME_LEN {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("frame length {len} exceeds {MAX_FRAME_LEN} byte limit"),
        ));
    }
    let mut buf = vec![0u8; len];
    r.read_exact(&mut buf)?;
    Ok(buf)
}

/// Write `payload` as a length-prefixed frame (`u32` LE length + payload) to `w`.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn write_frame<W: Write>(w: &mut W, payload: &[u8]) -> io::Result<()> {
    let len: u32 = payload
        .len()
        .try_into()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "frame too large"))?;
    w.write_all(&len.to_le_bytes())?;
    w.write_all(payload)?;
    w.flush()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn frame_round_trips() {
        let payload = b"\x89PNG\r\n\x1a\n some bytes".to_vec();
        let mut buf = Vec::new();
        write_frame(&mut buf, &payload).unwrap();
        // Length prefix is little-endian u32.
        assert_eq!(&buf[..4], &(payload.len() as u32).to_le_bytes());
        let mut cur = Cursor::new(buf);
        let back = read_frame(&mut cur).unwrap();
        assert_eq!(back, payload);
    }

    #[test]
    fn empty_frame_round_trips() {
        let mut buf = Vec::new();
        write_frame(&mut buf, &[]).unwrap();
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur).unwrap().is_empty());
    }

    #[test]
    fn truncated_frame_errors() {
        // Claims 10 bytes but provides none.
        let mut buf = 10u32.to_le_bytes().to_vec();
        buf.truncate(4);
        let mut cur = Cursor::new(buf);
        assert!(read_frame(&mut cur).is_err());
    }

    #[test]
    fn oversized_length_prefix_errors_without_allocating() {
        // A peer claiming a ~4 GiB payload must be rejected by the length check, not
        // turned into a `vec![0u8; len]` allocation attempt.
        let mut cur = Cursor::new(u32::MAX.to_le_bytes().to_vec());
        assert!(read_frame(&mut cur).is_err());

        let mut cur = Cursor::new((MAX_FRAME_LEN as u32 + 1).to_le_bytes().to_vec());
        assert!(read_frame(&mut cur).is_err());
    }
}
