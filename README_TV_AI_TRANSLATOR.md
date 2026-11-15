# TV AI Translator

Android 7+ prototype for displaying a selected SRT file over another player.

## Implemented in this milestone
- Select an SRT subtitle file through Android's document picker.
- Parse SRT time ranges and display the current cue in a system overlay.
- Request the Android "Display over other apps" permission when needed.
- Free manual subtitle offset from -10 to +10 minutes, with positive values delaying subtitles and negative values advancing them.
- Save offsets by the name entered for the channel/movie; changing the name immediately loads that profile.
- Reset offset to zero and stop the overlay.

## Build
GitHub Actions uses Gradle 7.5 and Java 11 to run `gradle assembleDebug`. This repository currently uses the installed Gradle distribution in Actions; it does not include the Gradle wrapper. The APK is only considered built after a successful Actions run and artifact upload.

## Important limitations
- The overlay subtitle clock starts when the user presses "تشغيل الترجمة"; it is not synchronized to the DVB player's playback clock. Pause, seek, channel changes, and live-program timing are not automatically detected.
- Player package identification does not grant access to EPG metadata or another app's audio.
- No speech recognition or machine translation service is connected yet. This milestone displays an existing SRT file only.
- Android 7.1.1 does not provide the standard Android 10 playback-capture API. Live transcription needs device-specific loopback support or a player integration and must be verified on the target box.
- Test the overlay and SRT timing on the actual Android TV box before treating it as release-ready.
