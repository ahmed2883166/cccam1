package com.tvaitranslator;

import android.app.Service;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.os.Build;
import android.content.Intent;
import android.content.Context;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.os.Bundle;
import android.util.Log;

import com.google.mlkit.nl.languageid.LanguageIdentification;
import com.google.mlkit.nl.languageid.LanguageIdentifier;
import com.google.mlkit.nl.translate.TranslateLanguage;
import com.google.mlkit.nl.translate.Translation;
import com.google.mlkit.nl.translate.Translator;
import com.google.mlkit.nl.translate.TranslatorOptions;

import java.util.ArrayList;
import java.util.Locale;

public class LiveAiService extends Service implements RecognitionListener {
    public static final String ACTION_START = "com.tvaitranslator.AI_START";
    public static final String EXTRA_SPEECH_LOCALE = "speech_locale";
    private static final String TAG = "LiveAiTranslator";

    private final Handler handler = new Handler(Looper.getMainLooper());
    private SpeechRecognizer speechRecognizer;
    private LanguageIdentifier languageIdentifier;
    private Translator translator;
    private String translatorSource = "";
    private String speechLocale = "en-US";
    private boolean running = false;
    private boolean listening = false;

    private final Runnable restartListening = new Runnable() {
        @Override public void run() {
            if (!running || speechRecognizer == null || listening) return;
            try {
                Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
                intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
                intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, speechLocale);
                intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, speechLocale);
                intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
                intent.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
                intent.putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, false);
                intent.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 900L);
                speechRecognizer.startListening(intent);
                listening = true;
            } catch (Exception e) {
                listening = false;
                handler.postDelayed(this, 1500);
            }
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        startListeningNotification();
        languageIdentifier = LanguageIdentification.getClient();
    }

    private void startListeningNotification() {
        final String channelId = "tv_ai_live_translation";
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(channelId, "Live TV translation", NotificationManager.IMPORTANCE_LOW);
            NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (manager != null) manager.createNotificationChannel(channel);
        }
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
            ? new Notification.Builder(this, channelId)
            : new Notification.Builder(this);
        Notification notification = builder
            .setContentTitle("TV AI Translator")
            .setContentText("الاستماع للصوت وترجمته إلى العربية يعمل")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build();
        startForeground(27, notification);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null) {
            String requestedLocale = intent.getStringExtra(EXTRA_SPEECH_LOCALE);
            if (requestedLocale != null && requestedLocale.trim().length() > 0) speechLocale = requestedLocale.trim();
        }
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            sendCaption("خدمة التعرف على الكلام غير متاحة على هذا الجهاز");
            stopSelf();
            return START_NOT_STICKY;
        }
        running = true;
        Intent overlay = new Intent(this, SubtitleOverlayService.class);
        overlay.setAction(SubtitleOverlayService.ACTION_LIVE);
        startService(overlay);
        if (speechRecognizer == null) {
            speechRecognizer = SpeechRecognizer.createSpeechRecognizer(this);
            speechRecognizer.setRecognitionListener(this);
        }
        handler.removeCallbacks(restartListening);
        listening = false;
        handler.post(restartListening);
        return START_NOT_STICKY;
    }

    private void recognizeAndTranslate(String spokenText) {
        if (spokenText == null || spokenText.trim().isEmpty() || languageIdentifier == null) return;
        final String clean = spokenText.trim();
        languageIdentifier.identifyLanguage(clean)
            .addOnSuccessListener(language -> {
                String source = TranslateLanguage.fromLanguageTag(language);
                if (source == null || "und".equals(language)) {
                    sendCaption(clean + "\n[تعذر تحديد اللغة]");
                    return;
                }
                if (TranslateLanguage.ARABIC.equals(source)) {
                    sendCaption(clean);
                    return;
                }
                ensureTranslator(source, clean);
            })
            .addOnFailureListener(error -> sendCaption(clean));
    }

    private void ensureTranslator(String source, String text) {
        if (translator == null || !source.equals(translatorSource)) {
            if (translator != null) translator.close();
            translatorSource = source;
            TranslatorOptions options = new TranslatorOptions.Builder()
                .setSourceLanguage(source)
                .setTargetLanguage(TranslateLanguage.ARABIC)
                .build();
            translator = Translation.getClient(options);
            final Translator active = translator;
            active.downloadModelIfNeeded()
                .addOnSuccessListener(unused -> translate(active, text))
                .addOnFailureListener(error -> sendCaption("تعذر تنزيل نموذج الترجمة. اتصل بالإنترنت مرة واحدة.\n" + text));
        } else {
            final Translator active = translator;
            active.downloadModelIfNeeded()
                .addOnSuccessListener(unused -> translate(active, text))
                .addOnFailureListener(error -> sendCaption(text));
        }
    }

    private void translate(Translator active, String text) {
        active.translate(text)
            .addOnSuccessListener(arabic -> sendCaption(arabic))
            .addOnFailureListener(error -> sendCaption(text));
    }

    private void sendCaption(String text) {
        Intent update = new Intent(this, SubtitleOverlayService.class);
        update.setAction(SubtitleOverlayService.ACTION_TEXT);
        update.putExtra(SubtitleOverlayService.EXTRA_TEXT, text);
        startService(update);
    }

    @Override public void onReadyForSpeech(Bundle params) { }
    @Override public void onBeginningOfSpeech() { }
    @Override public void onRmsChanged(float rmsdB) { }
    @Override public void onBufferReceived(byte[] buffer) { }
    @Override public void onEndOfSpeech() { listening = false; }
    @Override public void onError(int error) {
        listening = false;
        if (running) {
            handler.removeCallbacks(restartListening);
            handler.postDelayed(restartListening, 900);
        }
    }
    @Override public void onResults(Bundle results) {
        listening = false;
        ArrayList<String> matches = results == null ? null : results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        if (matches != null && !matches.isEmpty()) recognizeAndTranslate(matches.get(0));
        if (running) {
            handler.removeCallbacks(restartListening);
            handler.postDelayed(restartListening, 350);
        }
    }
    @Override public void onPartialResults(Bundle partialResults) { }
    @Override public void onEvent(int eventType, Bundle params) { }

    @Override public void onDestroy() {
        running = false;
        handler.removeCallbacksAndMessages(null);
        if (speechRecognizer != null) {
            try { speechRecognizer.cancel(); speechRecognizer.destroy(); } catch (Exception ignored) { }
            speechRecognizer = null;
        }
        if (translator != null) { translator.close(); translator = null; }
        if (languageIdentifier != null) { languageIdentifier.close(); languageIdentifier = null; }
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
