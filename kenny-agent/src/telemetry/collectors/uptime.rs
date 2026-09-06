//! `uptime` section — system uptime / boot time. Portable via `sysinfo`.

use serde_json::json;
use sysinfo::System;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Uptime in seconds beyond which we surface a `warn` (nudge a reboot).
/// Collect the `uptime` section.
pub fn collect() -> Section {
    section_for(System::uptime(), System::boot_time())
}

/// Report the uptime; do not grade it. Whether a month without a reboot is a
/// finding depends on the OS (Windows applies updates on reboot, a Linux
/// server does not care) and is the server's call
/// (`health_rules._rule_uptime`), so the status here is always `Ok`.
fn section_for(uptime_secs: u64, boot_time_unix: u64) -> Section {
    let days = uptime_secs / 86_400;
    let hours = (uptime_secs % 86_400) / 3_600;
    Section::with_fields(
        Status::Ok,
        format!("up {days}d {hours}h"),
        json!({
            "uptime_secs": uptime_secs,
            "boot_time_unix": boot_time_unix,
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uptime_section_is_valid() {
        let v = collect().into_value();
        assert!(v["uptime_secs"].as_u64().is_some());
    }

    #[test]
    fn uptime_never_grades_the_host() {
        // Report, do not grade (ADR-0058): a month without a reboot is the
        // server's call (`health_rules._rule_uptime`), OS-dependent.
        let v = section_for(120 * 86_400 + 3_600, 0).into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "up 120d 1h");
        assert_eq!(v["uptime_secs"], 120 * 86_400 + 3_600);
    }
}
