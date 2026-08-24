// The native client starts the separate installed daemon when needed and owns endpoint discovery, tunnels, previews, tray, and window behavior.

mod sound;

use std::collections::{BTreeSet, HashMap};
use std::io::{BufRead, BufReader};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::webview::WebviewBuilder;
use tauri::{AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Runtime, WebviewUrl};

const LOCAL_HOST: &str = "127.0.0.1";
// Only a fallback: the daemon picks a free port at startup and publishes it.
const LOCAL_PORT: u16 = 8823;
const TRAY_ID: &str = "langmesh-tray";
const MAIN_WINDOW: &str = "main";
// The embedded native webview used to preview external websites at full browser
// fidelity (real engine, top-level navigation — X-Frame-Options never applies). It
// floats over the app's preview panel, positioned to that panel's rect by the UI.
const PREVIEW_WEBVIEW: &str = "langmesh-preview";
// Where the preview webview parks when hidden — far off-screen so it stays alive
// (scripts, media, session) without being visible. Cheaper and less flickery than
// tearing it down and rebuilding on every open/close.
const PREVIEW_OFFSCREEN: f64 = -32000.0;

struct SshTunnels(Mutex<HashMap<String, SshTunnel>>);

struct SshTunnel {
    url: String,
    child: Child,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct SshHost {
    alias: String,
    host_name: String,
    user: String,
    port: u16,
    identity_files: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SshTunnelRequest {
    profile_id: String,
    host_alias: String,
    host_name: Option<String>,
    user: Option<String>,
    port: Option<u16>,
    identity_file: Option<String>,
    local_port: Option<u16>,
    remote_port: Option<u16>,
}

// Where the daemon publishes what it is listening on. XDG_RUNTIME_DIR is the right home for
// this — the OS clears it on logout — but macOS does not set it, so the fallback is a
// per-user directory under the temporary directory.
fn runtime_directory() -> PathBuf {
    if let Some(directory) = std::env::var_os("XDG_RUNTIME_DIR") {
        let path = PathBuf::from(directory);
        if path.is_absolute() {
            return path.join("langmesh");
        }
    }
    let uid = unsafe { libc::getuid() };
    std::env::temp_dir().join(format!("langmesh-{uid}"))
}

// The daemon's loopback port and capability token, which it writes on startup. The desktop
// client needs both: a webview cannot open a unix socket, so it reaches the same API over
// loopback and proves itself with the token.
fn daemon_endpoint_files() -> (PathBuf, PathBuf) {
    let directory = runtime_directory();
    (directory.join("port"), directory.join("token"))
}

fn local_base_url() -> String {
    let (port_path, _) = daemon_endpoint_files();
    let port = std::fs::read_to_string(port_path)
        .ok()
        .and_then(|raw| raw.trim().parse::<u16>().ok())
        .unwrap_or(LOCAL_PORT);
    format!("http://{LOCAL_HOST}:{port}")
}

// Whether a daemon is actually accepting connections.
//
// A socket file left by a crashed daemon is indistinguishable from a live one by existence
// alone, so the test has to be a connection. Getting this wrong in either direction is
// costly: a false positive leaves the app talking to nothing, a false negative starts a
// second daemon on top of a healthy one.
fn daemon_is_listening() -> bool {
    let (port_path, _) = daemon_endpoint_files();
    let Ok(raw) = std::fs::read_to_string(port_path) else {
        return false;
    };
    let Ok(port) = raw.trim().parse::<u16>() else {
        return false;
    };
    let Ok(address) = format!("{LOCAL_HOST}:{port}").parse::<SocketAddr>() else {
        return false;
    };
    TcpStream::connect_timeout(&address, Duration::from_millis(300)).is_ok()
}

fn home_ssh_config_path() -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").ok_or_else(|| "HOME is not set.".to_string())?;
    Ok(PathBuf::from(home).join(".ssh").join("config"))
}

fn configured_ssh_aliases() -> Result<Vec<String>, String> {
    let path = home_ssh_config_path()?;
    if !path.exists() {
        return Ok(Vec::new());
    }
    let file = std::fs::File::open(&path)
        .map_err(|error| format!("could not read {}: {error}", path.display()))?;
    let reader = BufReader::new(file);
    let mut aliases = BTreeSet::new();
    for line in reader.lines() {
        let line = line.map_err(|error| format!("could not read {}: {error}", path.display()))?;
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let mut parts = trimmed.split_whitespace();
        let Some(keyword) = parts.next() else {
            continue;
        };
        if !keyword.eq_ignore_ascii_case("host") {
            continue;
        }
        for alias in parts {
            if !alias.contains('*') && !alias.contains('?') && alias != "!" {
                aliases.insert(alias.to_string());
            }
        }
    }
    Ok(aliases.into_iter().collect())
}

fn resolve_ssh_host(alias: &str) -> SshHost {
    let mut host = SshHost {
        alias: alias.to_string(),
        host_name: alias.to_string(),
        user: String::new(),
        port: 22,
        identity_files: Vec::new(),
    };
    let output = Command::new("ssh").arg("-G").arg(alias).output();
    let Ok(output) = output else {
        return host;
    };
    if !output.status.success() {
        return host;
    }
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let Some((key, value)) = line.split_once(' ') else {
            continue;
        };
        match key {
            "hostname" => host.host_name = value.to_string(),
            "user" => host.user = value.to_string(),
            "port" => {
                host.port = value.parse().unwrap_or(22);
            }
            "identityfile" => host.identity_files.push(value.to_string()),
            _ => {}
        }
    }
    host
}

