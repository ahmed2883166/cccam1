# TV AI Translator

Android 7+ prototype for Arabic live captions over a TV player, plus SRT subtitles.

## Live AI translation now included in source
- Android SpeechRecognizer continuously listens through the device microphone.
- ML Kit Language Identification detects the language of recognized text.
- ML Kit Translation downloads the required language model and translates recognized text to Arabic on-device after the model is downloaded.
- Live translated text is sent to the floating subtitle overlay.
- The live recognition service uses a foreground notification while listening.
- The spoken language hint can be changed in the app (BCP-47 examples: en-US, fr-FR, de-DE).

## Existing subtitle features
- Select an SRT subtitle file through Android's document picker.
- Parse SRT time ranges and display the current cue in a system overlay.
- Request the Android Display over other apps permission when needed.
- Manual subtitle offset from -10 to +10 minutes; positive delays subtitles and negative advances them.
- Save offsets by the channel/movie profile name; reset to zero and stop the overlay.

## Important hardware and platform limits
- **This build listens through the Android device microphone.** It does not capture the DVB player's internal audio stream. If the DreamMax box has no built-in microphone, a compatible USB microphone or another supported audio input is required. The TV speaker must be audible to that microphone.
- Android 7.1.1 has no standard Android playback-capture API for recording another app's audio. True direct DVB audio capture requires a device/ROM-specific audio loopback or DVB player integration; neither has been verified.
- Speech recognition depends on an installed Android recognition provider and may use network services. It is not guaranteed to work offline.
- ML Kit translation models are downloaded when first needed. Internet is required for the initial model download; translation can then run on-device for supported languages.
- The speech locale is a recognition hint. Language identification is applied to the recognized text, but recognition quality still depends on the selected speech locale and installed recognizer.
- Captions are speech-segment based, not synchronized to the DVB playback clock. Pause, seek, channel changes, and exact subtitle timing are not automatically detected.
- The overlay and microphone pipeline have not yet been verified on the DreamMax B9S2X.

## Build
GitHub Actions uses Gradle 7.5 and Java 11 to run gradle assembleDebug. This repository currently uses the installed Gradle distribution in Actions; it does not include the Gradle wrapper. Treat the APK as built only after a successful Actions run and artifact upload.
