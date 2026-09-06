//! `shell_exec` — run a POSIX command and return stdout/stderr/exit_code.
//!
//! Off Windows this shells out to `sh -c <command>`. On Windows it is `unsupported` —
//! this tool's OS-scoped mirror is `powershell_exec` (`handlers::powershell`).

use serde::Deserialize;
use serde_json::Value;

use crate::protocol::ErrorCode;

#[derive(Debug, Deserialize)]
#[cfg_attr(windows, allow(dead_code))] // fields unused by the `unsupported` on-Windows stub
struct Args {
    command: String,
    #[serde(default)]
    timeout_s: Option<u64>,
}

/// Execute the requested command, honouring `timeout_s` if present.
pub async fn exec(args: Value) -> Result<Value, (ErrorCode, String)> {
    let args: Args = serde_json::from_value(args)
        .map_err(|e| (ErrorCode::BadArgs, format!("invalid shell_exec args: {e}")))?;

    run(&args).await
}

#[cfg(not(windows))]
async fn run(args: &Args) -> Result<Value, (ErrorCode, String)> {
    use serde_json::json;
    use std::time::Duration;
    use tokio::process::Command;

    let mut cmd = Command::new("sh");
    cmd.args(["-c", &args.command]);
    // A timed-out call abandons the `output()` future; without this the child survives
    // it and keeps doing whatever the caller asked us to stop. See the same call in
    // `handlers::powershell`, where abandoning it also pins a tokio reaper thread.
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

#[cfg(windows)]
async fn run(_args: &Args) -> Result<Value, (ErrorCode, String)> {
    Err((
        ErrorCode::Unsupported,
        "shell_exec is not supported on Windows; use powershell_exec instead".to_string(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[cfg(not(windows))]
    #[tokio::test]
    async fn echoes_stdout_via_sh() {
        let result = exec(json!({"command": "printf hi"})).await.unwrap();
        assert_eq!(result["stdout"], "hi");
        assert_eq!(result["exit_code"], 0);
    }

    #[tokio::test]
    async fn rejects_bad_args() {
        let err = exec(json!({"nope": 1})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::BadArgs);
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn times_out() {
        let err = exec(json!({"command": "sleep 5", "timeout_s": 1}))
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
    #[cfg(not(windows))]
    #[tokio::test]
    async fn timeout_kills_the_child_instead_of_abandoning_it() {
        let marker =
            std::env::temp_dir().join(format!("kenny-shell-timeout-{}", std::process::id()));
        let _ = std::fs::remove_file(&marker);

        let err = exec(json!({
            "command": format!("sleep 2; : > '{}'", marker.display()),
            "timeout_s": 1,
        }))
        .await
        .unwrap_err();
        assert_eq!(err.0, ErrorCode::Timeout);

        // Outlast the command's own sleep, so a surviving child would have written by now.
        tokio::time::sleep(std::time::Duration::from_secs(3)).await;
        let survived = marker.exists();
        let _ = std::fs::remove_file(&marker);
        assert!(
            !survived,
            "the timed-out command kept running after we gave up on it"
        );
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn unsupported_on_windows() {
        let err = exec(json!({"command": "echo hi"})).await.unwrap_err();
        assert_eq!(err.0, ErrorCode::Unsupported);
    }
}
