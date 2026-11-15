package com.tvaitranslator;

import android.app.Service;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.net.Uri;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.WindowManager;
import android.view.WindowManager.LayoutParams;
import android.widget.TextView;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class SubtitleOverlayService extends Service {
    public static final String ACTION_START = "com.tvaitranslator.START";
    public static final String ACTION_OFFSET = "com.tvaitranslator.OFFSET";
    public static final String ACTION_LIVE = "com.tvaitranslator.LIVE";
    public static final String ACTION_TEXT = "com.tvaitranslator.TEXT";
    public static final String EXTRA_TEXT = "text";
    public static final String EXTRA_OFFSET = "offset";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final List<Cue> cues = new ArrayList<>();
    private WindowManager windowManager;
    private TextView subtitleView;
    private long startedAt;
    private int offsetMs;
    private boolean attached;

    private final Runnable ticker = new Runnable() {
        @Override public void run() {
            if (subtitleView == null) return;
            long timeline = SystemClock.elapsedRealtime() - startedAt - offsetMs;
            String current = "";
            for (Cue cue : cues) {
                if (timeline >= cue.start && timeline <= cue.end) { current = cue.text; break; }
            }
            subtitleView.setText(current);
            handler.postDelayed(this, 150);
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) return START_NOT_STICKY;
        if (ACTION_OFFSET.equals(intent.getAction())) {
            offsetMs = intent.getIntExtra(EXTRA_OFFSET, 0);
            return START_NOT_STICKY;
        }
        if (ACTION_LIVE.equals(intent.getAction())) {
            handler.removeCallbacks(ticker);
            attachOverlay();
            subtitleView.setText("استمع الآن... جاري تجهيز الترجمة الذكية");
            return START_NOT_STICKY;
        }
        if (ACTION_TEXT.equals(intent.getAction())) {
            handler.removeCallbacks(ticker);
            attachOverlay();
            String value = intent.getStringExtra(EXTRA_TEXT);
            subtitleView.setText(value == null ? "" : value);
            return START_NOT_STICKY;
        }
        if (ACTION_START.equals(intent.getAction())) {
            Uri uri = intent.getData();
            offsetMs = intent.getIntExtra(EXTRA_OFFSET, 0);
            if (uri != null) {
                new Thread(() -> {
                    List<Cue> parsed = parse(uri);
                    handler.post(() -> {
                        cues.clear(); cues.addAll(parsed);
                        startedAt = SystemClock.elapsedRealtime();
                        attachOverlay();
                        handler.removeCallbacks(ticker);
                        handler.post(ticker);
                    });
                }).start();
            }
        }
        return START_NOT_STICKY;
    }

    private void attachOverlay() {
        if (subtitleView == null) {
            subtitleView = new TextView(this);
            subtitleView.setTextColor(Color.WHITE);
            subtitleView.setTextSize(23);
            subtitleView.setGravity(Gravity.CENTER);
            subtitleView.setShadowLayer(4, 2, 2, Color.BLACK);
            subtitleView.setBackgroundColor(0x66000000);
            subtitleView.setPadding(16, 8, 16, 8);
        }
        if (!attached) {
            int type = android.os.Build.VERSION.SDK_INT >= 26 ? LayoutParams.TYPE_APPLICATION_OVERLAY : LayoutParams.TYPE_PHONE;
            LayoutParams params = new LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT, type,
                LayoutParams.FLAG_NOT_FOCUSABLE | LayoutParams.FLAG_NOT_TOUCHABLE | LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
            params.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
            params.y = 36;
            windowManager.addView(subtitleView, params);
            attached = true;
        }
    }

    private List<Cue> parse(Uri uri) {
        List<Cue> result = new ArrayList<>();
        Pattern time = Pattern.compile("(\\d{2}):(\\d{2}):(\\d{2})[,.](\\d{3})\\s*-->\\s*(\\d{2}):(\\d{2}):(\\d{2})[,.](\\d{3})");
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(getContentResolver().openInputStream(uri), "UTF-8"))) {
            String line; long start = -1, end = -1; StringBuilder text = new StringBuilder();
            while ((line = reader.readLine()) != null) {
                Matcher m = time.matcher(line.trim());
                if (m.find()) {
                    if (start >= 0 && text.length() > 0) result.add(new Cue(start, end, text.toString().trim()));
                    start = stamp(m, 1); end = stamp(m, 5); text.setLength(0);
                } else if (line.trim().length() > 0 && !line.trim().matches("\\d+")) {
                    if (text.length() > 0) text.append("\n");
                    text.append(line.trim());
                } else if (line.trim().length() == 0 && start >= 0 && text.length() > 0) {
                    result.add(new Cue(start, end, text.toString().trim())); start = -1; text.setLength(0);
                }
            }
            if (start >= 0 && text.length() > 0) result.add(new Cue(start, end, text.toString().trim()));
        } catch (Exception ignored) { }
        Collections.sort(result, Comparator.comparingLong(c -> c.start));
        return result;
    }

    private long stamp(Matcher m, int i) {
        long h=Long.parseLong(m.group(i)), min=Long.parseLong(m.group(i+1)), sec=Long.parseLong(m.group(i+2)), ms=Long.parseLong(m.group(i+3));
        return ((h*60+min)*60+sec)*1000+ms;
    }
    @Override public void onDestroy() {
        handler.removeCallbacks(ticker);
        if (attached && subtitleView != null) { windowManager.removeView(subtitleView); attached = false; }
        super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }

    private static class Cue {
        final long start, end; final String text;
        Cue(long s,long e,String t) { start=s; end=e; text=t; }
    }
}