#[tauri::command]
fn list_ssh_hosts() -> Result<Vec<SshHost>, String> {
    Ok(configured_ssh_aliases()?
        .iter()
        .map(|alias| resolve_ssh_host(alias))
        .collect())
}

fn open_local_port(requested: Option<u16>) -> Result<u16, String> {
    if let Some(port) = requested.filter(|port| *port > 0) {
        return Ok(port);
    }
    let listener = TcpListener::bind((LOCAL_HOST, 0))
        .map_err(|error| format!("could not allocate a local tunnel port: {error}"))?;
    let port = listener
        .local_addr()
        .map_err(|error| format!("could not inspect local tunnel port: {error}"))?
        .port();
    drop(listener);
    Ok(port)
}

#[tauri::command]
fn start_ssh_tunnel(
    state: tauri::State<'_, SshTunnels>,
    request: SshTunnelRequest,
) -> Result<String, String> {
    let mut guard = state.0.lock().map_err(|error| error.to_string())?;
    if let Some(existing) = guard.get_mut(&request.profile_id) {
        if matches!(existing.child.try_wait(), Ok(None)) {
            return Ok(existing.url.clone());
        }
    }
    if let Some(mut stale) = guard.remove(&request.profile_id) {
        let _ = stale.child.kill();
        let _ = stale.child.wait();
    }

    let local_port = open_local_port(request.local_port)?;
    let remote_port = request.remote_port.unwrap_or(8823);
    let url = format!("http://{LOCAL_HOST}:{local_port}");
    let mut command = Command::new("ssh");
    command
        .arg("-N")
        .arg("-o")
        .arg("ExitOnForwardFailure=yes")
        .arg("-L")
        .arg(format!("{LOCAL_HOST}:{local_port}:127.0.0.1:{remote_port}"));
    if let Some(host_name) = request.host_name.as_deref().filter(|value| !value.trim().is_empty()) {
        command.arg("-o").arg(format!("HostName={}", host_name.trim()));
    }
    if let Some(user) = request.user.as_deref().filter(|value| !value.trim().is_empty()) {
        command.arg("-l").arg(user.trim());
    }
    if let Some(port) = request.port {
        command.arg("-p").arg(port.to_string());
    }
    if let Some(identity_file) = request.identity_file.as_deref().filter(|value| !value.trim().is_empty()) {
        command.arg("-i").arg(identity_file.trim());
    }
    command.arg(&request.host_alias);
    let child = command
        .spawn()
        .map_err(|error| format!("failed to start SSH tunnel: {error}"))?;
    guard.insert(request.profile_id, SshTunnel { url: url.clone(), child });
    Ok(url)
}

