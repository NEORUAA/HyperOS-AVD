"""Build the verified rear-panel double-tap wake bridge for root app_process."""
import hashlib
import os
from pathlib import Path
import subprocess
import zipfile

CLASS = 'io.github.hyperosavd.RearDisplayWake'
JAR_PATH = 'system_ext/framework/rear-display-wake.jar'
SCRIPT_PATH = 'system_ext/bin/rear-display-wake'
LABEL = 'u:object_r:system_file:s0'
UNIQUE_ID = 'local:4619827551948147201'
INPUT_NAME = 'virtio_input_multi_touch_7'
LAUNCHER = f'''#!/system/bin/sh
export CLASSPATH=/{JAR_PATH}
exec /system/bin/app_process /system/bin {CLASS}
'''.encode()

# No Android imports are needed: the original display Binder interface is
# reflected by name, permitting host tests to execute this same Java parser.
# Each first observed tracking-ID DOWN queries current Binder state. The
# independent evdev consumers require runtime verification of sleep/wake order.
JAVA_SOURCE = r'''package io.github.hyperosavd;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.lang.reflect.Method;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class RearDisplayWake {
    static final String UNIQUE_ID = "local:4619827551948147201";
    static final String INPUT_NAME = "virtio_input_multi_touch_7";
    static final int DISPLAY_ID = 1;
    static final int GROUP_ID = 1;
    static final long MAX_TAP_MS = 250;
    static final long MAX_GAP_MS = 400;
    static final double MOVE_LIMIT = 24.0;
    static final double TAP_DISTANCE = 100.0;
    static final double AXIS_MAX = 32767.0;
    static final double PANEL_WIDTH = 912.0, PANEL_HEIGHT = 596.0;

    static final class Snapshot {
        final int state;
        final long asleepSince;
        Snapshot(String uniqueId, int displayId, int groupId, int state) {
            this(uniqueId, displayId, groupId, state, Long.MIN_VALUE);
        }
        Snapshot(String uniqueId, int displayId, int groupId, int state, long asleepSince) {
            if (!UNIQUE_ID.equals(uniqueId) || displayId != DISPLAY_ID || groupId != GROUP_ID) {
                throw new IllegalStateException("Rear display identity/group changed");
            }
            this.state = state;
            this.asleepSince = asleepSince;
        }
        boolean asleep() { return state == 1 || state == 3 || state == 4; }
    }

    interface DisplayState { Snapshot snapshot() throws Exception; }
    interface Wake { void run() throws Exception; }

    static final class DisplayBinder implements DisplayState {
        final Object manager;
        final Method getInfo, uptimeMillis;
        Snapshot observed;
        volatile Exception failure;
        DisplayBinder() throws Exception {
            Object binder = Class.forName("android.os.ServiceManager")
                    .getMethod("getService", String.class).invoke(null, "display");
            if (binder == null) throw new IllegalStateException("Display service unavailable");
            manager = Class.forName("android.hardware.display.IDisplayManager$Stub")
                    .getMethod("asInterface", Class.forName("android.os.IBinder"))
                    .invoke(null, binder);
            getInfo = Class.forName("android.hardware.display.IDisplayManager")
                    .getMethod("getDisplayInfo", int.class);
            uptimeMillis = Class.forName("android.os.SystemClock").getMethod("uptimeMillis");
        }
        static Snapshot observe(Snapshot previous, Snapshot current, long uptime) {
            long since = previous != null && previous.state == current.state
                    ? previous.asleepSince : uptime;
            return new Snapshot(UNIQUE_ID, DISPLAY_ID, GROUP_ID, current.state,
                    current.asleep() ? since : Long.MAX_VALUE);
        }
        public synchronized Snapshot snapshot() throws Exception {
            if (failure != null) throw new IllegalStateException("Rear display observer failed", failure);
            Object info = getInfo.invoke(manager, DISPLAY_ID);
            if (info == null) throw new IllegalStateException("Rear display unavailable");
            Class<?> type = Class.forName("android.view.DisplayInfo");
            Snapshot current = new Snapshot((String) type.getField("uniqueId").get(info),
                    type.getField("displayId").getInt(info),
                    type.getField("displayGroupId").getInt(info),
                    type.getField("state").getInt(info));
            observed = observe(observed, current, ((Number) uptimeMillis.invoke(null)).longValue());
            return observed;
        }
    }

    static final class Detector {
        final DisplayState display;
        final Wake wake;
        boolean down, valid;
        long started, firstStarted, firstUp = -1;
        double x = Double.NaN, y, firstX, firstY;
        Detector(DisplayState display, Wake wake) { this.display = display; this.wake = wake; }
        void begin(long time) throws Exception {
            if (down) { cancel(); return; }
            down = true;
            started = time;
            x = Double.NaN;
            Snapshot state = display.snapshot();
            valid = state.asleep() && time >= state.asleepSince;
            if (!valid || time < firstUp || time - firstUp > MAX_GAP_MS
                    || (firstUp >= 0 && firstStarted < state.asleepSince)) firstUp = -1;
        }
        void position(int rawX, int rawY) {
            if (!down || !valid) return;
            if (rawX < 0 || rawY < 0 || rawX > AXIS_MAX || rawY > AXIS_MAX) {
                cancel(); return;
            }
            double nx = rawX / AXIS_MAX * PANEL_WIDTH, ny = rawY / AXIS_MAX * PANEL_HEIGHT;
            if (Double.isNaN(x)) { x = nx; y = ny; }
            else if (distance(x, y, nx, ny) > MOVE_LIMIT) cancel();
        }
        void cancel() { valid = false; firstUp = -1; }
        void end(long time) throws Exception {
            if (!down) return;
            down = false;
            if (!valid || Double.isNaN(x) || time < started || time - started > MAX_TAP_MS) {
                firstUp = -1; return;
            }
            if (firstUp >= 0 && started >= firstUp && started - firstUp <= MAX_GAP_MS
                    && distance(x, y, firstX, firstY) <= TAP_DISTANCE) {
                firstUp = -1;
                // Recheck at recognition; an independently awakened panel
                // must not receive another wake key from a stale first tap.
                Snapshot state = display.snapshot();
                if (state.asleep() && started >= state.asleepSince
                        && firstStarted >= state.asleepSince) wake.run();
            } else {
                firstUp = time; firstStarted = started; firstX = x; firstY = y;
            }
        }
        static double distance(double x1, double y1, double x2, double y2) {
            return Math.hypot(x1 - x2, y1 - y2);
        }
    }

    static final class Point { int x = -1, y = -1; }
    static final class EventReader {
        static final Pattern EVENT = Pattern.compile(
                "\\[\\s*([0-9]+\\.[0-9]+)\\]\\s+(?:(/dev/input/event[0-9]+):\\s+)?"
                + "(EV_[A-Z]+)\\s+([A-Z0-9_]+)\\s+([a-fA-F0-9]{8}|DOWN|UP)\\s*");
        final Detector detector;
        final String device;
        final Point[] points = new Point[11];
        final int[] lastX = new int[11], lastY = new int[11];
        int slot, active;
        EventReader(Detector detector, String device) {
            this.detector = detector; this.device = device;
            for (int i = 0; i < points.length; i++) { lastX[i] = -1; lastY[i] = -1; }
        }
        static long timestampMillis(String value) {
            int dot = value.indexOf('.');
            String fraction = value.substring(dot + 1) + "000";
            return Long.parseLong(value.substring(0, dot)) * 1000
                    + Long.parseLong(fraction.substring(0, 3));
        }
        void feed(String line) throws Exception {
            Matcher event = EVENT.matcher(line);
            if (!event.matches()) return;
            if (event.group(2) != null && !device.equals(event.group(2))) {
                throw new IllegalStateException("Unexpected input device in event stream");
            }
            long time = timestampMillis(event.group(1));
            String type = event.group(3), code = event.group(4);
            String text = event.group(5);
            int value = text.equals("DOWN") ? 1 : text.equals("UP") ? 0
                    : (int) Long.parseLong(text, 16);
            if (type.equals("EV_ABS")) {
                if (code.equals("ABS_MT_SLOT")) {
                    if (value < 0 || value >= points.length) throw new IllegalStateException("Invalid MT slot");
                    slot = value;
                } else if (code.equals("ABS_MT_TRACKING_ID")) {
                    if (value == -1) {
                        if (points[slot] != null) { points[slot] = null; active--; }
                    } else {
                        if (value < 0 || value > 65535) throw new IllegalStateException("Invalid tracking ID");
                        if (points[slot] != null) detector.cancel();
                        else {
                            points[slot] = new Point();
                            // Type B retains slot axes after UP; input core can
                            // omit unchanged positions on the next tracking ID.
                            points[slot].x = lastX[slot]; points[slot].y = lastY[slot];
                            active++;
                        }
                        if (active == 1 && !detector.down) detector.begin(time);
                        else detector.cancel();
                    }
                } else {
                    // Protocol B does not require axes to follow tracking ID.
                    if (code.equals("ABS_MT_POSITION_X")) {
                        lastX[slot] = value;
                        if (points[slot] != null) points[slot].x = value;
                    }
                    if (code.equals("ABS_MT_POSITION_Y")) {
                        lastY[slot] = value;
                        if (points[slot] != null) points[slot].y = value;
                    }
                }
            } else if (type.equals("EV_SYN")) {
                if (code.equals("SYN_DROPPED")) throw new IllegalStateException("Input events dropped");
                if (code.equals("SYN_REPORT")) {
                    if (active == 0) detector.end(time);
                    else if (active == 1) {
                        for (Point point : points) {
                            if (point != null && point.x >= 0 && point.y >= 0) {
                                detector.position(point.x, point.y); break;
                            }
                        }
                    } else detector.cancel();
                }
            }
        }
    }

    static String read(File file) throws Exception {
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                new FileInputStream(file), "UTF-8"))) {
            String line;
            while ((line = reader.readLine()) != null) result.append(line).append('\n');
        }
        return result.toString();
    }
    static void validateIdc(String data) {
        int association = 0;
        for (String line : data.split("\\n")) {
            line = line.split("#", 2)[0].trim();
            String[] item = line.split("\\s*=\\s*", 2);
            if (item.length != 2) continue;
            if (item[0].equals("touch.displayId")) {
                if (!item[1].equals(UNIQUE_ID)) throw new IllegalStateException("Rear IDC display association changed");
                association++;
            }
            if (item[0].equals("touch.wake") && !item[1].equals("false") && !item[1].equals("0")) {
                throw new IllegalStateException("Single-touch wake is incompatible with double-tap wake");
            }
        }
        if (association != 1) throw new IllegalStateException("Missing/ambiguous rear IDC association");
    }
    static String findDevice() throws Exception {
        validateIdc(read(new File("/vendor/usr/idc/" + INPUT_NAME + ".idc")));
        File[] entries = new File("/sys/class/input").listFiles();
        if (entries == null) throw new IllegalStateException("Input sysfs unavailable");
        String result = null;
        for (File entry : entries) {
            if (!entry.getName().matches("event[0-9]+")) continue;
            if (INPUT_NAME.equals(read(new File(entry, "device/name")).trim())) {
                if (result != null) throw new IllegalStateException("Ambiguous rear input device");
                result = "/dev/input/" + entry.getName();
            }
        }
        if (result == null || !new File(result).exists()) throw new IllegalStateException("Rear input device unavailable");
        return result;
    }

    public static void main(String[] args) throws Exception {
        final DisplayBinder display = new DisplayBinder();
        display.snapshot();
        String device = findDevice();
        Detector detector = new Detector(display, new Wake() {
            public void run() throws Exception {
                Process input = new ProcessBuilder("/system/bin/input", "-d", "1", "keyevent", "224")
                        .redirectErrorStream(true).start();
                try (BufferedReader output = new BufferedReader(new InputStreamReader(input.getInputStream(), "UTF-8"))) {
                    while (output.readLine() != null) { }
                }
                if (input.waitFor() != 0) throw new IllegalStateException("Rear wake key injection failed");
                System.err.println("rear-display-wake: rear double-tap wake");
            }
        });
        EventReader events = new EventReader(detector, device);
        final Process input = new ProcessBuilder("/system/bin/getevent", "-lt", device)
                .redirectError(ProcessBuilder.Redirect.INHERIT).start();
        Thread observer = new Thread(new Runnable() {
            public void run() {
                while (!Thread.currentThread().isInterrupted()) {
                    try {
                        display.snapshot();
                        Thread.sleep(20);
                    } catch (InterruptedException stopped) {
                        return;
                    } catch (Exception error) {
                        display.failure = error;
                        System.err.println("rear-display-wake: display observer failed: " + error);
                        input.destroy();
                        return;
                    }
                }
            }
        }, "rear-display-state");
        observer.setDaemon(true);
        observer.start();
        System.err.println("rear-display-wake: monitoring " + device + " for " + UNIQUE_ID);
        try (BufferedReader lines = new BufferedReader(new InputStreamReader(input.getInputStream(), "UTF-8"))) {
            String line;
            while ((line = lines.readLine()) != null) events.feed(line);
        } finally {
            observer.interrupt();
            input.destroy();
        }
        throw new IllegalStateException("Rear input event stream ended");
    }
}
'''


