# Smarter Dev Rebrand Reveal Video

Epic "Level Up" reveal video transitioning from Beginner.Codes to Smarter Dev.

## Current Status
✅ Video generation script created  
✅ Directory structure set up  
✅ Visual assets linked  
⏳ Audio files needed  
⏳ Custom fonts needed for final version  

## Quick Start

1. **Install dependencies:**
   ```bash
   uv sync --group movie
   ```

2. **Run the generator:**
   ```bash
   uv run python videos/rebrand-reveal/generate_video.py
   ```

3. **Output:** `output/smarter_dev_rebrand_reveal.mp4`

## What's Working Now
- Basic video structure and timing
- Background and visual elements
- Countdown sequence
- Brand reveal animation
- Glitch transition effects
- Silent audio track (placeholder)

## What's Needed

### Audio Files (See audio-sourcing-guide.md)
- `audio/bass-rumble.wav` - Opening tension
- `audio/beep-1.wav` through `audio/beep-5.wav` - Countdown
- `audio/epic-hit.wav` - Reveal moment
- `audio/background-music.wav` - Closing music
- `audio/whoosh-*.wav` - Streak animations
- `audio/glitch.wav` - Transition effects

### Fonts (Optional for final version)
- **Bungee Hairline** - For "smarter" text
- **Bruno Ace SC** - For "dev" text

## Directory Structure
```
videos/rebrand-reveal/
├── README.md
├── sequence-breakdown.md      # Detailed video concept
├── audio-sourcing-guide.md    # Where to find audio
├── generate_video.py          # Main script
├── requirements.txt           # Python dependencies
├── assets/                    # Symlinks to main resources
├── audio/                     # Audio files (empty, needs filling)
└── output/                    # Generated videos
```

## Next Steps
1. Source and download audio files
2. Test video generation
3. Fine-tune timing and effects
4. Install custom fonts for final polish
5. Generate final version

## Customization
Edit `generate_video.py` to adjust:
- Video dimensions (currently 1920x1080)
- Timing constants
- Colors and fonts
- Animation parameters
- Audio synchronization