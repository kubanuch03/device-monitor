// Device Monitor - программа для Linux: окно панели, значок в трее,
// уведомления рабочего стола. Сам мониторинг - отдельный сервис
// (device-monitor-server, systemd --user), программа его показывает.
//
// Закрытие окна прячет его в трей, а не завершает программу: уведомления о
// падениях должны приходить и при закрытом окне. Выход - из меню значка.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashSet;
use std::fs;
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use serde::Deserialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_notification::NotificationExt;

const POLL_SECONDS: u64 = 30;
const MENU_LIMIT: usize = 15;

// -- настройки -------------------------------------------------------------------

#[derive(Deserialize, Default)]
struct FileConfig {
    url: Option<String>,
    token_file: Option<String>,
}

struct Config {
    url: String,
    token_file: PathBuf,
}

fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

/// ~/.config/device-monitor/app.json, если есть: {"url": ..., "token_file": ...}.
/// Нужен, когда панель работает не из .deb, а, например, в Docker.
fn load_config() -> Config {
    let path = home().join(".config/device-monitor/app.json");
    let file: FileConfig = fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default();
    Config {
        url: file
            .url
            .unwrap_or_else(|| "http://127.0.0.1:8890".into())
            .trim_end_matches('/')
            .to_string(),
        token_file: file
            .token_file
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".local/share/device-monitor/data/tray-token")),
    }
}

// -- сводка от сервиса -----------------------------------------------------------

#[derive(Deserialize, Clone)]
struct DownDevice {
    id: String,
    name: String,
    site: String,
    host: String,
    since: Option<String>,
    muted: bool,
}

#[derive(Deserialize)]
struct Summary {
    total: u32,
    up: u32,
    down: Vec<DownDevice>,
    #[serde(default = "yes")]
    ready: bool,
    confirm_seconds: i64,
}

fn yes() -> bool {
    true
}

fn fetch(config: &Config) -> Result<Summary, String> {
    let token = fs::read_to_string(&config.token_file)
        .map_err(|_| "нет ключа трея".to_string())?;
    ureq::get(&format!("{}/api/v1/tray", config.url))
        .set("X-Tray-Token", token.trim())
        .timeout(Duration::from_secs(10))
        .call()
        .map_err(|_| "сервис не отвечает".to_string())?
        .into_string()
        .ok()
        .and_then(|body| serde_json::from_str::<Summary>(&body).ok())
        .ok_or_else(|| "непонятный ответ сервиса".to_string())
}

/// Сколько секунд устройство лежит (since - ISO-время перехода в down).
fn lasting(device: &DownDevice) -> i64 {
    device
        .since
        .as_deref()
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|t| (chrono::Utc::now() - t.with_timezone(&chrono::Utc)).num_seconds())
        .unwrap_or(0)
}

// -- уведомления: те же правила, что у Telegram ------------------------------------

struct NotifyState {
    first: bool,
    notified: HashSet<String>,
}

fn notify(app: &AppHandle, state: &mut NotifyState, summary: &Summary) {
    // Падение - только после окна подтверждения: одна потерянная проверка
    // через VPN не повод для уведомления.
    let confirmed: Vec<&DownDevice> = summary
        .down
        .iter()
        .filter(|d| !d.muted && lasting(d) >= summary.confirm_seconds)
        .collect();
    let confirmed_ids: HashSet<String> = confirmed.iter().map(|d| d.id.clone()).collect();
    if state.first {
        // Что лежало до запуска программы - не новость.
        state.notified = confirmed_ids;
        state.first = false;
        return;
    }
    let fresh: Vec<&&DownDevice> = confirmed.iter().filter(|d| !state.notified.contains(&d.id)).collect();
    let current: HashSet<String> = summary.down.iter().map(|d| d.id.clone()).collect();
    let back = state.notified.difference(&current).count();

    if !fresh.is_empty() {
        let mut body: Vec<String> = fresh
            .iter()
            .take(8)
            .map(|d| format!("{} · {} ({})", d.site, d.name, d.host))
            .collect();
        if fresh.len() > 8 {
            body.push(format!("… и ещё {}", fresh.len() - 8));
        }
        let _ = app
            .notification()
            .builder()
            .title(format!("Не отвечают: {}", fresh.len()))
            .body(body.join("\n"))
            .show();
    }
    if back > 0 {
        let _ = app
            .notification()
            .builder()
            .title(format!("Снова на связи: {}", back))
            .show();
    }
    state.notified = state.notified.intersection(&current).cloned().collect();
    state.notified.extend(confirmed_ids);
}

// -- окно и трей -------------------------------------------------------------------

