#!/usr/bin/env python3
"""
Smarter Dev Rebrand Reveal Video Generator
Epic "Level Up" reveal video with countdown and brand transition.
"""

import os
from pathlib import Path
import moviepy as mp
from moviepy import (
    VideoFileClip, AudioFileClip, ImageClip, TextClip, 
    CompositeVideoClip, CompositeAudioClip, AudioClip,
    concatenate_videoclips, concatenate_audioclips
)
import numpy as np

# Configuration
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30
TOTAL_DURATION = 20

# Colors
CYAN = "#00E1FF"
WHITE = "#FFFFFF"
BLACK = "#000000"

# Timing (in seconds)
OPENING_END = 3
COUNTDOWN_START = 3
COUNTDOWN_END = 8
REVEAL_START = 8
REVEAL_END = 12
BRAND_START = 12
BRAND_END = 16
CLOSING_END = 20

def setup_paths():
    """Setup and validate file paths."""
    base_path = Path(__file__).parent.resolve()
    assets_path = base_path / "assets"
    audio_path = base_path / "audio"
    output_path = base_path / "output"
    
    # Create output directory if it doesn't exist
    output_path.mkdir(exist_ok=True)
    
    # Check if assets exist
    required_assets = [
        "video-bg.png",
        "bg-grid.svg", 
        "streak.png",
        "smarter-dev-brain.png"
    ]
    
    missing = []
    for asset in required_assets:
        if not (assets_path / asset).exists():
            missing.append(asset)
    
    if missing:
        print(f"Warning: Missing assets: {missing}")
        print("Make sure symlinks are created properly")
    
    return assets_path, audio_path, output_path

def create_background(duration):
    """Create the background with video-bg.png and tiled grid overlay."""
    assets_path, _, _ = setup_paths()
    
    # Load background image
    bg_img = ImageClip(str(assets_path / "video-bg.png"))
    bg_img = bg_img.resized((VIDEO_WIDTH, VIDEO_HEIGHT))
    bg_clip = bg_img.with_duration(duration)
    
    # TODO: Add tiled grid overlay when SVG support is improved
    # For now, we'll add a subtle grid pattern programmatically
    
    return bg_clip

def create_animated_streaks(start_time, end_time):
    """Create animated streak elements moving at -15° angle."""
    assets_path, _, _ = setup_paths()
    
    streaks = []
    duration = end_time - start_time
    
    # Create multiple streaks at different positions and timing
    for i in range(8):
        try:
            streak_img = ImageClip(str(assets_path / "streak.png"))
            streak_img = streak_img.resized(height=100)  # Scale down streaks
            
            # Position streaks across screen width
            x_start = -100 + (i * 280)  # Spread across width
            y_start = -200
            x_end = x_start + 400  # Move diagonally down-right
            y_end = VIDEO_HEIGHT + 200
            
            # Animate position
            streak_clip = (streak_img
                          .with_start(start_time + i * 0.2)  # Stagger start times
                          .with_duration(duration + 2)
                          .with_position(lambda t: (x_start + (x_end - x_start) * t / (duration + 2),
                                                 y_start + (y_end - y_start) * t / (duration + 2)))
                          .rotate(-15)  # -15 degree angle
                          .with_opacity(0.7))
            
            streaks.append(streak_clip)
        except Exception as e:
            print(f"Could not load streak.png: {e}")
            break
    
    return streaks