fn kill_ssh_tunnels(state: &SshTunnels) {
    if let Ok(mut guard) = state.0.lock() {
        for (_, mut tunnel) in guard.drain() {
            let _ = tunnel.child.kill();
            let _ = tunnel.child.wait();
        }
    }
}

/// Whether a daemon on this machine is answering, and where.
#[tauri::command]
fn local_daemon() -> serde_json::Value {
    serde_json::json!({ "listening": daemon_is_listening(), "url": local_base_url() })
}

#[cfg(not(debug_assertions))]
fn installed_daemon_executable() -> Result<PathBuf, String> {
    let desktop_executable = std::env::current_exe()
        .map_err(|error| format!("could not locate the LangMesh desktop executable: {error}"))?;
    let mut candidates = Vec::new();
    if let Some(installation_directory) = desktop_executable
        .ancestors()
        .find(|ancestor| ancestor.extension().is_some_and(|extension| extension == "app"))
        .and_then(|bundle| bundle.parent())
    {
        candidates.push(
            installation_directory
                .join("LangMesh Computer Use.app")
                .join("Contents")
                .join("MacOS")
                .join("langmesh"),
        );
    }
    candidates.push(PathBuf::from(
        "/Applications/LangMesh Computer Use.app/Contents/MacOS/langmesh",
    ));
    candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .ok_or_else(|| {
            "the LangMesh daemon is not installed; install LangMesh Computer Use.app beside LangMesh.app"
                .to_string()
        })
}

#[cfg(not(debug_assertions))]
fn ensure_home_daemon() -> Result<(), String> {
    use std::process::Stdio;

    if daemon_is_listening() {
        return Ok(());
    }
    let executable = installed_daemon_executable()?;
    // The daemon is `langmesh langmeshd`, started detached so it outlives the app's launch.
    let mut child = Command::new(&executable)
        .arg("langmeshd")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("could not start {}: {error}", executable.display()))?;
    // Wait for the daemon to publish its endpoint, giving up if it exits or stalls.
    let deadline = std::time::Instant::now() + Duration::from_secs(20);
    loop {
        if daemon_is_listening() {
            return Ok(());
        }
        if matches!(child.try_wait(), Ok(Some(_))) {
            return Err(format!("{} exited before the daemon became ready", executable.display()));
        }
        if std::time::Instant::now() >= deadline {
            return Err("this machine's daemon did not become ready in time".to_string());
        }
        std::thread::sleep(Duration::from_millis(200));
    }
}

#[cfg(debug_assertions)]
fn ensure_home_daemon() -> Result<(), String> {
    daemon_is_listening()
        .then_some(())
        .ok_or_else(|| "start the checkout daemon before launching the development app".to_string())
}

fn read_daemon_endpoint() -> Result<serde_json::Value, String> {
    ensure_home_daemon()?;
    let (port_path, token_path) = daemon_endpoint_files();
    let port = std::fs::read_to_string(&port_path)
        .map_err(|error| format!("langmeshd has not published a port yet: {error}"))?
        .trim()
        .parse::<u16>()
        .map_err(|error| format!("langmeshd published an unreadable port: {error}"))?;
    let token = std::fs::read_to_string(&token_path)
        .map_err(|error| format!("langmeshd has not published a token yet: {error}"))?
        .trim()
        .to_string();
    Ok(serde_json::json!({ "url": format!("http://{LOCAL_HOST}:{port}"), "token": token }))
}

/// Start this machine's daemon when needed and return its authenticated loopback endpoint.
#[tauri::command]
async fn daemon_endpoint() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(read_daemon_endpoint)
        .await
        .map_err(|error| format!("could not resolve this machine's daemon endpoint: {error}"))?
}

// Quit and relaunch the app.
//
// Note what this no longer does. macOS caches the Accessibility trust check per process, so a
// daemon that was running before the grant will not see it — and the daemon is no longer this
// app's child, so restarting the window does not restart it. The settings dialog asks the
// daemon to restart itself over the control plane and then calls this to reload the webview
// against the fresh one.
#[tauri::command]
fn restart_app(app: AppHandle) {
    app.restart();
}

// The embedded native webview used to preview external websites at full browser
// fidelity, created on first use and positioned by the UI over the preview panel.

