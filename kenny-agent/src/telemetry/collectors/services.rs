//! `services` section — service inventory.
//!
//! Real data comes from `Get-Service`/CIM on Windows and from `systemctl` on
//! Linux. On other platforms we stub to `n/a`; the section shape
//! (`services: [{name, display, status, start}]`) matches the `diag_services`
//! tool result.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `services` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(target_os = "linux")]
    {
        linux_impl::collect()
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        Section::with_fields(
            Status::Ok,
            "n/a on this platform",
            json!({ "services": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Enumerate services via `Win32_Service` into `{name, display, status, start}`;
    /// `warn` when an auto-start service is Stopped.
    pub fn collect() -> Section {
        let script = r#"
Get-CimInstance -ClassName Win32_Service | ForEach-Object {
  [pscustomobject]@{
    name    = [string]$_.Name
    display = [string]$_.DisplayName
    status  = [string]$_.State
    start   = [string]$_.StartMode
  }
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Ok,
                "services unavailable",
                json!({ "services": [] }),
            );
        };
        let services = winps::as_array(v);
        let (status, summary) = grade(&services);
        Section::with_fields(status, summary, json!({ "services": services }))
    }
}

/// Summarize the inventory without grading it: the count of auto-start
/// services that are not running is reported in the summary, and the status
/// is always `Ok`. Whether an idle auto-start service is a finding is the
/// server's call (`health_rules._rule_services`) -- on a real PC it is almost
/// always an updater or trigger-start service idling by design, which the
/// old `Warn` here turned into a standing alarm the server could never lower.
#[cfg(windows)]
fn grade(services: &[Value]) -> (Status, String) {
    let stalled = services
        .iter()
        .filter(|s| {
            let start = s.get("start").and_then(Value::as_str).unwrap_or("");
            let state = s.get("status").and_then(Value::as_str).unwrap_or("");
            start.to_ascii_lowercase().starts_with("auto") && !state.eq_ignore_ascii_case("Running")
        })
        .count();
    let total = services.len();
    if stalled == 0 {
        (
            Status::Ok,
            format!("{total} services, all auto-start running"),
        )
    } else {
        (
            Status::Ok,
            format!("{total} services, {stalled} auto-start not running"),
        )
    }
}

#[cfg(target_os = "linux")]
mod linux_impl {
    use super::*;
    use std::process::Command;

    /// Collect `services` from systemd via `systemctl`. When systemd is absent
    /// (no D-Bus, container/CI sandbox), degrade to the portable `n/a` stub
    /// rather than reporting a fault.
    pub fn collect() -> Section {
        let output = Command::new("systemctl")
            .args([
                "list-units",
                "--type=service",
                "--state=failed",
                "--no-legend",
                "--plain",
                "--no-pager",
            ])
            .output();

        let Some(raw) = output.ok().filter(|o| o.status.success()).and_then(|o| {
            // A running-but-bus-down systemctl exits non-zero, but guard the
            // stderr signature too in case a wrapper masks the exit code.
            let err = String::from_utf8_lossy(&o.stderr);
            if bus_unavailable(&err) {
                None
            } else {
                String::from_utf8(o.stdout).ok()
            }
        }) else {
            return Section::with_fields(
                Status::Ok,
                "n/a on this platform",
                json!({ "services": [] }),
            );
        };

        let failed = parse_failed_units(&raw);
        let services: Vec<_> = failed
            .iter()
            .map(|name| {
                json!({
                    "name": name,
                    "display": name,
                    "status": "failed",
                    "start": "",
                })
            })
            .collect();

        // Report, do not grade: a failed unit is a finding for the server's
        // `health_rules._rule_services` to make, not this binary's.
        let summary = if failed.is_empty() {
            "all units healthy".to_string()
        } else {
            format!("{} failed unit(s)", failed.len())
        };
        Section::with_fields(Status::Ok, summary, json!({ "services": services }))
    }

    /// Whether `systemctl` stderr indicates the systemd bus is unreachable.
    fn bus_unavailable(stderr: &str) -> bool {
        let s = stderr.to_lowercase();
        s.contains("has not been booted") || s.contains("failed to connect to bus")
    }

    /// Extract failed unit names — the first whitespace-delimited token of each
    /// nonempty line of `systemctl list-units ... --no-legend --plain`.
    fn parse_failed_units(raw: &str) -> Vec<String> {
        raw.lines()
            .filter_map(|line| line.split_whitespace().next())
            .filter(|tok| !tok.is_empty())
            .map(str::to_string)
            .collect()
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn parses_failed_units_first_token() {
            let raw = "  nginx.service loaded failed failed A high performance web server\n\
                       docker.service loaded failed failed Docker Application Container Engine\n";
            let units = parse_failed_units(raw);
            assert_eq!(units, vec!["nginx.service", "docker.service"]);
        }

        #[test]
        fn parses_empty_output_as_no_failures() {
            assert!(parse_failed_units("").is_empty());
            assert!(parse_failed_units("\n  \n").is_empty());
        }

        #[test]
        fn detects_bus_down_stderr() {
            assert!(bus_unavailable(
                "System has not been booted with systemd as init system"
            ));
            assert!(bus_unavailable("Failed to connect to bus: No such file"));
            assert!(!bus_unavailable(""));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn services_section_is_valid() {
        let v = collect().into_value();
        assert!(v["services"].is_array());
        // Whatever platform path runs, the section must classify itself.
        assert!(v["status"].is_string());
    }

    #[test]
    fn services_never_grades_the_host() {
        // Report, do not grade (ADR-0058): whether an idle auto-start service or
        // a failed unit is a finding is `health_rules._rule_services`' call.
        assert_eq!(collect().into_value()["status"], "ok");
        #[cfg(windows)]
        {
            let stalled = vec![
                serde_json::json!({"name": "edgeupdate", "start": "Auto", "status": "Stopped"}),
            ];
            let (status, summary) = grade(&stalled);
            assert_eq!(status, Status::Ok);
            assert!(summary.contains("1 auto-start not running"));
        }
    }
}
