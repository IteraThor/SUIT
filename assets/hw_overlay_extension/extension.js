import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const FPS_LOW_WINDOW_MS = 60 * 1000;
const MARGIN = 24;
// Autodarts' own debug dump (what the Debug button on its web page saves) can take a while to build
const DUMP_TIMEOUT_S = 60;
// How long a reading of the network addresses is trusted before `ip` is asked again
const ADDRESS_MAX_AGE_US = 10 * 1000 * 1000;
// Interfaces that are not the machine's own LAN: containers, VMs, tunnels
const VIRTUAL_IFACES = /^(lo|docker|veth|virbr|br-|waydroid|lxc|podman|cni|flannel|tun|tailscale)/;
// Autodarts is asked to look for a new release this often (it also checks on its own)
const UPDATE_CHECK_S = 6 * 60 * 60;
// A second tap within this long confirms an update, which restarts Autodarts
const UPDATE_CONFIRM_MS = 4000;
// Camera tiles shown even when Autodarts reports fewer (or none): the setup has three
const MIN_CAM_TILES = 3;
const AUTODARTS_CONFIG = GLib.build_filenamev([GLib.get_home_dir(), '.config', 'autodarts', 'config.toml']);
const CONFIG_DIR = GLib.build_filenamev([GLib.get_home_dir(), '.config', 'suit']);
const CONFIG_NAME = 'hw_overlay.json';

// Mirrors core.desktop_service.HW_OVERLAY_DEFAULTS / HW_OVERLAY_CHOICES; SUIT writes the file.
const DEFAULTS = {
    style: 'card',
    position: 'top-right',
    size: 'medium',
    accent: 'blue',
    opacity: 70,
    meters: true,
    headings: true,
    interval: 2,
    metrics: {
        cpu: true, temp: true, ram: true, gpu: true, disk: true, net: false, ip_local: false, ip_tailscale: false,
        load: false, uptime: true,
        ad_state: true, ad_fps: true, ad_cams: true, ad_proc: false,
    },
    // Clickable Autodarts controls. The update button only appears while an update is waiting.
    buttons: { ad_toggle: false, ad_reset: false, ad_dump: false, ad_update: true },
};
const CHOICES = {
    style: ['card', 'minimal'],
    position: ['top-right', 'top-left', 'bottom-right', 'bottom-left'],
    size: ['small', 'medium', 'large'],
    accent: ['blue', 'teal', 'green', 'purple', 'white'],
    interval: [1, 2, 5],
};
const ACCENTS = { blue: '#81a1c1', teal: '#88c0d0', green: '#a3be8c', purple: '#b48ead', white: '#d8dee9' };

// [key label, has a meter]
const SYSTEM_ROWS = {
    cpu: ['CPU', true],
    temp: ['TEMP', true],
    ram: ['RAM', true],
    gpu: ['GPU', true],
    disk: ['DISK', true],
    net: ['NET', false],
    ip_local: ['LAN IP', false],
    ip_tailscale: ['TAILSCALE', false],
    load: ['LOAD', false],
    uptime: ['UPTIME', false],
};
const CPU_HWMON_NAMES = ['coretemp', 'k10temp', 'zenpower', 'cpu_thermal', 'soc_thermal'];

const decoder = new TextDecoder();

function readText(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? decoder.decode(bytes) : null;
    } catch {
        return null;
    }
}

function readInt(path) {
    const txt = readText(path);
    const val = txt === null ? NaN : parseInt(txt.trim(), 10);
    return Number.isFinite(val) ? val : null;
}

function listDir(path) {
    const names = [];
    try {
        const en = Gio.File.new_for_path(path).enumerate_children('standard::name', Gio.FileQueryInfoFlags.NONE, null);
        let info;
        while ((info = en.next_file(null)) !== null)
            names.push(info.get_name());
        en.close(null);
    } catch {
        // Directory missing on this machine
    }
    return names.sort();
}

function hwmonTempPath(dir, preferredLabels) {
    const inputs = listDir(dir).filter(n => /^temp\d+_input$/.test(n));
    for (const input of inputs) {
        const label = readText(`${dir}/${input.replace('_input', '_label')}`);
        if (label && preferredLabels.some(p => label.trim().startsWith(p)))
            return `${dir}/${input}`;
    }
    return inputs.length ? `${dir}/${inputs[0]}` : null;
}

