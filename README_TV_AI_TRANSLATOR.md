# TV AI Translator

Android TV subtitle overlay prototype for Android 7+.

## Planned first milestone
- Lightweight subtitle overlay UI.
- Manual SRT selection and playback timeline controls.
- Free subtitle offset control (advance/delay) with reset and per-player profile storage.
- Player detection starting with `com.amlogic.dvbplayer` and an extensible player profile model.
- GitHub Actions build workflow for an APK.

## Compatibility notes
- Overlay requires the user to grant Android's draw-over-other-apps permission.
- Reading another app's playback audio is not generally available to ordinary apps on Android 7.1.1. Live speech recognition must not be considered supported until device-specific audio capture is verified.
- Player identification does not imply access to its EPG data or playback clock.

This is a staged prototype; device compatibility and APK build must be verified before claiming release readiness.
