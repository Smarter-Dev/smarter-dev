# Verification — 2026-09-05

## Model and animation

- `npm run assets`, `npm test`, and `npm run build` pass.
- The generated GLB has zero glTF validation errors or warnings, eight distinct four-second mood loops, and two 1.2-second shell transitions.
- Exported facial geometry, including intermediate blink morphs, stays within 7 mm of the 0.98 m orb surface.
- Every mood uses a gray, non-emissive core; a smooth translucent casing; and matching distributed shell underlights. Tests assert the shared 0.80/0.98 m core/casing radii, a minimum 0.175 m surface gap, roughness between 0.1 and 0.22, full transmission, IOR ≥1.3, and near-black unlit pupils/brows. Inactive light variants remain out of range. The Chin light strip is the strongest source.
- Ray tests measure 98.4% closed shell coverage and 98.8% visibility across the sampled open face window. Separate geometry samples verify unobstructed eyes, mouth, and brows for all eight expressions, with the lower plate now covering the chin. Pairwise separating planes cover all ten clips; plate scale stays fixed. Eureka and Working's local spins and Excited's orbital spins preserve separation and loop continuity at ten openness settings. The live control settles independent rotation before closing and restarts during a hold after reopening; a simulated controller check also verifies these transitions while playback is paused.
- The exported Playful wink lasts about 229 ms above 5% closure, matches the normal blink's duration, closes fully, and leaves the other eye open. Eye, pupil, and glint morphs stay synchronized.
- Eureka's sun geometry and animation tracks are absent. Exported Eureka and Working plate pivots remain fixed through local spins; the armor rig does not orbit. Excited rotates the armor rig without twisting individual plates. Signed quaternion increments verify forward-only rotation. Eureka has two 0.4-second turns per four-second clip, Excited two 0.8-second turns, and Working one 3.2-second turn; all have verified stationary pause intervals.
- All modes share the same light positions, beam parameters, and intensity; only their colors change. The forward-set shell arrangement and chin shield are shared too. Eureka and Working's distributed lights remain fixed relative to the orb through every sampled spin. Ray tests verify the mounts stay beneath their plates; beam-direction tests verify their broad, soft spotlights point inward, away from the backing. The live render was checked after removing the lower shell's visible point hotspot and repositioning the shared shell layout.
- Working pupils and glints scan left–right–left over four seconds, in sync. Combined gaze/blink morphs preserve the existing surface-fit bounds.

## Live demo and exports

- All eight mood buttons select their expression. With the browser visible, successive canvas captures differ for all eight moods, confirming live animation.
- Closed and side-profile views were checked after adding the depth-tested pupil/brow bloom mask. Eyes do not show through armor. The marble refinement was tuned against the supplied mockup: the inner ball remains visible, the front of the casing is clear, and additional diffusion is concentrated toward the deeper edges instead of covering the face with haze.
- Actual downloaded PNGs: square 1024×1024, portrait 1080×1920, wide 1920×1080. All are RGBA with alpha values ranging from 0 to 255; the square corner is fully transparent.
- Pre-marble videos were downloaded and decoded at 0.2, 1.73, and 2.2 seconds: Excited 1024×1024 / 4.982 s; Mad 1024×1024 / 4.317 s; Eureka 1920×1080 / 4.606 s; Playful 1024×1024 / 4.560 s. Each contains different decoded frames, not a repeated still. These background-browser captures have sparse frames and overshoot four seconds; they are export smoke tests from the earlier frosted-material pass, not current appearance references or final animation footage.
- Recording has its own elapsed-time clock instead of depending on the browser's animation-frame loop. Encoding remains real-time and browser-dependent; use the GLB/Blender scene for exact-frame production footage.

## Blender handoff

- Eight transparent 768×768 mood stills are generated with the current smooth casing, smaller inner ball, and dark pupils. Default was visually rechecked; all eight were inspected during the earlier pipeline pass.
- The script asserts that imported mood tracks remain four seconds at 30 fps and that the Playful still contains the wink morph.
- The scene retains all mood tracks, shell opening/closing clips, lights, camera, and editable geometry. It is saved in the Default pose.
- COMPAT light conversion and calibrated color management prevent imported underlights from disappearing beneath the studio lighting.

The web preview's edge scattering/transmission blur, softbox reflections, eye-protecting bloom mask, atmospheric halo, and floor-light pool are presentation effects, not part of the GLB. Portable smooth transmission/absorption and unlit dark pupils are in the GLB. The visual is a stylized reconstruction; renderer lighting and reflections can differ. Vite reports a non-blocking large-bundle warning for Three.js.