// Locate the CPU package temperature and an optional AMD GPU, once per enable.
function discoverSensors() {
    const sensors = { cpuTemp: null, gpuTemp: null, gpuBusy: null, freqPaths: [] };

    for (const hw of listDir('/sys/class/hwmon')) {
        const dir = `/sys/class/hwmon/${hw}`;
        const name = (readText(`${dir}/name`) || '').trim();
        if (!sensors.cpuTemp && CPU_HWMON_NAMES.includes(name))
            sensors.cpuTemp = hwmonTempPath(dir, ['Package', 'Tctl', 'Tdie']);
        else if (!sensors.gpuTemp && name === 'amdgpu')
            sensors.gpuTemp = hwmonTempPath(dir, ['edge']);
    }

    if (!sensors.cpuTemp) {
        const zones = listDir('/sys/class/thermal').filter(n => n.startsWith('thermal_zone'));
        const pick = zones.find(z => (readText(`/sys/class/thermal/${z}/type`) || '').trim() === 'x86_pkg_temp') || zones[0];
        if (pick)
            sensors.cpuTemp = `/sys/class/thermal/${pick}/temp`;
    }

    for (const card of listDir('/sys/class/drm').filter(n => /^card\d+$/.test(n))) {
        const path = `/sys/class/drm/${card}/device/gpu_busy_percent`;
        if (readInt(path) !== null) {
            sensors.gpuBusy = path;
            break;
        }
    }

    for (const cpu of listDir('/sys/devices/system/cpu').filter(n => /^cpu\d+$/.test(n))) {
        const path = `/sys/devices/system/cpu/${cpu}/cpufreq/scaling_cur_freq`;
        if (readInt(path) !== null)
            sensors.freqPaths.push(path);
    }
    return sensors;
}

// Mirrors core.autodarts_service.get_autodarts_port: config.toml first, then the v2/v1 defaults.
function autodartsPorts() {
    const ports = [];
    const toml = readText(AUTODARTS_CONFIG);
    if (toml) {
        let section = '';
        for (const line of toml.split('\n')) {
            const sec = line.match(/^\s*\[([^\]]+)\]/);
            if (sec) {
                section = sec[1].trim();
                continue;
            }
            const m = line.match(/^\s*port\s*=\s*"?(\d+)"?/);
            if (m && (section === 'api' || section === 'host'))
                ports.push(parseInt(m[1], 10));
        }
    }
    return [...new Set([...ports, 3182, 3180])];
}

function loadConfig() {
    let raw = {};
    try {
        raw = JSON.parse(readText(GLib.build_filenamev([CONFIG_DIR, CONFIG_NAME])) || '{}') ?? {};
    } catch {
        // Half-written or hand-edited file: fall back to the defaults
    }
    const cfg = { ...DEFAULTS, metrics: { ...DEFAULTS.metrics }, buttons: { ...DEFAULTS.buttons } };
    for (const [key, choices] of Object.entries(CHOICES)) {
        if (choices.includes(raw[key]))
            cfg[key] = raw[key];
    }
    for (const key of ['meters', 'headings']) {
        if (typeof raw[key] === 'boolean')
            cfg[key] = raw[key];
    }
    if (typeof raw.opacity === 'number')
        cfg.opacity = Math.max(0, Math.min(100, raw.opacity));
    for (const key of Object.keys(cfg.metrics)) {
        if (typeof raw.metrics?.[key] === 'boolean')
            cfg.metrics[key] = raw.metrics[key];
    }
    for (const key of Object.keys(cfg.buttons)) {
        if (typeof raw.buttons?.[key] === 'boolean')
            cfg.buttons[key] = raw.buttons[key];
    }
    return cfg;
}

// `ip -4 -o addr show scope global` -> { local: {iface, ip} | null, tailscale: {iface, ip} | null }
function parseAddresses(text) {
    const found = { local: null, tailscale: null };
    for (const line of text.split('\n')) {
        const m = line.match(/^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)\//);
        if (!m)
            continue;
        const [, iface, ip] = m;
        if (iface.startsWith('tailscale'))
            found.tailscale ??= { iface, ip };
        else if (!VIRTUAL_IFACES.test(iface))
            found.local ??= { iface, ip };
    }
    return found;
}

