// MyPlow desktop shell.
//
// The product is already a set of daemons serving HTML over loopback, so this binary is
// deliberately thin: it carries the runtime Homebrew used to provide, runs the CLI that
// already knows how to start everything, and points one window at the board.
//
// It does NOT reimplement any of `mypeople up`. supervise.sh owns the seven daemons and
// respawns them; duplicating that here would be a second supervisor to keep in sync.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::env;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;

use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder};

/// A `Command` for the carried interpreter, with the carried runtime ahead of everything.
///
/// PATH is the whole integration. supervise.sh, mp and the daemons all invoke bare `python3`,
/// `ttyd` and `tmux`, so putting our bin directories first is what makes the app self-contained
/// without touching a line of the runtime.
///
/// The tail matters as much as the head. A double-clicked .app inherits a minimal PATH, and the
/// user's own AI CLI — the `claude` that firstrun's auth check shells out to — is not in it:
/// claude.ai/install.sh puts it in ~/.local/bin. Without these entries the app finds no login on
/// a machine that has one and opens permanently in "you are not logged in" mode. They come after
/// our own directories, so what we carry still wins; Homebrew is here only to find the user's
/// CLI if that is how they installed it, never to satisfy anything we ship.
fn python(res: &Path) -> Command {
    let rt = res.join("runtime");
    let home = env::var("HOME").unwrap_or_default();
    let mut cmd = Command::new(rt.join("python/bin/python3"));
    cmd.env(
        "PATH",
        format!(
            "{}:{}:{}/.local/bin:{}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            rt.join("bin").display(),
            rt.join("python/bin").display(),
            home,
            home,
        ),
    )
    .env("PYTHONPATH", rt.join("pylib"))
    // Resources/ inside an installed .app is not writable by the user running it.
    .env("PYTHONDONTWRITEBYTECODE", "1")
    // Take the degraded "board + HUD only, no login yet" path instead of sys.exit(2): a
    // double-clicked app has no terminal to refuse into, and the login it wants is reachable
    // from the terminal tab inside the window. See firstrun.ensure().
    .env("MYPEOPLE_DESKTOP", "1")
    .stdin(Stdio::null());
    cmd
}

/// Ask the runtime where the board is rather than hardcoding 9933 here.
///
/// The ports are configurable (TODO_PORT/HUD_PORT in queue.env) and a second copy of that
/// resolution in Rust would be a copy that drifts. `cli.urls()` is already the one answer.
fn board_url(res: &Path) -> String {
    const FALLBACK: &str = "http://localhost:9933";
    let out = python(res)
        .args([
            "-c",
            "from mypeople import cli; print(cli.urls(cli.load_cfg())['board'])",
        ])
        .output();
    match out {
        Ok(o) if o.status.success() => {
            let url = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if url.starts_with("http") {
                url
            } else {
                FALLBACK.into()
            }
        }
        _ => FALLBACK.into(),
    }
}

/// Bring the stack up. `--detach` returns once the HUD answers /health (or after its own 40s
/// warning), so this is the point where the board is ready to be shown.
fn start_runtime(res: &Path) {
    match python(res)
        .args(["-m", "mypeople.cli", "up", "--detach"])
        .status()
    {
        Ok(s) if s.success() => {}
        Ok(s) => eprintln!("[myplow] `mypeople up` exited {s}"),
        Err(e) => eprintln!("[myplow] could not start the runtime: {e}"),
    }
}

/// Stop the daemons on quit.
///
/// Leaving seven daemons and a tmux server running after the window is gone would strand the
/// user with no way to stop them except the terminal this app exists to remove.
fn stop_runtime(res: &Path) {
    let _ = python(res).args(["-m", "mypeople.cli", "down"]).status();
}

/// Loopback is our own daemons: the ttyd terminals and the board. Anything else is the internet.
fn is_local(url: &Url) -> bool {
    matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "0.0.0.0" | "::1" | "[::1]"))
}

/// Every `window.open(...)` and `target="_blank"` link in the pages lands here.
///
/// WKWebView silently drops a new-window request unless the app handles it, so without this
/// the click on an agent's name — which opens its live terminal — did nothing in the app while
/// it opened a tab in a browser. It is handled once, here, for every such link the pages have
/// (agent names, proof links, links inside comments) instead of patching each caller.
///
/// Our own terminals open as a new MyPlow window, the app's equivalent of the browser tab.
/// Links to the outside world (a GitHub PR in a comment) go to the default browser via macOS's
/// own `open`, where the user's logins live; rendering GitHub inside MyPlow would be a browser
/// without their session.
fn open_new_window(app: &AppHandle, url: Url) -> NewWindowResponse<tauri::Wry> {
    if !is_local(&url) {
        if matches!(url.scheme(), "http" | "https" | "mailto") {
            let _ = Command::new("/usr/bin/open").arg(url.as_str()).status();
        }
        return NewWindowResponse::Deny;
    }
    // A window of our own, pointed straight at the URL, rather than handing WebKit a popup to
    // load into. A popup has to be built on the opener's exact webview configuration (macOS
    // requires it) and inherits the page's window.open() features; an ordinary window has
    // neither constraint, and the terminal page never uses window.opener, so declining the
    // popup loses nothing.
    static N: AtomicUsize = AtomicUsize::new(0);
    let label = format!("term-{}", N.fetch_add(1, Ordering::Relaxed));
    let _ = WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
        .title("MyPlow")
        .inner_size(1100.0, 720.0)
        .on_document_title_changed(|w, title| {
            let _ = w.set_title(&title);
        })
        .build();
    NewWindowResponse::Deny
}

fn resources(app: &tauri::AppHandle) -> PathBuf {
    app.path()
        .resource_dir()
        .expect("resource dir is always present in a bundled app")
}

fn main() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            let res = resources(&handle);

            // Built here rather than in tauri.conf.json: only a window made in code can carry the
            // new-window handler, and without it every link that opens a new tab is dead.
            let opener = handle.clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("MyPlow")
                .inner_size(1280.0, 860.0)
                .min_inner_size(900.0, 600.0)
                .center()
                .on_new_window(move |url, _features| open_new_window(&opener, url))
                .build()?;

            // Off the main thread: first run materializes the install and can take seconds,
            // and blocking here would mean a beachball instead of the splash.
            thread::spawn(move || {
                start_runtime(&res);
                let url = board_url(&res);
                let win = handle.clone();
                let _ = handle.run_on_main_thread(move || {
                    if let Some(w) = win.get_webview_window("main") {
                        if let Ok(parsed) = url.parse() {
                            let _ = w.navigate(parsed);
                        }
                    }
                });
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build MyPlow");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            stop_runtime(&resources(handle));
        }
    });
}
