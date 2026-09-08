# Smarter Dev — mystery teasers

## Color flight (current cut)

`output/color-flight/smarter-dev-color-flight-teaser.mp4` follows the approved
six-beat storyboard. It is a 14-second, 1280 × 720, 24 fps teaser with stereo AAC.
The first cut below is preserved separately. Nothing is posted to Discord.

| Time | Picture | Sound |
| --- | --- | --- |
| 0–2 s | A cyan shell flashes past | Approaching air sweeps across stereo |
| 2–4.5 s | Violet, mint, gold, and cyan join at different depths | Related propulsion voices overlap |
| 4.5–7 s | Curved, weaving paths and a playful near miss | Passing motion supplies the rhythm |
| 7–9 s | Five colors accelerate toward and past the camera | A shared rush builds |
| 9–11 s | One cyan shell settles and tilts toward the viewer | Deliberate silence, then a quiet breath |
| 11–14 s | “Something bright is coming.” / “SMARTER DEV DISCORD” | The same voice leaves a soft residual tail |

All shells stay closed. Facial meshes are excluded entirely, and the film
contains no mascot name, date, capability claims, or reveal. The original
mascot GLB and its lighting/materials are not modified. Flight-specific materials
and geometry instances live only in this separate Blender project.

The original synthesized sound is driven by the exact flight paths: speed,
distance, screen direction, and color. It contains no sampled audio, separate
beat bed, notification effects, voice-over, or new end-card jingle. The sound
stops deliberately at 9.38 seconds, with a quiet beat through 9.90 seconds.
`sound-cues.json` records source levels and event timing.

```sh
cd static/mascot-studio
blender -b --python trailer/create-flight-teaser.py -- --preview
blender -b --python trailer/create-flight-teaser.py
swift -suppress-warnings trailer/verify-teaser.swift trailer/output/color-flight/smarter-dev-color-flight-teaser.mp4 14
```

Use `--audio-only` to regenerate just the WAV and cue report, and `--encode-only`
to regenerate the sound and encode existing frames. The private `.blend` includes
the rendered edit and audio; rerun the Python script to change flight choreography.
Six still previews cover each storyboard beat. Verification decodes all 336 video
frames, checks stereo audio length/headroom and the quiet beat, then extracts six
frames from the encoded MP4 for visual review.

## Original signal cut (preserved)

Upload `output/smarter-dev-teaser.mp4`. The public filename, on-screen copy,
and film conceal Qubit's name and face. This is a teaser, not the reveal.
No reveal date or product capabilities are claimed. Nothing is posted to Discord.

- 12 seconds, 1280 × 720, 24 fps, H.264 MP4 with stereo AAC.
- Macro seam / faint signal → closed shell stirring → “COMING SOON”.
- Original synthesized heartbeat-like pulses, air swells, and a closing chime.
- Readable without sound. No voice-over, stock footage, or borrowed music.

The source uses the existing model but removes every facial mesh and keeps
the armor almost closed. Trailer-specific lighting does not modify the studio
model or its mood materials. The private working Blender project contains a
rendered-image edit with its soundtrack; rerun the script to change the photography.

```sh
cd static/mascot-studio
blender -b --python trailer/create-teaser.py -- --preview
blender -b --python trailer/create-teaser.py
swift trailer/verify-teaser.swift trailer/output/smarter-dev-teaser.mp4
```

The verification script decodes the actual encoded video and audio, checks
duration/resolution/frame rate, rejects silent/clipped audio, and extracts four
frames for visual review. Its report is `output/verification.json`.

Rendering requires Blender 5.2 (NumPy included) and the installed macOS fonts
referenced in the script. Verification uses macOS AVFoundation.