// Only ever preview real web pages; anything else is rejected so the embedded
// webview can never be pointed at a local or non-web scheme.
fn parse_preview_url(url: &str) -> Result<tauri::Url, String> {
    let parsed = tauri::Url::parse(url).map_err(|error| format!("invalid preview url: {error}"))?;
    match parsed.scheme() {
        "http" | "https" => Ok(parsed),
        other => Err(format!("unsupported preview url scheme: {other}")),
    }
}

// Show the preview webview at the given rect (logical/CSS pixels relative to the
// main window's content), creating it on first use and navigating it when the URL
// changes. The UI drives every call from the preview panel's measured bounds.
#[tauri::command]
fn artifact_show(
    app: AppHandle,
    url: String,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    let target = parse_preview_url(&url)?;
    let width = width.max(1.0);
    let height = height.max(1.0);
    if let Some(webview) = app.get_webview(PREVIEW_WEBVIEW) {
        // Re-navigate only when the destination actually changed, so repositioning
        // the panel (scroll/resize) never reloads the page.
        if webview.url().map(|current| current != target).unwrap_or(true) {
            webview.navigate(target).map_err(|error| error.to_string())?;
        }
        webview
            .set_position(LogicalPosition::new(x, y))
            .map_err(|error| error.to_string())?;
        webview
            .set_size(LogicalSize::new(width, height))
            .map_err(|error| error.to_string())?;
        return Ok(());
    }
    // A WebviewWindow is a Window plus its primary Webview; the multiwebview
    // `add_child` lives on the underlying Window, reached via `.as_ref().window()`.
    let main_window = app
        .get_webview_window(MAIN_WINDOW)
        .ok_or_else(|| "main window is not available".to_string())?;
    let window = main_window.as_ref().window();
    let builder = WebviewBuilder::new(PREVIEW_WEBVIEW, WebviewUrl::External(target));
    window
        .add_child(
            builder,
            LogicalPosition::new(x, y),
            LogicalSize::new(width, height),
        )
        .map_err(|error| error.to_string())?;
    Ok(())
}

