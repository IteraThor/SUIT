import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';
import St from 'gi://St';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as AppFavorites from 'resource:///org/gnome/shell/ui/appFavorites.js';
import { DashIcon } from 'resource:///org/gnome/shell/ui/dash.js';
import { QuickToggle, SystemIndicator } from 'resource:///org/gnome/shell/ui/quickSettings.js';

const LOG_PREFIX = '[SUIT Minimal Desktop]';

const DOCK_ICON_SIZE = 48;
const DOCK_SHOW_APPS_ICON_SIZE = 28;
const DOCK_BOTTOM_GAP = 16;

// GNOME builds the quick settings indicators asynchronously after the panel exists
const QS_RETRY_MS = 250;
const QS_MAX_RETRIES = 40;

const AUTOGLOW_URL = 'http://localhost:8080';
// Written by AutoGlow's setup.sh; the shortcut only shows while AutoGlow is installed
const AUTOGLOW_UNIT = '/etc/systemd/system/autoglow.service';
// Same order as SUIT's own "open in browser": Chromium-based first, else the default browser
const BROWSER_APP_IDS = ['chromium-browser.desktop', 'chromium.desktop', 'google-chrome.desktop', 'brave-browser.desktop'];

/* Calls `callback(quickSettings)` once GNOME has built its quick settings indicators.
   Returns a function that cancels the wait. */
function whenQuickSettingsReady(callback) {
    let retries = 0;
    let retryId = 0;
    const check = () => {
        retryId = 0;
        const qs = Main.panel.statusArea.quickSettings;
        if (qs?._doNotDisturb || retries++ >= QS_MAX_RETRIES) {
            callback(qs);
            return;
        }
        retryId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, QS_RETRY_MS, () => {
            check();
            return GLib.SOURCE_REMOVE;
        });
    };
    check();
    return () => {
        if (retryId)
            GLib.source_remove(retryId);
        retryId = 0;
    };
}

// Each finder returns the actor (or list of actors) to hide; `qs` is the quick settings button
const HIDDEN_ITEMS = {
    'accessibility icon': () => Main.panel.statusArea.a11y?.container,
    'microphone icon': qs => qs._volumeInput?._indicator,
    'microphone slider': qs => qs._volumeInput?.quickSettingsItems,
    'power mode icon': qs => qs._powerProfiles?._indicator,
    'power mode toggle': qs => qs._powerProfiles?.quickSettingsItems,
    'night light icon': qs => qs._nightLight?._indicator,
    'night light toggle': qs => qs._nightLight?.quickSettingsItems,
    'dark style toggle': qs => qs._darkMode?.quickSettingsItems,
    'do not disturb icon': qs => qs._doNotDisturb?._indicator,
    'do not disturb toggle': qs => qs._doNotDisturb?.quickSettingsItems,
};

/* Keeps one shell actor hidden although the shell keeps syncing its visibility,
   and remembers what the shell wanted so disabling restores exactly that. */
class VisibilityGuard {
    constructor(actor) {
        this._actor = actor;
        this._wanted = actor.visible;
        this._hiding = false;
        actor.connectObject(
            'notify::visible', () => this._onVisibleChanged(),
            'destroy', () => (this._actor = null),
            this);
        this._hide();
    }

    _onVisibleChanged() {
        if (this._hiding)
            return;
        this._wanted = this._actor.visible;
        this._hide();
    }

    _hide() {
        if (!this._actor.visible)
            return;
        this._hiding = true;
        this._actor.visible = false;
        this._hiding = false;
    }

    destroy() {
        if (!this._actor)
            return;
        this._actor.disconnectObject(this);
        this._actor.visible = this._wanted;
        this._actor = null;
    }
}

class HiddenShellItems {
    constructor() {
        this._guards = [];
        this._cancelWait = whenQuickSettingsReady(qs => this._hideAll(qs));
    }

    _hideAll(qs) {
        for (const [name, find] of Object.entries(HIDDEN_ITEMS)) {
            try {
                const found = qs ? find(qs) : null;
                const actors = (Array.isArray(found) ? found : [found]).filter(Boolean);
                if (!actors.length)
                    console.warn(`${LOG_PREFIX} ${name} not found, left as is`);
                for (const actor of actors)
                    this._guards.push(new VisibilityGuard(actor));
            } catch (e) {
                console.warn(`${LOG_PREFIX} could not hide ${name}: ${e.message}`);
            }
        }
    }

    destroy() {
        this._cancelWait();
        for (const guard of this._guards)
            guard.destroy();
        this._guards = [];
    }
}

/* Favorites and running apps on the desktop itself, just above the wallpaper, so any
   window covers it and it shows whenever the desktop is free. The window group is
   hidden in the overview, which has its own dash. */
class DesktopDock {
    constructor() {
        this._icons = new Map();
        this._redisplayId = 0;

        this._box = new St.BoxLayout({ style_class: 'suit-dock', reactive: true });
        this._appsBox = new St.BoxLayout({ style_class: 'suit-dock-apps' });
        this._separator = new St.Widget({ style_class: 'suit-dock-separator', y_expand: true });
        const showApps = new St.Button({
            style_class: 'suit-dock-show-apps',
            accessible_name: 'Show Apps',
            child: new St.Icon({ icon_name: 'view-app-grid-symbolic', icon_size: DOCK_SHOW_APPS_ICON_SIZE }),
        });
        showApps.connect('clicked', () => Main.overview.showApps());
        this._box.add_child(this._appsBox);
        this._box.add_child(this._separator);
        this._box.add_child(showApps);

        Main.layoutManager._backgroundGroup.add_child(this._box);
        this._box.connect('notify::width', () => this._reposition());
        this._box.connect('notify::height', () => this._reposition());

        AppFavorites.getAppFavorites().connectObject('changed', () => this._queueRedisplay(), this);
        Shell.AppSystem.get_default().connectObject(
            'app-state-changed', () => this._queueRedisplay(),
            'installed-changed', () => this._queueRedisplay(),
            this);
        Main.layoutManager.connectObject('monitors-changed', () => this._reposition(), this);

        this._redisplay();
    }