def compile_assets(sdk, work):
    """Compile one app_process DEX jar locally, without installing anything."""
    from patch_gnss import java
    sdk, work = Path(sdk), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    source = work / 'src/io/github/hyperosavd/RearDisplayWake.java'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(JAVA_SOURCE)
    revision = hashlib.sha256(JAVA_SOURCE.encode()).hexdigest()[:16]
    classes, dex = work / 'classes' / revision, work / 'dex' / revision
    classes.mkdir(parents=True, exist_ok=True)
    dex.mkdir(parents=True, exist_ok=True)
    jdk = Path(java()).parent.parent
    android = sdk / 'platforms/android-36/android.jar'
    environment = {**os.environ, 'JAVA_HOME': str(jdk)}
    subprocess.run([str(jdk / 'bin/javac'), '-source', '8', '-target', '8',
                    '-classpath', str(android), '-d', str(classes), str(source)],
                   check=True, capture_output=True, env=environment)
    compiled = sorted(classes.glob('io/github/hyperosavd/RearDisplayWake*.class'))
    subprocess.run([str(sdk / 'build-tools/37.0.0/d8'), '--min-api', '26',
                    '--lib', str(android), '--output', str(dex), *map(str, compiled)],
                   check=True, capture_output=True, env=environment)
    jar = work / 'rear-display-wake.jar'
    with zipfile.ZipFile(jar, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo('classes.dex', (1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(entry, (dex / 'classes.dex').read_bytes())
    (work / 'rear-display-wake').write_bytes(LAUNCHER)
    return jar.read_bytes()


def image_replacements(sdk, work):
    """Return (image edits, manifest); root owns KSU startup and runtime checks."""
    jar = compile_assets(sdk, work)
    manifest = {'schema': 1, 'revision': 2, 'display_id': 1, 'display_group_id': 1,
                'display_unique_id': UNIQUE_ID, 'input_name': INPUT_NAME,
                'axis_range': [0, 32767], 'panel_geometry': [912, 596],
                'max_tap_ms': 250, 'max_gap_ms': 400,
                'movement_pixels': 24, 'tap_distance_pixels': 100,
                'state_poll_ms': 20, 'require_down_after_observed_sleep': True,
                'sleep_states': ['OFF', 'DOZE', 'DOZE_SUSPEND'],
                'source_sha256': hashlib.sha256(JAVA_SOURCE.encode()).hexdigest(),
                'jar_path': '/' + JAR_PATH, 'jar_sha256': hashlib.sha256(jar).hexdigest(),
                'script_path': '/' + SCRIPT_PATH,
                'script_sha256': hashlib.sha256(LAUNCHER).hexdigest()}
    return {JAR_PATH: (jar, 0o644, LABEL), SCRIPT_PATH: (LAUNCHER, 0o755, LABEL)}, manifest
