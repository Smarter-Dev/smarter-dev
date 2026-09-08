# Smarter character studio

An editable 3D reconstruction of the supplied cyan orb / graphite shell reference. The unseen back is an interpretation, not a scan. All geometry and facial features are true meshes; no image billboard or remote model service is used.

## Run

```sh
cd static/mascot-studio
npm ci
npm run assets
npm test
npm run dev
```

`npm run build` produces a self-contained `dist/` web demo. Serve that directory with any static web server. The demo starts animated (except when the device requests reduced motion), supports orbit/zoom, eight selectable moods, playback speed, cycling, square/portrait/wide PNG export and a four-second video recording when the browser supports MediaRecorder. PNG supports alpha; video uses a dark background. Fonts fall back to system sans when offline.

## Production asset

`public/smarter-orb.glb` contains the mesh, PBR materials, individually named shell and face parts, and eight looping four-second animation clips: **default, happy, playful, excited, working, eureka, sad, mad**. Import into Blender or any glTF 2.0 animation tool. Select one clip at a time. Meters, Y up, face points +Z. No external textures are needed. Keep the tiny scale keys: they hide expression variants portably.

The orb uses mood-tinted glass around a smaller opaque, neutral-gray mottled core. Each shell carries a mood-colored underlight; the bottom shell is the strongest source. There is no visible emitter ring. The demo adds bloom with transparent halo alpha and displays the original mockup beside the interactive render. Use a viewer that supports glTF transmission, volume, unlit materials, morph targets, and punctual lights. Four thick rounded spherical triangles form a nearly complete closed shell close to the orb. Opening combines a short outward movement with a gentle backward sweep, without shrinking or stacking the plates. The Shell slider and Open/Close button demonstrate this movement.

The GLB also includes `shell_open` and `shell_close`, two 1.2-second non-looping clips. They animate only the armor; play them as an override layer, replacing armor tracks from the mood animation. The demo combines mood motion and shell openness through `setShellState(root, mood, seconds, openness)` (0 closed, 1 open). Geometry checks verify 98% sphere coverage when closed and clear sightlines to the face when open.

Every mood has a distinct glass, trim, and underlight color; the core material remains gray. The GLB selects variants with transform tracks, moving inactive lights beyond their finite range rather than relying on scale to switch lights off. The demo uses four directly recolored three-source light strips for efficiency. Every mode uses the same recessed, inward-facing light placement to wash the core; dimmer seams keep the orb as the focal point. A matte 0.80 m core sits inside a smooth 0.98 m translucent casing, with 0.18 m between their surfaces. This is a ball inside a marble, not a uniformly frosted ball: full transmission, modest refraction, low surface roughness, and subtle interior mottling keep the layers distinct. `orbRadii` is shared by the geometry and preview scattering. The graphite armor has a matte micrograin finish.

The demo keeps the front of the casing clear, while adding optical-path scattering and softer transmitted detail in the deeper edge volume. Broad softbox reflections describe the smooth surface; tiny punctual-light reflections are suppressed. These effects, the atmospheric halo, and the floor-light pool are presentation effects, not portable GLB features. The GLB retains smooth PBR transmission and absorption; other renderers need their own scattering/bloom setup. Near-black unlit pupils and brows are exported in the model. The demo protects those details from bloom with a depth-tested mask, retaining luminous eyes and glints without letting dark eyes show through closed armor.

Facial features and blink morphs are projected onto the spherical surface. Happy uses smiling eye curves; Playful uses a quick right-eye blink with the same timing as the normal blink (about 0.23 seconds) and a shell wobble. Working taps the plates, Sad retracts inward, and Mad ruffles the armor. The Blender setup suppresses secondary reflections of facial graphics.

Eureka has no sun/idea prop: each plate makes a rapid forward turn around its own radial centerline, with its center fixed relative to the orb. Every two-second beat holds for 0.6 seconds, spins for 0.4 seconds, then holds for 1 second. Excited rotates the whole armor around the orb instead, retaining its 0.8-second bursts. Neither reverses. Working uses the same in-place motion at a calmer pace: one 3.2-second turn with 0.4-second holds at either end of the four-second loop. Its pupils and glints scan left–right–left over four seconds; combined gaze/blink morphs remain projected onto the orb.

All eight moods share one shell arrangement and lighting setup. The assembly sits farther forward; the chin plate stays forward-facing and moves across the lower orb, while the crown and side tuck back enough to frame the face. The same rear-cap clearance applies in every mode. Wide, soft inward-facing lights wash the orb without shining bright point hotspots onto their own shell backing. During Eureka and Working, the shells turn over stationary light mounts. Independent spins need an almost fully open shell (98% or more). The live control finishes the current forward turn before closing, and rejoins rotation during a hold after reopening. Tests check chin coverage, clear facial features, separation, fixed mounts, shared lights, forward direction, and loop continuity across all three rotating moods and ten openness settings.

`rig.js` is the editable source, re-exported by `model.js`: `createMascot()` builds the character, `pose(root, mood, seconds)` evaluates the rig, and `createClips(root)` bakes transform and morph tracks at 30 fps. Run `npm run assets` after edits. `npm test` validates the actual GLB, plays every mood, checks surface-fit during blinks, verifies mood colors and shell spin, and ray-tests closed coverage and open face visibility.

For profile icons, use square PNG and transparent background. For emotes, choose a mood and export a still or loop. For trailers, import GLB into the production scene for custom lighting, cameras, transparent renders, frame-accurate sequencing, and higher resolutions. The demo's video recorder captures real-time playback, so final production footage should be rendered offline. Studio lighting is intentionally separate from the reusable character asset; recreate it in the destination application.

## Blender assets

`blender -b --python render-assets.py` renders eight transparent 768px PNGs and saves a lit, camera-ready `renders/smarter-studio.blend`, opened on Default. Each expression is a named NLA track; unmute the desired track on both animated objects and facial shape keys. The script sets 30 fps before importing, preserving the four-second loops, and uses Blender's COMPAT lighting conversion to retain the underlights' intended output. Playful captures the brief wink; Eureka and Excited capture their different rotations mid-burst; Working captures its rightward glance. To refresh selected stills, use `blender -b --python render-assets.py -- --mood playful --mood eureka --mood excited --mood working`.

Regenerate at any resolution by changing the render settings. The web demo and Blender use different studio lighting and color management, so their highlights differ. In particular, the web's suppressed punctual reflections and edge diffusion are not reproduced exactly by Cycles. The model is a stylized reconstruction of the reference, not a pixel-identical photorealistic match. The `renders/preview-*.webm` files are historical demo exports from earlier material or motion passes, before the shared forward/chin shell arrangement; use the live demo or GLB for the current version. Background-browser throttling can reduce their frame rate and extend the encoded duration; they are not final animation footage. See `QA.md` for the verified asset and playback checks.