def create_countdown_text():
    """Create countdown numbers with glitch effects."""
    assets_path, _, _ = setup_paths()
    bruno_ace_font = str(assets_path / "fonts" / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf")
    
    countdown_clips = []
    
    for i, number in enumerate([5, 4, 3, 2, 1]):
        start_time = COUNTDOWN_START + i * 1.0  # 1 second per number
        
        # Main countdown number
        text_clip = (TextClip(str(number), 
                             fontsize=200, 
                             color=CYAN,
                             font=bruno_ace_font)
                    .with_position('center')
                    .with_start(start_time)
                    .with_duration(1.0))
        
        # Add glitch effect (simple version - offset copies)
        glitch1 = (text_clip.copy()
                  .with_position(('center', 'center'))
                  .with_opacity(0.3)
                  .resized(1.02))
        
        glitch2 = (text_clip.copy()
                  .with_position(('center', 'center'))  
                  .with_opacity(0.2)
                  .resized(0.98))
        
        countdown_clips.extend([text_clip, glitch1, glitch2])
    
    return countdown_clips

def create_brand_reveal():
    """Create the brain logo and Smarter Dev text reveal."""
    assets_path, _, _ = setup_paths()
    
    # Font paths
    bungee_hairline_font = str(assets_path / "fonts" / "Bungee Hairline" / "BungeeHairline-Regular.ttf")
    bruno_ace_font = str(assets_path / "fonts" / "Bruno_Ace_SC" / "BrunoAceSC-Regular.ttf")
    
    clips = []
    
    try:
        # Brain logo with glow effect
        brain_img = ImageClip(str(assets_path / "smarter-dev-brain.png"))
        brain_img = brain_img.resized(height=300)
        
        brain_clip = (brain_img
                     .set_start(BRAND_START)
                     .set_duration(CLOSING_END - BRAND_START)
                     .set_position(('center', VIDEO_HEIGHT // 2 - 100))
                     .fadeIn(0.5))
        
        clips.append(brain_clip)
        
    except Exception as e:
        print(f"Could not load brain logo: {e}")
    
    # "Smarter" text (white, Bungee Hairline)
    smarter_text = (TextClip("smarter", 
                            fontsize=80, 
                            color=WHITE,
                            font=bungee_hairline_font)
                   .set_position(('center', VIDEO_HEIGHT // 2 + 150))
                   .set_start(BRAND_START + 0.5)
                   .set_duration(CLOSING_END - BRAND_START - 0.5))
    
    # "Dev" text (cyan, Bruno Ace SC)  
    dev_text = (TextClip("dev", 
                        fontsize=80, 
                        color=CYAN,
                        font=bruno_ace_font)
               .set_position(('center', VIDEO_HEIGHT // 2 + 230))
               .set_start(BRAND_START + 1.0)
               .set_duration(CLOSING_END - BRAND_START - 1.0))
    
    clips.extend([smarter_text, dev_text])
    
    # Subtitle (using Bungee Hairline for consistency)
    subtitle = (TextClip("Level up your code", 
                        fontsize=40, 
                        color=WHITE,
                        font=bungee_hairline_font)
               .set_position(('center', VIDEO_HEIGHT // 2 + 320))
               .set_start(BRAND_END)
               .set_duration(CLOSING_END - BRAND_END)
               .fadeIn(0.5))
    
    clips.append(subtitle)
    
    return clips

def create_beginner_codes_glitch():
    """Create the brief Beginner.Codes appearance that glitches out."""
    assets_path, _, _ = setup_paths()
    bungee_hairline_font = str(assets_path / "fonts" / "Bungee Hairline" / "BungeeHairline-Regular.ttf")
    
    # "BEGINNER.CODES" text that appears briefly
    old_brand = (TextClip("BEGINNER.CODES", 
                         fontsize=60, 
                         color=WHITE,
                         font=bungee_hairline_font)
                .set_position('center')
                .set_start(REVEAL_START)
                .set_duration(1.0)
                .fadeIn(0.1)
                .fadeOut(0.3))
    
    # Glitch effect - multiple offset copies
    glitches = []
    for i in range(3):
        glitch = (old_brand.copy()
                 .set_opacity(0.3 - i * 0.1)
                 .resized(1.0 + i * 0.02)
                 .set_position(('center', 'center')))
        glitches.append(glitch)
    
    return [old_brand] + glitches

def create_audio_track():
    """Create audio track with all sound effects and music."""
    audio_path = setup_paths()[1]
    
    audio_clips = []
    
    try:
        # Bass rumble (0-3s opening)
        bass_rumble = (AudioFileClip(str(audio_path / "bass-rumble.wav"))
                      .set_start(0)
                      .set_duration(OPENING_END)
                      .volumex(0.7))  # Reduce volume slightly
        audio_clips.append(bass_rumble)
        print("✅ Added bass rumble")
    except Exception as e:
        print(f"⚠️  Could not load bass-rumble.wav: {e}")
    
    try:
        # Countdown beeps (3-8s, one per second)
        for i in range(1, 4):  # Only using beep-1, beep-2, beep-3 for 5,4,3,2,1
            beep_file = audio_path / f"beep-{i}.mp3"
            if beep_file.exists():
                beep_time = COUNTDOWN_START + (i-1) * 1.0  # Space 1 second apart
                beep = (AudioFileClip(str(beep_file))
                       .set_start(beep_time)
                       .volumex(0.8))
                audio_clips.append(beep)
        
        # Use the same beeps for counts 2 and 1 (reverse order for pitch progression)
        beep_2_file = audio_path / "beep-2.mp3"
        beep_3_file = audio_path / "beep-3.mp3"
        if beep_2_file.exists():
            beep_4 = (AudioFileClip(str(beep_2_file))
                     .set_start(COUNTDOWN_START + 3 * 1.0)
                     .volumex(0.9))
            audio_clips.append(beep_4)
        if beep_3_file.exists():
            beep_5 = (AudioFileClip(str(beep_3_file))
                     .set_start(COUNTDOWN_START + 4 * 1.0)
                     .volumex(1.0))  # Loudest for final beep
            audio_clips.append(beep_5)
        
        print("✅ Added countdown beeps")
    except Exception as e:
        print(f"⚠️  Could not load countdown beeps: {e}")
    
    try:
        # Epic hit (8s reveal moment)
        epic_hit = (AudioFileClip(str(audio_path / "epic-hit.mp3"))
                   .set_start(REVEAL_START)
                   .volumex(1.2))  # Boost volume for impact
        audio_clips.append(epic_hit)
        print("✅ Added epic hit")
    except Exception as e:
        print(f"⚠️  Could not load epic-hit.mp3: {e}")
    
    try:
        # Glitch effect (8-9s during BEGINNER.CODES transition)
        glitch = (AudioFileClip(str(audio_path / "glitch.mp3"))
                 .set_start(REVEAL_START + 0.5)
                 .volumex(0.6))  # Subtle glitch sound
        audio_clips.append(glitch)
        print("✅ Added glitch effect")
    except Exception as e:
        print(f"⚠️  Could not load glitch.mp3: {e}")
    
    try:
        # Background music (12-20s closing)
        background_music = (AudioFileClip(str(audio_path / "background-music.mp3"))
                          .set_start(BRAND_START)
                          .set_duration(CLOSING_END - BRAND_START)
                          .volumex(0.5)  # Keep music subtle
                          .fadeIn(0.5)
                          .fadeOut(1.0))
        audio_clips.append(background_music)
        print("✅ Added background music")
    except Exception as e:
        print(f"⚠️  Could not load background-music.mp3: {e}")
    
    # Combine all audio clips
    if audio_clips:
        final_audio = CompositeAudioClip(audio_clips).set_duration(TOTAL_DURATION)
        print(f"🎵 Combined {len(audio_clips)} audio clips")
        return final_audio
    else:
        print("⚠️  No audio files loaded, using silent track")
        return AudioClip(lambda t: 0, duration=TOTAL_DURATION)

def generate_video():
    """Generate the complete rebrand reveal video."""
    print("🎬 Starting Smarter Dev Rebrand Reveal Video Generation...")
    
    # Setup paths
    assets_path, audio_path, output_path = setup_paths()
    
    # Create all video elements
    print("📽️  Creating background...")
    background = create_background(TOTAL_DURATION)
    
    print("🎯 Creating animated streaks...")
    streaks = create_animated_streaks(0, TOTAL_DURATION)
    
    print("🔢 Creating countdown...")
    countdown = create_countdown_text()
    
    print("🧠 Creating brand reveal...")
    brand_reveal = create_brand_reveal()
    
    print("⚡ Creating glitch transition...")
    glitch_transition = create_beginner_codes_glitch()
    
    print("🎵 Setting up audio...")
    audio = create_audio_track()
    
    # Combine all elements
    print("🎭 Compositing final video...")
    all_clips = [background] + streaks + countdown + glitch_transition + brand_reveal
    
    final_video = CompositeVideoClip(all_clips, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
    final_video = final_video.set_audio(audio)
    final_video = final_video.set_duration(TOTAL_DURATION)
    
    # Export video
    output_file = output_path / "smarter_dev_rebrand_reveal.mp4"
    print(f"🚀 Exporting to {output_file}...")
    
    final_video.write_videofile(
        str(output_file),
        fps=FPS,
        codec='libx264',
        audio_codec='aac',
        temp_audiofile='temp-audio.m4a',
        remove_temp=True,
        verbose=False,
        logger=None
    )
    
    print(f"✅ Video generation complete! Output: {output_file}")
    return output_file

if __name__ == "__main__":
    generate_video()