// Move/resize the preview webview to a new rect without touching its navigation —
// called as the panel is resized or the window layout shifts.
#[tauri::command]
fn artifact_set_bounds(
    app: AppHandle,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    if let Some(webview) = app.get_webview(PREVIEW_WEBVIEW) {
        webview
            .set_position(LogicalPosition::new(x, y))
            .map_err(|error| error.to_string())?;
        webview
            .set_size(LogicalSize::new(width.max(1.0), height.max(1.0)))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

// Park the preview webview off-screen (kept alive) so the panel can close, a modal
// can cover the area, or the transcript can scroll without the native layer showing
// through.
#[tauri::command]
fn artifact_hide(app: AppHandle) -> Result<(), String> {
    if let Some(webview) = app.get_webview(PREVIEW_WEBVIEW) {
        webview
            .set_position(LogicalPosition::new(PREVIEW_OFFSCREEN, PREVIEW_OFFSCREEN))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

// Tear the preview webview down entirely (its scripts, media, and network stop).
#[tauri::command]
fn artifact_close(app: AppHandle) -> Result<(), String> {
    if let Some(webview) = app.get_webview(PREVIEW_WEBVIEW) {
        webview.close().map_err(|error| error.to_string())?;
    }
    Ok(())
}

// Build and manage the macOS tray menu with recent conversations and lifecycle commands.

// One recent conversation, pushed from the UI so the tray can list them.
#[derive(Debug, Deserialize)]
struct RecentItem {
    id: String,
    title: String,
}

// Build the tray menu, with the recent-conversations submenu populated from the
// UI's latest sessions (empty on first launch).
fn build_tray_menu<R: Runtime>(
    app: &AppHandle<R>,
    recents: &[RecentItem],
) -> tauri::Result<Menu<R>> {
    let new_chat = MenuItem::with_id(app, "new_chat", "New Chat", true, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open LangMesh", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit LangMesh", true, None::<&str>)?;
    let separator_one = PredefinedMenuItem::separator(app)?;
    let separator_two = PredefinedMenuItem::separator(app)?;

    // The recent items own their menu entries; keep them alive for the build.
    let recent_entries: Vec<MenuItem<R>> = recents
        .iter()
        .map(|item| MenuItem::with_id(app, &item.id, &item.title, true, None::<&str>))
        .collect::<tauri::Result<_>>()?;

    let recent_submenu = if recent_entries.is_empty() {
        let empty = MenuItem::with_id(app, "recent_none", "No recent conversations", false, None::<&str>)?;
        Submenu::with_items(app, "Recent Conversations", true, &[&empty])?
    } else {
        let refs: Vec<&dyn tauri::menu::IsMenuItem<R>> = recent_entries
            .iter()
            .map(|entry| entry as &dyn tauri::menu::IsMenuItem<R>)
            .collect();
        Submenu::with_items(app, "Recent Conversations", true, &refs)?
    };

    Menu::with_items(
        app,
        &[
            &new_chat as &dyn tauri::menu::IsMenuItem<R>,
            &recent_submenu,
            &separator_one,
            &open,
            &separator_two,
            &quit,
        ],
    )
}

fn show_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

// Route a tray menu click. Static ids are handled by name; anything else is a
// recent conversation's session id, forwarded to the UI to open.
fn handle_tray_menu<R: Runtime>(app: &AppHandle<R>, id: &str) {
    match id {
        "new_chat" => {
            show_main_window(app);
            let _ = app.emit("langmesh://new-chat", ());
        }
        "open" => show_main_window(app),
        "quit" => app.exit(0),
        "recent_none" => {}
        session_id => {
            show_main_window(app);
            let _ = app.emit("langmesh://open-session", session_id.to_string());
        }
    }
}

// Rebuild the tray menu with the UI's latest recent conversations.
#[tauri::command]
fn update_tray_recent(app: AppHandle, items: Vec<RecentItem>) -> Result<(), String> {
    let menu = build_tray_menu(&app, &items).map_err(|error| error.to_string())?;
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        tray.set_menu(Some(menu)).map_err(|error| error.to_string())?;
    }
    Ok(())
}

// Application entry point: register Tauri plugins, commands, and the tray.
//
// This window used to carry a SQLite database of its own, holding the colour mode, the locale
// and which workspace to reopen. It is gone: those are the daemon's, in the same database as
// the sessions, so the desktop app, a browser tab and the phone answer "what theme is LangMesh
// in" the same way instead of each holding a private copy.

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(SshTunnels(Mutex::new(HashMap::new())))
        .invoke_handler(tauri::generate_handler![
            local_daemon,
            daemon_endpoint,
            restart_app,
            list_ssh_hosts,
            start_ssh_tunnel,
            update_tray_recent,
            artifact_show,
            artifact_set_bounds,
            artifact_hide,
            artifact_close,
            sound::play_system_sound
        ])
        .setup(|app| {
            let handle = app.handle();
            let menu = build_tray_menu(handle, &[])?;
            // A dedicated monochrome, transparent TEMPLATE image for the menubar — not the
            // full-colour app icon, which renders as an opaque white square. `as_template`
            // lets macOS tint it to match the menubar (dark/light), like native items.
            let tray_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray-icon.png"))
                .expect("bundled tray icon");
            TrayIconBuilder::with_id(TRAY_ID)
                .icon(tray_icon)
                .icon_as_template(true)
                .tooltip("LangMesh")
                .menu(&menu)
                .on_menu_event(|app, event| handle_tray_menu(app, event.id().as_ref()))
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // The close button hides the window and keeps the app alive in the dock
            // and menu bar (with the local server still running). Only an explicit
            // Quit — Cmd+Q or the tray's Quit — actually terminates.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the LangMesh desktop app")
        .run(|app_handle, event| match event {
            // Clicking the dock icon while hidden brings the window back.
            tauri::RunEvent::Reopen { .. } => show_main_window(app_handle),
            // A real quit closes the tunnels this app opened; the independent daemon outlives the window.
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit => {
                if let Some(state) = app_handle.try_state::<SshTunnels>() {
                    kill_ssh_tunnels(&state);
                }
            }
            _ => {}
        });
}
