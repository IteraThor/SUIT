//! `suit create-launcher`: application menu entries, hicolor icons and the Dash pin (was create_launcher.py).
use crate::core::util::{fmt_strv, gsettings_get, gsettings_set, home, parse_strv, project_dir, run, self_exe};
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

fn desktop_dir() -> PathBuf {
    let x = run(&["xdg-user-dir", "DESKTOP"], Some(5)).stdout.trim().to_string();
    if !x.is_empty() && PathBuf::from(&x).exists() {
        return PathBuf::from(x);
    }
    for f in ["Desktop", "Schreibtisch", "Bureau", "Escritorio"] {
        if home().join(f).exists() {
            return home().join(f);
        }
    }
    home()
}

/// SUIT application icon in the user hicolor theme for GNOME Shell.
fn install_hicolor_icons() {
    let src = project_dir().join("assets/icons/suit-icon.png");
    if !src.exists() {
        return;
    }
    let link = project_dir().join("assets/icons/de.iterathor.suit.gtk.png");
    if !link.exists() {
        let _ = std::os::unix::fs::symlink("suit-icon.png", &link);
    }
    for size in ["512x512", "256x256", "128x128", "scalable"] {
        let d = home().join(format!(".local/share/icons/hicolor/{}/apps", size));
        let _ = std::fs::create_dir_all(&d);
        let _ = std::fs::copy(&src, d.join("de.iterathor.suit.gtk.png"));
        let _ = std::fs::copy(&src, d.join("suit-icon.png"));
    }
    run(&["gtk-update-icon-cache", "-f", "-t", &home().join(".local/share/icons/hicolor").to_string_lossy()], Some(30));
}

/// Pin the SUIT desktop entry to GNOME Shell Dash favorites.
pub fn pin_to_dash(desktop: &str) -> bool {
    let Some(mut favs) = parse_strv(&gsettings_get("org.gnome.shell", "favorite-apps")) else { return false };
    favs.retain(|f| f != "SUIT.desktop");
    if favs.iter().any(|f| f == desktop) {
        println!("{} is already pinned to GNOME Dash favorites.", desktop);
        return true;
    }
    favs.push(desktop.into());
    let ok = gsettings_set("org.gnome.shell", "favorite-apps", &fmt_strv(&favs));
    if ok {
        println!("Pinned {} to GNOME Dash favorites.", desktop);
    }
    ok
}

pub fn create_desktop_launcher() -> std::io::Result<()> {
    install_hicolor_icons();
    let pd = project_dir();
    let entry = format!(
        "[Desktop Entry]\nVersion=1.0\nName=SUIT for Fedora\nComment=Setup Utilities by IteraThor (GTK4 / Libadwaita)\nExec={}\nIcon=de.iterathor.suit.gtk\nPath={}\nTerminal=false\nType=Application\nCategories=Utility;Settings;\nStartupNotify=true\nStartupWMClass=de.iterathor.suit.gtk\n",
        pd.join("suit-launch.sh").display(),
        pd.display()
    );
    let apps = home().join(".local/share/applications");
    std::fs::create_dir_all(&apps)?;
    let p = apps.join("de.iterathor.suit.gtk.desktop");
    std::fs::write(&p, &entry)?;
    let mode = std::fs::metadata(&p)?.permissions().mode() | 0o111;
    std::fs::set_permissions(&p, std::fs::Permissions::from_mode(mode))?;
    println!("Application menu launcher created at: {}", p.display());
    // Older installs also wrote SUIT.desktop, which showed SUIT twice in the app grid
    let legacy = apps.join("SUIT.desktop");
    if legacy.exists() && std::fs::remove_file(&legacy).is_ok() {
        println!("Removed duplicate launcher: {}", legacy.display());
    }
    run(&["update-desktop-database", &apps.to_string_lossy()], Some(30));
    // GNOME does not use desktop icons: remove launcher files from the desktop folder
    let dd = desktop_dir();
    if dd != home() {
        for f in ["de.iterathor.suit.gtk.desktop", "SUIT.desktop"] {
            let p = dd.join(f);
            if p.exists() && std::fs::remove_file(&p).is_ok() {
                println!("Removed unnecessary desktop folder file: {}", p.display());
            }
        }
    }
    pin_to_dash("de.iterathor.suit.gtk.desktop");
    Ok(())
}

pub fn main() -> i32 {
    println!("--- SUIT GTK4 Setup ---");
    let _ = self_exe();
    match create_desktop_launcher() {
        Ok(_) => {
            println!("Launcher setup completed successfully.");
            0
        }
        Err(e) => {
            println!("Failed to create launcher: {}", e);
            1
        }
    }
}
