import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

export default class TouchScalingExtension extends Extension {
    enable() {
        if (Main.panel) {
            Main.panel.add_style_class_name('suit-touch-panel');
        }
    }

    disable() {
        if (Main.panel) {
            Main.panel.remove_style_class_name('suit-touch-panel');
        }
    }
}