    _queueRedisplay() {
        if (this._redisplayId)
            return;
        this._redisplayId = GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            this._redisplayId = 0;
            this._redisplay();
            return GLib.SOURCE_REMOVE;
        });
    }

    _apps() {
        const favorites = AppFavorites.getAppFavorites().getFavorites();
        const ids = new Set(favorites.map(app => app.get_id()));
        const running = Shell.AppSystem.get_default().get_running()
            .filter(app => !ids.has(app.get_id()));
        return [...favorites, ...running];
    }

    // Reuses existing icons so their running dots and open menus survive app state changes
    _redisplay() {
        const apps = this._apps();
        const keep = new Set(apps.map(app => app.get_id()));
        for (const [id, icon] of this._icons) {
            if (!keep.has(id)) {
                icon.destroy();
                this._icons.delete(id);
            }
        }
        apps.forEach((app, index) => {
            let icon = this._icons.get(app.get_id());
            if (!icon) {
                icon = new DashIcon(app);
                icon.icon.setIconSize(DOCK_ICON_SIZE);
                this._icons.set(app.get_id(), icon);
                this._appsBox.insert_child_at_index(icon, index);
            } else {
                this._appsBox.set_child_at_index(icon, index);
            }
        });
        this._separator.visible = apps.length > 0;
    }

    _reposition() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor)
            return;
        const { scaleFactor } = St.ThemeContext.get_for_stage(global.stage);
        this._box.set_position(
            Math.floor(monitor.x + (monitor.width - this._box.width) / 2),
            Math.floor(monitor.y + monitor.height - this._box.height - DOCK_BOTTOM_GAP * scaleFactor));
    }

    destroy() {
        if (this._redisplayId)
            GLib.source_remove(this._redisplayId);
        this._redisplayId = 0;
        AppFavorites.getAppFavorites().disconnectObject(this);
        Shell.AppSystem.get_default().disconnectObject(this);
        Main.layoutManager.disconnectObject(this);
        this._box.destroy();
        this._icons.clear();
    }
}

const AutoglowToggle = GObject.registerClass(
class AutoglowToggle extends QuickToggle {
    _init(gicon) {
        super._init({ title: 'Autoglow 2', subtitle: 'Open web interface', gicon });
    }
});

/* Quick settings button next to Wi-Fi that opens the AutoGlow 2 web interface */
class AutoglowShortcut {
    constructor(extension) {
        this._indicator = null;
        this._gicon = Gio.icon_new_for_string(`${extension.path}/autoglow-symbolic.svg`);
        this._cancelWait = whenQuickSettingsReady(qs => this._add(qs));
    }

    _add(qs) {
        this._toggle = new AutoglowToggle(this._gicon);
        this._toggle.connect('clicked', () => this._open());
        this._indicator = new SystemIndicator();
        this._indicator.quickSettingsItems.push(this._toggle);
        qs.addExternalIndicator(this._indicator);

        // AutoGlow can be installed or removed while the shell runs, so check on every open
        this._syncVisible();
        qs.menu.connectObject('open-state-changed', (menu, open) => open && this._syncVisible(), this);
    }

    _syncVisible() {
        this._toggle.visible = GLib.file_test(AUTOGLOW_UNIT, GLib.FileTest.EXISTS);
    }

    _open() {
        Main.panel.statusArea.quickSettings.menu.close();
        const context = global.create_app_launch_context(0, -1);
        try {
            const browser = BROWSER_APP_IDS.map(id => Gio.DesktopAppInfo.new(id)).find(Boolean);
            if (browser)
                browser.launch_uris([AUTOGLOW_URL], context);
            else
                Gio.AppInfo.launch_default_for_uri(AUTOGLOW_URL, context);
        } catch (e) {
            Main.notifyError('Could not open Autoglow 2', e.message);
        }
    }

    destroy() {
        this._cancelWait();
        if (!this._indicator)
            return;
        Main.panel.statusArea.quickSettings.menu.disconnectObject(this);
        this._indicator.quickSettingsItems.forEach(item => item.destroy());
        this._indicator.destroy();
        this._indicator = null;
    }
}

// Independent tweaks: one that breaks on a GNOME update must not take the others down
const TWEAKS = {
    'hidden shell items': HiddenShellItems,
    'desktop dock': DesktopDock,
    'autoglow shortcut': AutoglowShortcut,
};

export default class MinimalDesktopExtension extends Extension {
    enable() {
        this._tweaks = [];
        for (const [name, Tweak] of Object.entries(TWEAKS)) {
            try {
                this._tweaks.push(new Tweak(this));
            } catch (e) {
                console.error(`${LOG_PREFIX} ${name} failed to start: ${e.message}`);
            }
        }
    }

    disable() {
        for (const tweak of this._tweaks) {
            try {
                tweak.destroy();
            } catch (e) {
                console.error(`${LOG_PREFIX} cleanup failed: ${e.message}`);
            }
        }
        this._tweaks = null;
    }
}
