import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const POLL_SECONDS = 2;
const FPS_LOW_WINDOW_MS = 60 * 1000;
const BAR_WIDTH = 72;
const MARGIN = 24;
const AUTODARTS_CONFIG = GLib.build_filenamev([GLib.get_home_dir(), '.config', 'autodarts', 'config.toml']);
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

function level(value, warn, bad) {
    if (value >= bad)
        return 'suit-hwmon-bad';
    return value >= warn ? 'suit-hwmon-warn' : 'suit-hwmon-ok';
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
        this._cancellable = new Gio.Cancellable();

        this._buildUi();

        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._reposition());
        this._widthId = this._box.connect('notify::width', () => this._reposition());

        this._tick();
        this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, POLL_SECONDS, () => {
            this._tick();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
        this._cancellable?.cancel();
        this._session?.abort();
        if (this._monitorsId) {
            Main.layoutManager.disconnect(this._monitorsId);
            this._monitorsId = 0;
        }
        this._box?.destroy();
        this._box = null;
        this._session = null;
        this._cancellable = null;
        this._sensors = null;
        this._rows = null;
    }

    // ---------------------------------------------------------------- UI

    _buildUi() {
        this._box = vbox('suit-hwmon');
        this._box.reactive = false;
        this._rows = {};

        this._box.add_child(new St.Label({ text: 'SYSTEM', style_class: 'suit-hwmon-title' }));
        this._addRow('cpu', 'CPU', true);
        this._addRow('temp', 'TEMP', false);
        this._addRow('ram', 'RAM', true);
        if (this._sensors.gpuBusy || this._sensors.gpuTemp)
            this._addRow('gpu', 'GPU', Boolean(this._sensors.gpuBusy));
        this._addRow('disk', 'DISK', true);
        this._addRow('net', 'NET', false);
        this._addRow('load', 'LOAD', false);
        this._addRow('up', 'UP', false);

        this._box.add_child(new St.Widget({ style_class: 'suit-hwmon-sep' }));
        this._box.add_child(new St.Label({ text: 'AUTODARTS', style_class: 'suit-hwmon-title' }));
        this._addRow('engine', 'STATE', false);
        this._camBox = new St.BoxLayout({ style_class: 'suit-hwmon-row suit-hwmon-cams' });
        this._box.add_child(this._camBox);
        this._camLabels = [];
        this._addRow('avg', 'AVG', false);
        this._addRow('proc', 'PROC', false);

        Main.layoutManager._backgroundGroup.add_child(this._box);
        this._reposition();
    }

    _addRow(id, key, withBar) {
        const row = new St.BoxLayout({ style_class: 'suit-hwmon-row' });
        row.add_child(new St.Label({ text: key, style_class: 'suit-hwmon-key', y_align: Clutter.ActorAlign.CENTER }));
        const value = new St.Label({ text: '—', x_expand: true, y_align: Clutter.ActorAlign.CENTER });
        row.add_child(value);
        let fill = null;
        if (withBar) {
            const bar = new St.Widget({ style_class: 'suit-hwmon-bar', y_align: Clutter.ActorAlign.CENTER });
            fill = new St.Widget({ style_class: 'suit-hwmon-fill', width: 0 });
            bar.add_child(fill);
            row.add_child(bar);
        }
        this._box.add_child(row);
        this._rows[id] = { value, fill };
    }

    _set(id, text, cls = null, fraction = null) {
        const row = this._rows?.[id];
        if (!row)
            return;
        row.value.text = text;
        setLevel(row.value, cls);
        if (row.fill && fraction !== null) {
            const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
            row.fill.width = Math.round(Math.max(0, Math.min(1, fraction)) * BAR_WIDTH * scale);
            setLevel(row.fill, cls);
        }
    }

    _reposition() {
        const mon = Main.layoutManager.primaryMonitor;
        if (!this._box || !mon)
            return;
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const margin = MARGIN * scale;
        const panelHeight = Main.panel?.height ?? 0;
        this._box.set_position(
            Math.round(mon.x + mon.width - this._box.width - margin),
            Math.round(mon.y + panelHeight + margin));
    }

    // ---------------------------------------------------------- Sampling

    _tick() {
        if (!this._box)
            return;
        this._updateCpu();
        this._updateTemps();
        this._updateMemory();
        this._updateDisk();
        this._updateNetwork();
        this._updateLoad();
        this._pollAutodarts();
    }

    _updateCpu() {
        const line = (readText('/proc/stat') || '').split('\n')[0];
        const nums = line.trim().split(/\s+/).slice(1, 9).map(Number);
        if (nums.length < 4)
            return;
        const idle = nums[3] + (nums[4] || 0);
        const total = nums.reduce((a, b) => a + b, 0);
        let freq = '';
        const freqs = this._sensors.freqPaths.map(readInt).filter(v => v !== null);
        if (freqs.length)
            freq = `  ${(freqs.reduce((a, b) => a + b, 0) / freqs.length / 1e6).toFixed(2)} GHz`;
        if (this._prevCpu) {
            const dt = total - this._prevCpu.total;
            const usage = dt > 0 ? 100 * (1 - (idle - this._prevCpu.idle) / dt) : 0;
            this._set('cpu', `${usage.toFixed(0).padStart(3)}%${freq}`, level(usage, 70, 90), usage / 100);
        }
        this._prevCpu = { idle, total };
    }

    _updateTemps() {
        if (this._sensors.cpuTemp) {
            const raw = readInt(this._sensors.cpuTemp);
            if (raw !== null) {
                const c = raw / 1000;
                this._set('temp', `${c.toFixed(0)} °C`, level(c, 75, 90));
            }
        } else {
            this._set('temp', 'n/a', null);
        }

        if (this._rows.gpu) {
            const parts = [];
            let cls = null;
            const busy = this._sensors.gpuBusy ? readInt(this._sensors.gpuBusy) : null;
            if (busy !== null) {
                parts.push(`${String(busy).padStart(3)}%`);
                cls = level(busy, 70, 90);
            }
            const t = this._sensors.gpuTemp ? readInt(this._sensors.gpuTemp) : null;
            if (t !== null)
                parts.push(`${(t / 1000).toFixed(0)} °C`);
            this._set('gpu', parts.join('  ') || 'n/a', cls, busy !== null ? busy / 100 : null);
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
        this._set('ram', `${formatBytes(used)} / ${formatBytes(info.MemTotal)}`, level(pct, 80, 92), pct / 100);
    }

    _updateDisk() {
        try {
            const info = Gio.File.new_for_path('/').query_filesystem_info('filesystem::size,filesystem::free', null);
            const size = info.get_attribute_uint64('filesystem::size');
            const free = info.get_attribute_uint64('filesystem::free');
            if (size > 0) {
                const pct = 100 * (size - free) / size;
                this._set('disk', `${formatBytes(size - free)} / ${formatBytes(size)}`, level(pct, 85, 95), pct / 100);
            }
        } catch {
            this._set('disk', 'n/a', null);
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
                this._set('net', `↓ ${formatBytes(down)}/s  ↑ ${formatBytes(up)}/s`);
            }
        }
        this._prevNet = { rx, tx, time: now };
    }

    _updateLoad() {
        const load = (readText('/proc/loadavg') || '').trim().split(/\s+/).slice(0, 3);
        if (load.length === 3)
            this._set('load', load.join('  '));
        const up = parseFloat(readText('/proc/uptime') || '');
        if (Number.isFinite(up))
            this._set('up', formatUptime(up));
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
        if (!data) {
            this._set('engine', 'offline', 'suit-hwmon-bad');
            this._setCams([], 0);
            this._set('avg', '—');
            this._set('proc', '—');
            return;
        }

        const version = data.status?.version ?? data.version ?? '';
        const running = data.camState?.isRunning ?? true;
        const state = running ? (data.turn?.status ?? data.state?.status ?? 'running') : 'standby';
        this._set('engine', `${state}${version ? `  v${String(version).replace(/^v/, '')}` : ''}`,
            running ? 'suit-hwmon-ok' : 'suit-hwmon-warn');

        const target = data.config?.cam?.fps ?? 30;
        const fps = (data.camStats ?? []).map(c => Number(c.fps) || 0);
        this._setCams(fps, running ? target : null);

        if (fps.length && running) {
            const avg = fps.reduce((a, b) => a + b, 0) / fps.length;
            const now = Date.now();
            this._fpsLows.push({ t: now, v: Math.min(...fps) });
            this._fpsLows = this._fpsLows.filter(s => now - s.t <= FPS_LOW_WINDOW_MS);
            const low = Math.min(...this._fpsLows.map(s => s.v));
            const cam = data.camStats[0];
            const res = cam?.width ? `  ${cam.width}×${cam.height}` : '';
            this._set('avg', `${avg.toFixed(1)} fps  1m low ${low}${res}`, fpsLevel(avg, target));
        } else if (fps.length) {
            // Standby streams black frames; keep those zeros out of the 1m low.
            this._fpsLows = [];
            this._set('avg', 'cameras in standby', 'suit-hwmon-dim');
        } else {
            this._set('avg', running ? 'no cameras' : 'cameras paused', 'suit-hwmon-warn');
        }

        const stats = data.stats ?? {};
        if (stats.cpuPercent !== undefined) {
            const cpu = Number(stats.cpuPercent) || 0;
            this._set('proc', `CPU ${cpu.toFixed(0)}%  RAM ${formatBytes(Number(stats.memoryBytes) || 0)}`);
        }
    }

    _setCams(fps, target) {
        if (this._camLabels.length !== fps.length + 1) {
            this._camBox.destroy_all_children();
            this._camLabels = [new St.Label({ text: 'CAMS', style_class: 'suit-hwmon-key' })];
            this._camBox.add_child(this._camLabels[0]);
            for (let i = 0; i < fps.length; i++) {
                const lbl = new St.Label();
                this._camLabels.push(lbl);
                this._camBox.add_child(lbl);
            }
        }
        this._camBox.visible = fps.length > 0;
        fps.forEach((v, i) => {
            const lbl = this._camLabels[i + 1];
            lbl.text = `#${i + 1} ${String(v).padStart(2)}`;
            setLevel(lbl, target === null ? 'suit-hwmon-dim' : fpsLevel(v, target));
        });
    }
}
