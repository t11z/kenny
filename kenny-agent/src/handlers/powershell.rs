//! `powershell_exec` — run a script and return stdout/stderr/exit_code.
//!
//! On Windows this shells out to `powershell.exe -NoProfile -Command <script>`. It is
//! `unsupported` off Windows — this tool's OS-scoped mirror is `shell_exec`
//! (`handlers::shell`), which runs the POSIX equivalent via `sh -c` there.

use serde::Deserialize;
use serde_json::Value;

use crate::protocol::ErrorCode;

#[derive(Debug, Deserialize)]
#[cfg_attr(not(windows), allow(dead_code))] // fields unused by the `unsupported` off-Windows stub
struct Args {
    script: String,
    #[serde(default)]
    timeout_s: Option<u64>,
}

/// Execute the requested script, honouring `timeout_s` if present.
pub async fn exec(args: Value) -> Result<Value, (ErrorCode, String)> {
    let args: Args = serde_json::from_value(args).map_err(|e| {
        (
            ErrorCode::BadArgs,
            format!("invalid powershell_exec args: {e}"),
        )
    })?;

    run(&args).await
}

#[cfg(windows)]
async fn run(args: &Args) -> Result<Value, (ErrorCode, String)> {
    use serde_json::json;
    use std::time::Duration;
    use tokio::process::Command;

    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-NonInteractive", "-Command", &args.script]);
    // The timeout below abandons the `output()` future, and an abandoned child is not
    // killed unless we ask for it. On Windows tokio reaps each child on a blocking
    // wait thread that the runtime's shutdown joins, so an abandoned child holds that
    // thread — and the runtime — for as long as the real process keeps running,
    // however long after we reported `timeout` that is. `kill_on_drop` ends the child
    // with the future, which is also what the caller means by a timeout: stop the
    // work, not just stop waiting for it.
    cmd.kill_on_drop(true);
    let fut = cmd.output();

    let output = match args.timeout_s {
        Some(secs) => match tokio::time::timeout(Duration::from_secs(secs), fut).await {
            Ok(res) => res,
            Err(_) => return Err((ErrorCode::Timeout, format!("tool exceeded {secs}s"))),
        },
        None => fut.await,
    }
    .map_err(|e| (ErrorCode::ExecFailed, format!("failed to spawn shell: {e}")))?;

    Ok(json!({
        "stdout": String::from_utf8_lossy(&output.stdout),
        "stderr": String::from_utf8_lossy(&output.stderr),
        "exit_code": output.status.code().unwrap_or(-1),
    }))
}

#[cfg(not(windows))]
async fn run(_args: &Args) -> Result<Value, (ErrorCode, String)> {
    Err((
        ErrorCode::Unsupported,
        "powershell_exec is not supported off Windows; use shell_exec instead".to_string(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn rejects_bad_args() {
        let err = exec(json!({"nope": 1})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn echoes_stdout() {
        let result = exec(json!({"script": "Write-Output hi"})).await.unwrap();
        assert_eq!(result["stdout"].as_str().unwrap().trim(), "hi");
        assert_eq!(result["exit_code"], 0);
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn times_out() {
        let err = exec(json!({"script": "Start-Sleep -Seconds 5", "timeout_s": 1}))
            .await
            .unwrap_err();
        assert_eq!(err.0, ErrorCode::Timeout);
    }

    /// A timeout must stop the work, not just stop waiting for it.
    ///
    /// Without `kill_on_drop` the abandoned child runs to completion behind our back:
    /// the caller is told `timeout` while the command it asked us to stop carries on.
    /// On Windows that also pins the tokio reaper thread the runtime's shutdown joins,
    /// which is how one wedged child can hold a whole test binary open. The marker file
    /// is written only by the half of the command that runs *after* the timeout fires,
    /// so its absence is the proof the child died with the future.
    #[cfg(windows)]
    #[tokio::test]
    async fn timeout_kills_the_child_instead_of_abandoning_it() {
        let marker = std::env::temp_dir().join(format!("kenny-ps-timeout-{}", std::process::id()));
        let _ = std::fs::remove_file(&marker);

        let err = exec(json!({
            "script": format!(
                "Start-Sleep -Seconds 2; New-Item -ItemType File -Path '{}' | Out-Null",
                marker.display()
            ),
            "timeout_s": 1,
        }))
        .await
        .unwrap_err();
        assert_eq!(err.0, ErrorCode::Timeout);

        // Outlast the script's own sleep, so a surviving child would have written by now.
        tokio::time::sleep(std::time::Duration::from_secs(3)).await;
        let survived = marker.exists();
        let _ = std::fs::remove_file(&marker);
        assert!(
            !survived,
            "the timed-out script kept running after we gave up on it"
        );
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn unsupported_off_windows() {
        let err = exec(json!({"script": "echo hi"})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::Unsupported);
    }
}
