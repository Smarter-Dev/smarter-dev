# Qubit asset recovery — September 8, 2026

Recovered from the September 5 session for deleted worktree `sc-paired-niobium-c26a`.

## Recovery method

Reconstructed all 17 recorded source files by replaying 159 successful file changes from session `01a07201-95bd-7173-9be7-5b4407eb2441`. Every unified-diff context matched exactly. Historical shell commands were inspected, not executed wholesale.

The production model was regenerated from the recovered source with the original Three.js version, 0.180.0. Its SHA-256 matches the final checksum recorded in the original session byte for byte:

```
aac1df043b364d298318cd35947286dbedf7f436697ea8fab39434fc5d4c1eb3
```

Model: `public/smarter-orb.glb` (7,614,520 bytes).

`npm test` passes, including glTF validation (zero errors or warnings), all eight moods, the two shell transitions, motion continuity, light placement, and face/shell geometry checks. `npm run build` passes with its existing bundle-size advisory.

## Surviving original files

- `public/mockup-reference.png`: copied from the original session attachment.
- `recovered-exports/`: eight PNG mood exports from Downloads and two WebM exports (Working and Excited) from macOS drag caches. Their original filenames are preserved; these may represent different historical revisions.
- `trailer/color-flight-storyboard.html`: copied from the surviving temporary storyboard.

## Regenerated files

The GLB, web build, Blender projects, render PNGs, and teaser outputs are generated from recovered source. Only the GLB has a recorded original checksum proving binary identity. The original dependency lockfile was not present in recorded patches; `package-lock.json` was regenerated using the recovered package manifest.

All eight transparent 768px mood renders completed. `renders/smarter-studio.blend` reopened successfully with 395 objects, ten animation actions, a camera, and a 120-frame timeline at 30 fps. Both teaser renders completed, and the recovered verification script decoded every frame: 288 frames for the 12-second original cut and 336 for the 14-second color-flight cut, both 1280×720 at 24 fps with stereo 48 kHz audio. The color-flight silence check passed. Verification reports and extracted review frames accompany the videos.

Historical preview WebMs and the three root-level GIFs (`smarter-excited.gif`, `smarter-playful.gif`, `smarter-working.gif`) were not recovered at their original paths. The current model and editable source are recovered. Old QA documents describe the original session's checks; this file records recovery-time verification.

## Independent backup

Backup directory outside app-managed worktrees:

`/Users/zech/RecoveredAssets/qubit-20260908-tUA6vo`

It contains the original creation session history (`original-session.jsonl`, from September 5), `source-and-model.tar.gz`, and `complete-assets.tar.gz`. The complete archive includes generated animation frames so the Blender video-edit projects retain their image sequences; it excludes reinstallable `node_modules`, reproducible `dist`, and redundant `.blend1` backups. The session history is private and was intentionally kept outside this repository.

The recovery initially left this directory uncommitted. The recovered project and video deliverables are now being committed at the owner's request. Intermediate animation frames remain ignored in Git; regenerate them with the trailer scripts, or restore them from the independent complete archive before using the Blender video-edit sequences.
