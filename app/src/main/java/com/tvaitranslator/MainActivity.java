package com.tvaitranslator;

import android.app.Activity;
import android.os.Bundle;
import android.graphics.Color;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.TextView;
import android.content.SharedPreferences;
import java.util.Locale;

public class MainActivity extends Activity {
    private static final int MAX_OFFSET_MS = 600000;
    private SharedPreferences prefs;
    private TextView offsetLabel;
    private SeekBar offsetSlider;
    private EditText profileInput;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("subtitle_profiles", MODE_PRIVATE);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(28, 28, 28, 28);
        root.setGravity(Gravity.CENTER_VERTICAL);
        root.setBackgroundColor(Color.rgb(18, 22, 30));

        TextView title = text("TV AI Translator", 26, Color.WHITE);
        root.addView(title);
        TextView hint = text("تحكم حر في توقيت الترجمة", 18, Color.LTGRAY);
        root.addView(hint);

        profileInput = new EditText(this);
        profileInput.setSingleLine(true);
        profileInput.setTextColor(Color.WHITE);
        profileInput.setHintTextColor(Color.GRAY);
        profileInput.setHint("اسم القناة أو الفيلم");
        profileInput.setText("com.amlogic.dvbplayer");
        root.addView(profileInput);

        offsetLabel = text("التوقيت: 0.0 ثانية", 20, Color.WHITE);
        root.addView(offsetLabel);
        offsetSlider = new SeekBar(this);
        offsetSlider.setMax(1200);
        root.addView(offsetSlider);
        offsetSlider.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar bar, int value, boolean user) {
                int offset = (value - 600) * 1000;
                showOffset(offset);
            }
            @Override public void onStartTrackingTouch(SeekBar bar) { }
            @Override public void onStopTrackingTouch(SeekBar bar) { saveProfile(); }
        });

        Button reset = new Button(this);
        reset.setText("إعادة التوقيت للصفر");
        reset.setOnClickListener(v -> { offsetSlider.setProgress(600); saveProfile(); });
        root.addView(reset);

        Button save = new Button(this);
        save.setText("حفظ إعدادات هذا الفيلم / القناة");
        save.setOnClickListener(v -> saveProfile());
        root.addView(save);

        setContentView(root);
        loadProfile();
    }

    private TextView text(String value, int size, int color) {
        TextView t = new TextView(this); t.setText(value); t.setTextSize(size); t.setTextColor(color); t.setPadding(0, 12, 0, 12); return t;
    }
    private String key() { String s = profileInput.getText().toString().trim(); return s.isEmpty() ? "default" : s; }
    private void showOffset(int millis) {
        double seconds = millis / 1000.0;
        String direction = millis > 0 ? "تأخير" : (millis < 0 ? "تقديم" : "بدون إزاحة");
        offsetLabel.setText(String.format(Locale.US, "%s: %+.1f ثانية", direction, seconds));
    }
    private void loadProfile() {
        int offset = prefs.getInt(key(), 0);
        offsetSlider.setProgress(Math.max(0, Math.min(1200, 600 + offset / 1000)));
        showOffset(offset);
    }
    private void saveProfile() {
        int offset = (offsetSlider.getProgress() - 600) * 1000;
        prefs.edit().putInt(key(), offset).apply();
        showOffset(offset);
    }
}
