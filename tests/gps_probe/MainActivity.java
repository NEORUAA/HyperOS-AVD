package io.github.hyperosavd.gpsprobe;

import android.app.Activity;
import android.location.GnssStatus;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import android.os.Handler;
import android.util.Log;
import android.view.WindowManager;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

public class MainActivity extends Activity {
    private LocationManager locationManager;
    private TextView text;
    private File trace;
    private final Handler handler = new Handler();
    private int starts;
    private int stops;
    private String firstFix = "Waiting";
    private String latestFix = "Waiting";

    private final LocationListener listener = location -> {
        String fix = "provider=" + location.getProvider()
                + " lat=" + location.getLatitude() + " lon=" + location.getLongitude()
                + " mock=" + location.isMock();
        if (firstFix.equals("Waiting")) firstFix = fix;
        latestFix = fix;
        record("LOCATION " + fix);
    };

    private final GnssStatus.Callback status = new GnssStatus.Callback() {
        @Override public void onStarted() { starts++; record("GNSS STARTED"); }
        @Override public void onStopped() { stops++; record("GNSS STOPPED"); }
        @Override public void onFirstFix(int milliseconds) { record("GNSS FIX " + milliseconds); }
        @Override public void onSatelliteStatusChanged(GnssStatus satellites) {
            record("SATELLITES " + satellites.getSatelliteCount());
        }
    };

    private void record(String message) {
        Log.i("HyperOSGpsProbe", message);
        try (FileOutputStream output = new FileOutputStream(trace, true)) {
            output.write((message + "\n").getBytes(StandardCharsets.UTF_8));
        } catch (IOException error) {
            Log.e("HyperOSGpsProbe", "Trace write failed", error);
        }
        text.setText("HyperOS GPS Probe\n\nGNSS starts / stops: " + starts + " / " + stops
                + "\n\nFirst fix\n" + firstFix + "\n\nLatest fix\n" + latestFix
                + "\n\n" + message);
    }

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        trace = new File(getFilesDir(), "gps-probe.txt");
        trace.delete();
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        text = new TextView(this);
        text.setTextColor(0xff111111);
        text.setTextSize(18);
        text.setPadding(32, 140, 32, 32);
        setContentView(text);
        locationManager = getSystemService(LocationManager.class);
        try {
            locationManager.registerGnssStatusCallback(status, handler);
            cycle(0);
        } catch (Exception error) {
            record("ERROR " + error);
        }
    }

    private void cycle(int index) {
        try {
            record("CYCLE " + index + " REQUEST GPS/FUSED");
            locationManager.requestLocationUpdates("gps", 1000, 0, listener);
            locationManager.requestLocationUpdates("fused", 1000, 0, listener);
            handler.postDelayed(() -> {
                record("CYCLE " + index + " STOP REQUEST");
                locationManager.removeUpdates(listener);
                if (index < 3) {
                    handler.postDelayed(() -> cycle(index + 1), 4000);
                } else {
                    handler.postDelayed(() -> {
                        locationManager.unregisterGnssStatusCallback(status);
                        record("COMPLETE");
                    }, 4000);
                }
            }, 12000);
        } catch (Exception error) {
            record("ERROR " + error);
        }
    }

    @Override protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        locationManager.removeUpdates(listener);
        locationManager.unregisterGnssStatusCallback(status);
        super.onDestroy();
    }
}