function formatBytes(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) {
        bytes /= 1024;
        i++;
    }
    return `${bytes.toFixed(bytes >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return d > 0 ? `${d}d ${h}h` : `${h}h ${String(m).padStart(2, '0')}m`;
}

// Hardware values stay neutral while healthy; only warn and bad are colored.
function level(value, warn, bad) {
    if (value >= bad)
        return 'suit-hwmon-bad';
    return value >= warn ? 'suit-hwmon-warn' : null;
}

function fpsLevel(fps, target) {
    if (fps >= target * 0.9)
        return 'suit-hwmon-ok';
    return fps >= target * 0.6 ? 'suit-hwmon-warn' : 'suit-hwmon-bad';
}

const LEVEL_CLASSES = ['suit-hwmon-ok', 'suit-hwmon-warn', 'suit-hwmon-bad', 'suit-hwmon-dim'];

function setLevel(actor, cls) {
    for (const c of LEVEL_CLASSES)
        actor.remove_style_class_name(c);
    if (cls)
        actor.add_style_class_name(cls);
}

function capitalize(text) {
    const t = String(text);
    return t.charAt(0).toUpperCase() + t.slice(1);
}

function vbox(styleClass) {
    const box = new St.BoxLayout({ style_class: styleClass });
    if ('orientation' in box)
        box.orientation = Clutter.Orientation.VERTICAL;
    else
        box.vertical = true;
    return box;
}

export default class HwOverlayExtension extends Extension {
    enable() {
        this._sensors = discoverSensors();
        this._prevCpu = null;
        this._prevNet = null;
        this._fpsLows = [];
        this._ports = autodartsPorts();
        this._portIdx = 0;
        this._adBusy = false;
        this._adFailures = 0;
        this._session = new Soup.Session({ timeout: 2 });
        this._dumpSession = new Soup.Session({ timeout: DUMP_TIMEOUT_S });
        this._dumpBusy = false;
        this._addrs = null;
        this._addrsAt = 0;
        this._addrsBusy = false;
        this._updateArmed = false;
        this._cancellable = new Gio.Cancellable();
        this._config = loadConfig();
        this._lastAd = undefined;

        this._buildUi();
        this._watchConfig();

        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._reposition());

        this._tick();
        this._startTimer();
        this._startUpdateChecks();
    }

    disable() {
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
        if (this._reloadId) {
            GLib.source_remove(this._reloadId);
            this._reloadId = 0;
        }
        for (const id of ['_checkFirstId', '_checkId', '_armId']) {
            if (this[id]) {
                GLib.source_remove(this[id]);
                this[id] = 0;
            }
        }
        if (this._configMonitor) {
            this._configMonitor.cancel();
            this._configMonitor = null;
        }
        this._cancellable?.cancel();
        this._session?.abort();
        this._dumpSession?.abort();
        if (this._monitorsId) {
            Main.layoutManager.disconnect(this._monitorsId);
            this._monitorsId = 0;
        }
        this._box?.destroy();
        this._box = null;
        this._session = null;
        this._dumpSession = null;
        this._cancellable = null;
        this._sensors = null;
        this._rows = null;
        this._ad = null;
        this._lastAd = undefined;
    }

    _startTimer() {
        if (this._timerId)
            GLib.source_remove(this._timerId);
        this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, this._config.interval, () => {
            this._tick();
            return GLib.SOURCE_CONTINUE;
        });
    }

    // SUIT saves hw_overlay.json by renaming a temp file over it, so watch the folder, not the file.
    _watchConfig() {
        GLib.mkdir_with_parents(CONFIG_DIR, 0o755);
        try {
            this._configMonitor = Gio.File.new_for_path(CONFIG_DIR).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, null);
        } catch {
            return;
        }
        this._configMonitor.connect('changed', (monitor, file, other) => {
            if (file?.get_basename() !== CONFIG_NAME && other?.get_basename() !== CONFIG_NAME)
                return;
            if (this._reloadId)
                return;
            this._reloadId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
                this._reloadId = 0;
                this._reload();
                return GLib.SOURCE_REMOVE;
            });
        });
    }

    _reload() {
        if (!this._box)
            return;
        const prevInterval = this._config.interval;
        this._config = loadConfig();
        this._box.destroy();
        this._buildUi();
        if (this._config.interval !== prevInterval)
            this._startTimer();
        this._tick();
        if (this._lastAd !== undefined)
            this._renderAutodarts(this._lastAd);
    }

    // ---------------------------------------------------------------- UI

    _buildUi() {
        const cfg = this._config;
        const m = cfg.metrics;
        this._box = vbox(`suit-hwmon suit-hwmon-${cfg.style} suit-hwmon-${cfg.size}`);
        this._box.reactive = false;
        if (cfg.style === 'card')
            this._box.set_style(`background-color: rgba(16, 18, 24, ${(cfg.opacity / 100).toFixed(2)});`);
        this._rows = {};
        this._ad = {};

        const hasGpu = Boolean(this._sensors.gpuBusy || this._sensors.gpuTemp);
        const system = Object.keys(SYSTEM_ROWS).filter(k => m[k] && (k !== 'gpu' || hasGpu));
        const autodarts = ['ad_state', 'ad_fps', 'ad_cams', 'ad_proc'].some(k => m[k])
            || Object.values(cfg.buttons).some(Boolean);

        if (system.length) {
            if (cfg.headings)
                this._box.add_child(new St.Label({ text: 'SYSTEM', style_class: 'suit-hwmon-heading' }));
            for (const id of system)
                this._addRow(id, SYSTEM_ROWS[id][0], SYSTEM_ROWS[id][1] && cfg.meters);
        }
        if (autodarts) {
            if (system.length)
                this._box.add_child(new St.Widget({ style_class: 'suit-hwmon-sep' }));
            this._buildAutodarts();
        }
        this._box.visible = system.length > 0 || autodarts;

        Main.layoutManager._backgroundGroup.add_child(this._box);
        this._box.connect('notify::width', () => this._reposition());
        this._box.connect('notify::height', () => this._reposition());
        this._reposition();
    }

    _buildAutodarts() {
        const cfg = this._config;
        const m = cfg.metrics;
        const ad = this._ad;

        if (cfg.headings || m.ad_state) {
            const head = new St.BoxLayout({ style_class: 'suit-hwmon-line' });
            if (cfg.headings)
                head.add_child(new St.Label({ text: 'AUTODARTS', style_class: 'suit-hwmon-heading', y_align: Clutter.ActorAlign.CENTER }));
            head.add_child(new St.Widget({ x_expand: true }));
            if (m.ad_state) {
                ad.version = new St.Label({ style_class: 'suit-hwmon-detail', y_align: Clutter.ActorAlign.CENTER });
                ad.pill = new St.Label({ text: '…', style_class: 'suit-hwmon-pill suit-hwmon-dim', y_align: Clutter.ActorAlign.CENTER });
                head.add_child(ad.version);
                head.add_child(ad.pill);
            }
            this._box.add_child(head);
        }

        if (m.ad_fps) {
            const hero = new St.BoxLayout({ style_class: 'suit-hwmon-hero' });
            ad.fps = new St.Label({ style_class: 'suit-hwmon-hero-value', y_align: Clutter.ActorAlign.END });
            ad.fpsUnit = new St.Label({ text: 'fps', style_class: 'suit-hwmon-hero-unit', y_align: Clutter.ActorAlign.END });
            // Replaces the number while there is nothing to measure (offline, standby)
            ad.fpsNote = new St.Label({ text: '…', style_class: 'suit-hwmon-detail', y_align: Clutter.ActorAlign.CENTER });
            hero.add_child(ad.fps);
            hero.add_child(ad.fpsUnit);
            hero.add_child(ad.fpsNote);
            hero.add_child(new St.Widget({ x_expand: true }));
            const info = vbox('suit-hwmon-hero-info');
            info.y_align = Clutter.ActorAlign.CENTER;
            ad.fpsLow = new St.Label({ style_class: 'suit-hwmon-detail', x_align: Clutter.ActorAlign.END });
            ad.fpsRes = new St.Label({ style_class: 'suit-hwmon-detail', x_align: Clutter.ActorAlign.END });
            info.add_child(ad.fpsLow);
            info.add_child(ad.fpsRes);
            hero.add_child(info);
            this._box.add_child(hero);
        }

        if (m.ad_cams) {
            ad.cams = new St.BoxLayout({ style_class: 'suit-hwmon-cams', x_expand: true });
            ad.cams.layout_manager.homogeneous = true;
            ad.camLabels = [];
            this._box.add_child(ad.cams);
            this._setCams([], null, false);
        }

        if (m.ad_proc)
            this._addRow('proc', 'PROCESS', false);

        if (Object.values(cfg.buttons).some(Boolean))
            this._buildButtons();
    }

    // The same actions as the buttons on Autodarts' own page (localhost:3180)
    _buildButtons() {
        const b = this._config.buttons;
        const ad = this._ad;
        if (b.ad_update) {
            // Shown by _renderAutodarts only while Autodarts reports a newer release
            ad.update = new St.Button({
                label: '', style_class: 'suit-hwmon-btn suit-hwmon-btn-update', reactive: true,
                can_focus: false, track_hover: true, x_expand: true,
            });
            ad.update.visible = false;
            ad.update.connect('clicked', () => this._onUpdate());
            this._box.add_child(ad.update);
        }
        if (!(b.ad_toggle || b.ad_reset || b.ad_dump))
            return;
        const row = new St.BoxLayout({ style_class: 'suit-hwmon-buttons', x_expand: true });
        row.layout_manager.homogeneous = true;
        const add = (label, handler) => {
            const btn = new St.Button({
                label, style_class: 'suit-hwmon-btn', reactive: true, can_focus: false,
                track_hover: true, x_expand: true,
            });
            btn.connect('clicked', handler);
            row.add_child(btn);
            return btn;
        };
        if (b.ad_toggle)
            ad.toggle = add('Start', () => this._onToggle());
        if (b.ad_reset)
            ad.reset = add('Reset', () => this._onReset());
        if (b.ad_dump)
            ad.dump = add('Debug', () => this._onDump());
        this._box.add_child(row);
    }

    _adPort() {
        return this._ports[this._portIdx % this._ports.length];
    }

    // Fire-and-forget request to the local Autodarts API; done(status, bytes, msg) runs on the main loop.
    _adRequest(method, path, done, session = this._session) {
        if (!session)
            return;
        const msg = Soup.Message.new(method, `http://127.0.0.1:${this._adPort()}${path}`);
        session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancellable, (sess, res) => {
            let bytes = null;
            try {
                bytes = sess.send_and_read_finish(res);
            } catch (e) {
                if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
            }
            done(bytes ? msg.get_status() : 0, bytes, msg);
        });
    }

    _notify(text) {
        Main.notify('SUIT', text);
    }

    // Ask Autodarts to look for a release now and then; the answer shows up in /api/system
    _startUpdateChecks() {
        const check = () => {
            if (this._config.buttons.ad_update)
                this._adRequest('POST', '/api/update/check', () => this._pollAutodarts(), this._dumpSession);
        };
        this._checkFirstId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 8, () => {
            this._checkFirstId = 0;
            check();
            return GLib.SOURCE_REMOVE;
        });
        this._checkId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, UPDATE_CHECK_S, () => {
            check();
            return GLib.SOURCE_CONTINUE;
        });
    }

    // Updating restarts Autodarts, so the first tap only arms the button
    _onUpdate() {
        const btn = this._ad.update;
        if (!btn)
            return;
        if (!this._updateArmed) {
            this._updateArmed = true;
            btn.label = 'Tap again to install';
            this._armId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, UPDATE_CONFIRM_MS, () => {
                this._armId = 0;
                this._updateArmed = false;
                this._renderAutodarts(this._lastAd);
                return GLib.SOURCE_REMOVE;
            });
            return;
        }
        if (this._armId) {
            GLib.source_remove(this._armId);
            this._armId = 0;
        }
        this._updateArmed = false;
        btn.label = 'Updating…';
        this._adRequest('POST', '/api/update/apply', status => {
            if (status === Soup.Status.OK || status === Soup.Status.ACCEPTED) {
                this._notify('Autodarts is installing its update and will restart.');
            } else {
                this._notify('Could not start the Autodarts update.');
                this._renderAutodarts(this._lastAd);
            }
        }, this._dumpSession);
    }

    _onToggle() {
        const running = this._lastAd?.camState?.isRunning ?? true;
        this._adRequest('PUT', running ? '/api/stop' : '/api/start', status => {
            if (status === Soup.Status.OK)
                this._pollAutodarts();
            else
                this._notify(`Could not ${running ? 'stop' : 'start'} Autodarts.`);
        });
    }

    _onReset() {
        this._adRequest('POST', '/api/reset', status => {
            if (status === Soup.Status.OK) {
                this._notify('Autodarts was reset.');
                this._pollAutodarts();
            } else {
                this._notify('Could not reset Autodarts.');
            }
        });
    }

    // Saves the same zip as the Debug button on the Autodarts page, into the Downloads folder.
    _onDump() {
        if (this._dumpBusy)
            return;
        this._dumpBusy = true;
        this._notify('Saving the Autodarts debug snapshot…');
        this._adRequest('POST', '/api/debug/dump', (status, bytes) => {
            this._dumpBusy = false;
            if (status !== Soup.Status.OK || !bytes) {
                this._notify(status === Soup.Status.SERVICE_UNAVAILABLE
                    ? 'Autodarts is not ready for a debug snapshot yet.'
                    : 'Could not get a debug snapshot from Autodarts.');
                return;
            }
            const dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                ?? GLib.build_filenamev([GLib.get_home_dir(), 'Downloads']);
            const stamp = GLib.DateTime.new_now_local().format('%Y%m%d-%H%M%S');
            const path = GLib.build_filenamev([dir, `autodarts-dump-${stamp}.zip`]);
            try {
                GLib.mkdir_with_parents(dir, 0o755);
                GLib.file_set_contents(path, bytes.get_data());
                this._notify(`Debug snapshot saved to Downloads (autodarts-dump-${stamp}.zip).`);
            } catch (e) {
                this._notify(`Could not save the snapshot: ${e.message}`);
            }
        }, this._dumpSession);
    }

    _addRow(id, key, withMeter) {
        const item = vbox('suit-hwmon-item');
        const line = new St.BoxLayout({ style_class: 'suit-hwmon-line' });
        line.add_child(new St.Label({ text: key, style_class: 'suit-hwmon-key', y_align: Clutter.ActorAlign.CENTER }));
        line.add_child(new St.Widget({ x_expand: true }));
        const detail = new St.Label({ style_class: 'suit-hwmon-detail', y_align: Clutter.ActorAlign.CENTER });
        const value = new St.Label({ text: '—', style_class: 'suit-hwmon-value', y_align: Clutter.ActorAlign.CENTER });
        line.add_child(detail);
        line.add_child(value);
        item.add_child(line);

        const row = { value, detail, bar: null, fill: null, fraction: null, cls: null };
        if (withMeter) {
            row.bar = new St.Widget({ style_class: 'suit-hwmon-bar', x_expand: true });
            row.fill = new St.Widget({ style_class: 'suit-hwmon-fill', width: 0 });
            row.bar.add_child(row.fill);
            row.bar.connect('notify::width', () => this._applyMeter(row));
            item.add_child(row.bar);
        }
        this._box.add_child(item);
        this._rows[id] = row;
    }

    // `value` is the bright right-aligned figure, `detail` the dim context left of it.
    _set(id, value, detail = '', cls = null, fraction = null) {
        const row = this._rows?.[id];
        if (!row)
            return;
        row.value.text = value;
        row.detail.text = detail;
        row.detail.visible = detail !== '';
        setLevel(row.value, cls);
        row.cls = cls;
        if (fraction !== null) {
            row.fraction = Math.max(0, Math.min(1, fraction));
            this._applyMeter(row);
        }
    }

    _applyMeter(row) {
        if (!row.fill || row.fraction === null)
            return;
        row.fill.width = Math.round(row.fraction * row.bar.width);
        setLevel(row.fill, row.cls);
        row.fill.set_style(row.cls ? null : `background-color: ${ACCENTS[this._config.accent]};`);
    }

    _reposition() {
        const mon = Main.layoutManager.primaryMonitor;
        if (!this._box || !mon)
            return;
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const margin = MARGIN * scale;
        const panelHeight = Main.panel?.height ?? 0;
        const [vertical, horizontal] = this._config.position.split('-');
        const x = horizontal === 'left' ? mon.x + margin : mon.x + mon.width - this._box.width - margin;
        const y = vertical === 'top' ? mon.y + panelHeight + margin : mon.y + mon.height - this._box.height - margin;
        this._box.set_position(Math.round(x), Math.round(y));
    }

    // ---------------------------------------------------------- Sampling

    _tick() {
        if (!this._box)
            return;
        // CPU and network keep sampling while hidden so their deltas are ready when shown again.
        this._updateCpu();
        this._updateNetwork();
        if (this._rows.temp || this._rows.gpu)
            this._updateTemps();
        if (this._rows.ram)
            this._updateMemory();
        if (this._rows.disk)
            this._updateDisk();
        if (this._rows.load || this._rows.uptime)
            this._updateLoad();
        if (this._rows.ip_local || this._rows.ip_tailscale)
            this._updateAddresses();
        if (this._ad.pill || this._ad.fps || this._ad.cams || this._rows.proc)
            this._pollAutodarts();
    }

    _updateCpu() {
        const line = (readText('/proc/stat') || '').split('\n')[0];
        const nums = line.trim().split(/\s+/).slice(1, 9).map(Number);
        if (nums.length < 4)
            return;
        const idle = nums[3] + (nums[4] || 0);
        const total = nums.reduce((a, b) => a + b, 0);
        if (this._prevCpu && this._rows.cpu) {
            let freq = '';
            const freqs = this._sensors.freqPaths.map(readInt).filter(v => v !== null);
            if (freqs.length)
                freq = `${(freqs.reduce((a, b) => a + b, 0) / freqs.length / 1e6).toFixed(1)} GHz`;
            const dt = total - this._prevCpu.total;
            const usage = dt > 0 ? 100 * (1 - (idle - this._prevCpu.idle) / dt) : 0;
            this._set('cpu', `${usage.toFixed(0)}%`, freq, level(usage, 70, 90), usage / 100);
        }
        this._prevCpu = { idle, total };
    }

    _updateTemps() {
        if (this._sensors.cpuTemp) {
            const raw = readInt(this._sensors.cpuTemp);
            if (raw !== null) {
                const c = raw / 1000;
                this._set('temp', `${c.toFixed(0)}°C`, '', level(c, 75, 90), c / 100);
            }
        } else {
            this._set('temp', 'n/a');
        }

        if (this._rows.gpu) {
            const busy = this._sensors.gpuBusy ? readInt(this._sensors.gpuBusy) : null;
            const t = this._sensors.gpuTemp ? readInt(this._sensors.gpuTemp) : null;
            const temp = t !== null ? `${(t / 1000).toFixed(0)}°C` : '';
            if (busy !== null)
                this._set('gpu', `${busy}%`, temp, level(busy, 70, 90), busy / 100);
            else
                this._set('gpu', temp || 'n/a', '', t !== null ? level(t / 1000, 80, 95) : null, t !== null ? t / 100000 : null);
        }
    }

    _updateMemory() {
        const info = {};
        for (const line of (readText('/proc/meminfo') || '').split('\n')) {
            const m = line.match(/^(\w+):\s+(\d+)/);
            if (m)
                info[m[1]] = parseInt(m[2], 10) * 1024;
        }
        if (!info.MemTotal)
            return;
        const used = info.MemTotal - (info.MemAvailable ?? info.MemFree);
        const pct = 100 * used / info.MemTotal;
        this._set('ram', `${pct.toFixed(0)}%`, `${formatBytes(used)} of ${formatBytes(info.MemTotal)}`, level(pct, 80, 92), pct / 100);
    }

    _updateDisk() {
        try {
            const info = Gio.File.new_for_path('/').query_filesystem_info('filesystem::size,filesystem::free', null);
            const size = info.get_attribute_uint64('filesystem::size');
            const free = info.get_attribute_uint64('filesystem::free');
            if (size > 0) {
                const pct = 100 * (size - free) / size;
                this._set('disk', `${pct.toFixed(0)}%`, `${formatBytes(free)} free`, level(pct, 85, 95), pct / 100);
            }
        } catch {
            this._set('disk', 'n/a');
        }
    }

    _updateNetwork() {
        let rx = 0, tx = 0;
        for (const line of (readText('/proc/net/dev') || '').split('\n').slice(2)) {
            const [iface, rest] = line.split(':');
            if (!rest || iface.trim() === 'lo')
                continue;
            const cols = rest.trim().split(/\s+/).map(Number);
            rx += cols[0];
            tx += cols[8];
        }
        const now = GLib.get_monotonic_time();
        if (this._prevNet) {
            const dt = (now - this._prevNet.time) / 1e6;
            if (dt > 0) {
                const down = Math.max(0, (rx - this._prevNet.rx) / dt);
                const up = Math.max(0, (tx - this._prevNet.tx) / dt);
                this._set('net', `↓ ${formatBytes(down)}/s`, `↑ ${formatBytes(up)}/s`);
            }
        }
        this._prevNet = { rx, tx, time: now };
    }

    // `ip` is asked at most every ADDRESS_MAX_AGE_US; between readings the last answer is redrawn.
    _updateAddresses() {
        this._renderAddresses();
        if (this._addrsBusy || GLib.get_monotonic_time() - this._addrsAt < ADDRESS_MAX_AGE_US)
            return;
        this._addrsBusy = true;
        try {
            const proc = Gio.Subprocess.new(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
            proc.communicate_utf8_async(null, this._cancellable, (p, res) => {
                this._addrsBusy = false;
                try {
                    const [, out] = p.communicate_utf8_finish(res);
                    this._addrs = parseAddresses(out ?? '');
                } catch (e) {
                    if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        return;
                    this._addrs = { local: null, tailscale: null };
                }
                this._addrsAt = GLib.get_monotonic_time();
                this._renderAddresses();
            });
        } catch {
            // `ip` is not installed
            this._addrsBusy = false;
            this._addrs = { local: null, tailscale: null };
            this._addrsAt = GLib.get_monotonic_time();
            this._renderAddresses();
        }
    }

    _renderAddresses() {
        if (!this._rows || !this._addrs)
            return;
        const show = (id, entry) => {
            if (this._rows[id])
                this._set(id, entry ? entry.ip : '—', entry ? entry.iface : 'not connected', entry ? null : 'suit-hwmon-dim');
        };
        show('ip_local', this._addrs.local);
        show('ip_tailscale', this._addrs.tailscale);
    }

    _updateLoad() {
        const load = (readText('/proc/loadavg') || '').trim().split(/\s+/).slice(0, 3);
        if (load.length === 3)
            this._set('load', load[0], `${load[1]} · ${load[2]}`);
        const up = parseFloat(readText('/proc/uptime') || '');
        if (Number.isFinite(up))
            this._set('uptime', formatUptime(up));
    }

    // --------------------------------------------------------- Autodarts

    _pollAutodarts() {
        if (this._adBusy || !this._session)
            return;
        this._adBusy = true;
        const port = this._ports[this._portIdx % this._ports.length];
        const msg = Soup.Message.new('GET', `http://127.0.0.1:${port}/api/system`);
        this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancellable, (session, res) => {
            this._adBusy = false;
            let data = null;
            try {
                const bytes = session.send_and_read_finish(res);
                if (msg.get_status() === Soup.Status.OK)
                    data = JSON.parse(decoder.decode(bytes.get_data()));
            } catch (e) {
                if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
            }
            if (!this._box)
                return;
            if (data) {
                this._adFailures = 0;
            } else {
                // Try the next candidate port and re-read config.toml in case Autodarts moved.
                this._portIdx++;
                if (this._portIdx % this._ports.length === 0)
                    this._ports = autodartsPorts();
                // A single slow reply under load should not flash "offline".
                if (++this._adFailures < 2)
                    return;
            }
            this._renderAutodarts(data);
        });
    }

    _renderAutodarts(data) {
        this._lastAd = data;
        const ad = this._ad;
        const running = data?.camState?.isRunning ?? true;

        if (ad.pill) {
            let text = 'Offline', cls = 'suit-hwmon-bad';
            if (data && !running)
                [text, cls] = ['Standby', 'suit-hwmon-warn'];
            else if (data)
                [text, cls] = [capitalize(data.turn?.status ?? data.state?.status ?? 'running'), 'suit-hwmon-ok'];
            ad.pill.text = text;
            setLevel(ad.pill, cls);
            const version = data?.status?.version ?? data?.version ?? '';
            ad.version.text = version ? `v${String(version).replace(/^v/, '')}` : '';
        }

        if (ad.toggle) {
            ad.toggle.label = data && !running ? 'Start' : 'Stop';
            ad.toggle.reactive = Boolean(data);
            setLevel(ad.toggle, data ? null : 'suit-hwmon-dim');
        }
        for (const btn of [ad.reset, ad.dump]) {
            if (btn) {
                btn.reactive = Boolean(data);
                setLevel(btn, data ? null : 'suit-hwmon-dim');
            }
        }

        if (ad.update && !this._updateArmed) {
            const version = String(data?.status?.updateAvailable ?? '').trim().replace(/^v/, '');
            ad.update.visible = version !== '';
            ad.update.label = `Update to v${version}`;
        }

        const target = data?.config?.cam?.fps ?? 30;
        const fps = (data?.camStats ?? []).map(c => Number(c.fps) || 0);
        if (ad.cams)
            this._setCams(fps, running ? target : null, Boolean(data?.camState?.isOpened));

        if (fps.length && running) {
            const avg = fps.reduce((a, b) => a + b, 0) / fps.length;
            const now = Date.now();
            this._fpsLows.push({ t: now, v: Math.min(...fps) });
            this._fpsLows = this._fpsLows.filter(s => now - s.t <= FPS_LOW_WINDOW_MS);
            if (ad.fps) {
                const cam = data.camStats[0];
                this._showFps(true);
                ad.fps.text = avg.toFixed(1);
                setLevel(ad.fps, fpsLevel(avg, target));
                ad.fpsLow.text = `1m low ${Math.min(...this._fpsLows.map(s => s.v))}`;
                ad.fpsRes.text = cam?.width ? `${cam.width}×${cam.height}` : '';
            }
        } else {
            // Standby streams black frames; keep those zeros out of the 1m low.
            this._fpsLows = [];
            if (ad.fps) {
                this._showFps(false);
                if (!data)
                    ad.fpsNote.text = 'Autodarts is not running';
                else if (fps.length)
                    ad.fpsNote.text = 'Cameras in standby';
                else
                    ad.fpsNote.text = running ? 'No cameras found' : 'Cameras paused';
            }
        }

        const stats = data?.stats ?? {};
        if (!data)
            this._set('proc', '—');
        else if (stats.cpuPercent !== undefined)
            this._set('proc', `${(Number(stats.cpuPercent) || 0).toFixed(0)}%`, formatBytes(Number(stats.memoryBytes) || 0));
    }

    _showFps(live) {
        const ad = this._ad;
        ad.fps.visible = live;
        ad.fpsUnit.visible = live;
        ad.fpsLow.visible = live;
        ad.fpsRes.visible = live;
        ad.fpsNote.visible = !live;
    }

    // Always at least MIN_CAM_TILES tiles. A tile shows a dash unless its camera is really delivering
    // frames: Autodarts lists a 0 fps entry for a slot with nothing connected.
    _setCams(fps, target, opened) {
        const ad = this._ad;
        const count = Math.max(MIN_CAM_TILES, fps.length);
        if (ad.camLabels.length !== count) {
            ad.cams.destroy_all_children();
            ad.camLabels = Array.from({ length: count }, (_, i) => {
                const tile = vbox('suit-hwmon-cam');
                tile.x_expand = true;
                const lbl = new St.Label({ text: '—', style_class: 'suit-hwmon-cam-fps', x_align: Clutter.ActorAlign.CENTER });
                tile.add_child(lbl);
                tile.add_child(new St.Label({ text: `CAM ${i + 1}`, style_class: 'suit-hwmon-cam-name', x_align: Clutter.ActorAlign.CENTER }));
                ad.cams.add_child(tile);
                return lbl;
            });
        }
        ad.cams.visible = true;
        ad.camLabels.forEach((lbl, i) => {
            const v = fps[i];
            const live = target !== null && v !== undefined && (opened || v > 0);
            lbl.text = live ? String(Math.round(v)) : '—';
            setLevel(lbl, live ? fpsLevel(v, target) : 'suit-hwmon-dim');
        });
    }
}
