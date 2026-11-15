package com.tvaitranslator;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.graphics.Color;
import android.view.Gravity;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.TextView;
import android.text.Editable;
import android.text.TextWatcher;
import android.widget.Toast;
import android.content.SharedPreferences;
import java.util.Locale;

public class MainActivity extends Activity {
    private static final int PICK_SRT = 41;
    private static final int MAX_OFFSET_MS = 600000;
    private SharedPreferences prefs;
    private TextView offsetLabel, fileLabel;
    private SeekBar offsetSlider;
    private EditText profileInput;
    private Uri selectedSrt;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("subtitle_profiles", MODE_PRIVATE);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(24, 20, 24, 20);
        root.setGravity(Gravity.CENTER_VERTICAL);
        root.setBackgroundColor(Color.rgb(18, 22, 30));

        root.addView(text("TV AI Translator", 25, Color.WHITE));
        root.addView(text("ترجمة ملفات SRT فوق مشغل القنوات", 17, Color.LTGRAY));

        profileInput = new EditText(this);
        profileInput.setSingleLine(true);
        profileInput.setTextColor(Color.WHITE);
        profileInput.setHintTextColor(Color.GRAY);
        profileInput.setHint("اكتب اسم القناة أو الفيلم");
        root.addView(profileInput);

        offsetLabel = text("التوقيت: 0.0 ثانية", 19, Color.WHITE);
        root.addView(offsetLabel);
        offsetSlider = new SeekBar(this);
        offsetSlider.setMax(1200);
        root.addView(offsetSlider);
        offsetSlider.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar bar, int value, boolean user) {
                int offset = Math.max(-MAX_OFFSET_MS, Math.min(MAX_OFFSET_MS, (value - 600) * 1000));
                showOffset(offset);
                prefs.edit().putInt(key(), offset).apply();
                Intent update = new Intent(MainActivity.this, SubtitleOverlayService.class);
                update.setAction(SubtitleOverlayService.ACTION_OFFSET);
                update.putExtra(SubtitleOverlayService.EXTRA_OFFSET, offset);
                startService(update);
            }
            @Override public void onStartTrackingTouch(SeekBar bar) { }
            @Override public void onStopTrackingTouch(SeekBar bar) { }
        });

        Button choose = new Button(this);
        choose.setText("اختيار ملف ترجمة SRT");
        choose.setOnClickListener(v -> {
            Intent pick = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            pick.addCategory(Intent.CATEGORY_OPENABLE);
            pick.setType("*/*");
            startActivityForResult(pick, PICK_SRT);
        });
        root.addView(choose);

        fileLabel = text("لم يتم اختيار ملف ترجمة", 14, Color.LTGRAY);
        root.addView(fileLabel);

        Button show = new Button(this);
        show.setText("تشغيل الترجمة فوق المشغل");
        show.setOnClickListener(v -> startOverlay());
        root.addView(show);

        Button stop = new Button(this);
        stop.setText("إيقاف الترجمة");
        stop.setOnClickListener(v -> stopService(new Intent(this, SubtitleOverlayService.class)));
        root.addView(stop);

        Button reset = new Button(this);
        reset.setText("إعادة التوقيت للصفر");
        reset.setOnClickListener(v -> offsetSlider.setProgress(600));
        root.addView(reset);

        profileInput.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s,int st,int c,int a) { }
            @Override public void onTextChanged(CharSequence s,int st,int before,int count) { loadProfile(); }
            @Override public void afterTextChanged(Editable e) { }
        });
        setContentView(root);
        profileInput.setText("عام");
        loadProfile();
    }

    private void startOverlay() {
        if (selectedSrt == null) {
            Toast.makeText(this, "اختار ملف SRT الأول", Toast.LENGTH_LONG).show();
            return;
        }
        if (!Settings.canDrawOverlays(this)) {
            Intent permission = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:" + getPackageName()));
            startActivity(permission);
            Toast.makeText(this, "فعّل السماح بالظهور فوق التطبيقات ثم اضغط تشغيل مرة أخرى", Toast.LENGTH_LONG).show();
            return;
        }
        Intent service = new Intent(this, SubtitleOverlayService.class);
        service.setAction(SubtitleOverlayService.ACTION_START);
        service.setData(selectedSrt);
        service.putExtra(SubtitleOverlayService.EXTRA_OFFSET, prefs.getInt(key(), 0));
        startService(service);
    }

    @Override protected void onActivityResult(int requestCode,int resultCode,Intent data) {
        super.onActivityResult(requestCode,resultCode,data);
        if (requestCode == PICK_SRT && resultCode == RESULT_OK && data != null) {
            selectedSrt = data.getData();
            if (selectedSrt != null) {
                getContentResolver().takePersistableUriPermission(selectedSrt, Intent.FLAG_GRANT_READ_URI_PERMISSION);
                fileLabel.setText("ملف الترجمة: " + selectedSrt.getLastPathSegment());
            }
        }
    }

    private TextView text(String value,int size,int color) {
        TextView t = new TextView(this); t.setText(value); t.setTextSize(size); t.setTextColor(color); t.setPadding(0,8,0,8); return t;
    }
    private String key() {
        String value = profileInput == null ? "" : profileInput.getText().toString().trim();
        return value.isEmpty() ? "default" : value;
    }
    private void showOffset(int millis) {
        double seconds = millis / 1000.0;
        String direction = millis > 0 ? "تأخير" : (millis < 0 ? "تقديم" : "بدون إزاحة");
        offsetLabel.setText(String.format(Locale.US, "%s: %+.1f ثانية", direction, seconds));
    }
    private void loadProfile() {
        if (profileInput == null || offsetSlider == null) return;
        int offset = prefs.getInt(key(), 0);
        offsetSlider.setProgress(Math.max(0, Math.min(1200, 600 + offset / 1000)));
        showOffset(offset);
    }
}