fn show_main(app: &AppHandle, hash: &str) {
    if let Some(window) = app.get_webview_window("main") {
        if !hash.is_empty() {
            let url = format!("{}/{}", load_config().url, hash);
            let literal = serde_json::to_string(&url).unwrap_or_default();
            let _ = window.eval(&format!("location.href = {literal}"));
        }
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn tray_menu(app: &AppHandle, summary: Result<&Summary, &str>) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::new(app)?;
    match summary {
        Err(error) => {
            menu.append(&MenuItem::with_id(app, "status", format!("Нет связи с сервисом: {error}"), false, None::<&str>)?)?;
        }
        Ok(s) => {
            menu.append(&MenuItem::with_id(app, "status", format!("На связи {} из {}", s.up, s.total), false, None::<&str>)?)?;
            if !s.down.is_empty() {
                menu.append(&MenuItem::with_id(app, "down", format!("Не отвечают: {}", s.down.len()), true, None::<&str>)?)?;
                for (i, d) in s.down.iter().take(MENU_LIMIT).enumerate() {
                    let mark = if d.muted { "🔕 " } else { "" };
                    menu.append(&MenuItem::with_id(app, format!("down-{i}"), format!("   {mark}{} · {}", d.site, d.name), true, None::<&str>)?)?;
                }
                if s.down.len() > MENU_LIMIT {
                    menu.append(&MenuItem::with_id(app, "down-more", format!("   … ещё {}", s.down.len() - MENU_LIMIT), true, None::<&str>)?)?;
                }
            }
        }
    }
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&MenuItem::with_id(app, "open", "Открыть панель", true, None::<&str>)?)?;
    menu.append(&MenuItem::with_id(app, "quit", "Выход", true, None::<&str>)?)?;
    Ok(menu)
}

fn poll_loop(app: AppHandle) {
    let mut state = NotifyState { first: true, notified: HashSet::new() };
    loop {
        let config = load_config();
        let result = fetch(&config);
        if let Some(tray) = app.tray_by_id("main") {
            let menu = match &result {
                Ok(s) => tray_menu(&app, Ok(s)),
                Err(e) => tray_menu(&app, Err(e.as_str())),
            };
            if let Ok(menu) = menu {
                let _ = tray.set_menu(Some(menu));
            }
            let tooltip = match &result {
                Ok(s) if s.down.is_empty() => "Device Monitor: все на связи".to_string(),
                Ok(s) => format!("Device Monitor: не отвечают {}", s.down.len()),
                Err(e) => format!("Device Monitor: {e}"),
            };
            let _ = tray.set_tooltip(Some(tooltip));
        }
        // Сразу после рестарта сервиса круга ещё не было: пустой список
        // упавших значит «не знаем», а не «все вернулись».
        if let Ok(summary) = &result {
            if summary.ready {
                notify(&app, &mut state, summary);
            }
        }
        thread::sleep(Duration::from_secs(POLL_SECONDS));
    }
}

fn open_external(url: &str) {
    let _ = std::process::Command::new("xdg-open").arg(url).spawn();
}

fn percent_encode(value: &str) -> String {
    value
        .bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b':' | b'/' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Второй запуск поднимает уже открытое окно.
            show_main(app, "");
        }))
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            let config = load_config();
            let start = format!("index.html?target={}", percent_encode(&config.url));
            let panel = config.url.clone();
            // --tray: автозапуск при входе в систему - только значок, без окна.
            let in_tray = std::env::args().any(|a| a == "--tray");
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App(start.into()))
                .visible(!in_tray)
                // Камеры и видео (window.open / target=_blank) - окнами самой
                // программы. Без этого обработчика WebKitGTK молча гасит
                // всплывающие окна, и «Открыть» у устройства не работало бы.
                // Allow сохраняет связь с открывшим окном: автовход «открыть
                // пустое окно, затем подставить адрес с логином» работает.
                .on_new_window(|url, _features| {
                    if matches!(url.scheme(), "http" | "https" | "about") {
                        NewWindowResponse::Allow
                    } else {
                        open_external(url.as_str());
                        NewWindowResponse::Deny
                    }
                })
                // Главное окно не уходит со страницы панели: адресной строки и
                // «назад» нет, вернуться было бы нельзя. Чужие адреса и
                // devmon:// (профиль браузера точки) - системе.
                .on_navigation(move |url| {
                    let allowed = url.scheme() == "tauri"
                        || url.host_str() == Some("tauri.localhost")
                        || url.as_str().starts_with(&panel);
                    if !allowed {
                        open_external(url.as_str());
                    }
                    allowed
                })
                .title("Device Monitor")
                .inner_size(1360.0, 860.0)
                .min_inner_size(720.0, 480.0)
                .build()?;

            let menu = tray_menu(app.handle(), Err("ещё не опрошено"))?;
            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().cloned().expect("иконка программы"))
                .tooltip("Device Monitor")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "quit" => app.exit(0),
                    "open" => show_main(app, ""),
                    id if id.starts_with("down") => show_main(app, "#down=1"),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        show_main(tray.app_handle(), "");
                    }
                })
                .build(app)?;

            // Только что поставили пакет и не перелогинились - сервис ещё не
            // запущен (он включается при входе). Запускаем сами, если не отвечает.
            let url = config.url.clone();
            thread::spawn(move || {
                let alive = ureq::get(&format!("{url}/api/v1/health"))
                    .timeout(Duration::from_secs(3))
                    .call()
                    .is_ok();
                if !alive {
                    let _ = std::process::Command::new("systemctl")
                        .args(["--user", "start", "device-monitor.service"])
                        .status();
                }
            });

            let handle = app.handle().clone();
            thread::spawn(move || poll_loop(handle));
            Ok(())
        })
        .on_window_event(|window, event| {
            // Крестик прячет окно в трей: мониторинг и уведомления работают дальше.
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("не удалось запустить Device Monitor");
}
