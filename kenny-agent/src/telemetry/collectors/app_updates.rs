//! `app_updates` section — count of available application upgrades.
//!
//! Real data from `winget upgrade` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `app_updates` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(
            Status::Ok,
            "n/a on this platform",
            json!({ "available": 0, "packages": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Parse `winget upgrade` into `{id, name, version, available}` rows; `warn`
    /// when upgrades are pending.
    pub fn collect() -> Section {
        // Through `winps::run_command` rather than a bare `Command`: it is
        // success-gated the same way, and it holds every probe to
        // `winps::PROBE_BUDGET`. `winget` is the slowest and least predictable
        // program any collector shells out to — it refreshes its sources over the
        // network and can sit there indefinitely on a host with no usable source —
        // and an unbounded probe here would pin one collector-pool worker for as
        // long as it takes, which is the one thing the pool's budget exists to
        // prevent. A killed probe reads as "unavailable", like any other failure.
        let raw = match winps::run_command(
            "winget",
            &["upgrade", "--include-unknown", "--accept-source-agreements"],
        ) {
            Some(raw) => raw,
            None => {
                return Section::with_fields(
                    Status::Ok,
                    "winget upgrade unavailable",
                    json!({ "available": 0, "packages": [] }),
                );
            }
        };

        let packages = crate::handlers::winget::parse_table(&raw);
        let available = packages.len();
        let (status, summary) = if available == 0 {
            (Status::Ok, "0 updates available".to_string())
        } else {
            (Status::Warn, format!("{available} app update(s) available"))
        };
        Section::with_fields(
            status,
            summary,
            json!({ "available": available, "packages": packages }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_updates_section_is_valid() {
        assert!(collect().into_value()["packages"].is_array());
    }
}
