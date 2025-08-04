#!/usr/bin/env python3
"""
Smarter Dev Rebrand Reveal Video Generator - Simplified Version
Epic "Level Up" reveal video with countdown and brand transition.
"""

import os
from pathlib import Path
from moviepy import *
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

def generate_video():
    """Generate a simple version of the rebrand reveal video."""
    print("🎬 Starting Smarter Dev Rebrand Reveal Video Generation...")
    
    base_path = Path(__file__).parent.resolve()
    assets_path = base_path / "assets"
    audio_path = base_path / "audio"
    output_path = base_path / "output"
    output_path.mkdir(exist_ok=True)
    
    # Create background
    print("📽️  Creating background...")
    try:
        bg_img = ImageClip(str(assets_path / "video-bg.png"))
        background = bg_img.resized((VIDEO_WIDTH, VIDEO_HEIGHT)).with_duration(TOTAL_DURATION)
    except Exception as e:
        print(f"⚠️  Could not load background: {e}")
        # Create solid background as fallback
        background = ColorClip((VIDEO_WIDTH, VIDEO_HEIGHT), color=(20, 20, 40)).with_duration(TOTAL_DURATION)
    
    clips = [background]
    
    # Create countdown text
    print("🔢 Creating countdown...")
    fonts_path = assets_path / "fonts" / "Bruno_Ace_SC" 
    bruno_font = str(fonts_path / "BrunoAceSC-Regular.ttf") if fonts_path.exists() else None
    
    for i, number in enumerate([5, 4, 3, 2, 1]):
        start_time = COUNTDOWN_START + i * 1.0
        
        try:
            if bruno_font:
                text_clip = (TextClip(str(number), 
                                    fontsize=200, 
                                    color=CYAN,
                                    font=bruno_font)
                            .with_position('center')
                            .with_start(start_time)
                            .with_duration(1.0))
            else:
                text_clip = (TextClip(str(number), 
                                    fontsize=200, 
                                    color=CYAN)
                            .with_position('center')
                            .with_start(start_time)
                            .with_duration(1.0))
            clips.append(text_clip)
        except Exception as e:
            print(f"⚠️  Could not create countdown text: {e}")
    
    # Create "BEGINNER.CODES" glitch
    print("⚡ Creating glitch transition...")
    bungee_font_path = assets_path / "fonts" / "Bungee Hairline"
    bungee_font = str(bungee_font_path / "BungeeHairline-Regular.ttf") if bungee_font_path.exists() else None
    
    try:
        if bungee_font:
            old_brand = (TextClip("BEGINNER.CODES", 
                                 fontsize=60, 
                                 color=WHITE,
                                 font=bungee_font)
                        .with_position('center')
                        .with_start(REVEAL_START)
                        .with_duration(1.0))
        else:
            old_brand = (TextClip("BEGINNER.CODES", 
                                 fontsize=60, 
                                 color=WHITE)
                        .with_position('center')
                        .with_start(REVEAL_START)
                        .with_duration(1.0))
        clips.append(old_brand)
    except Exception as e:
        print(f"⚠️  Could not create old brand text: {e}")
    
    # Create brand reveal
    print("🧠 Creating brand reveal...")
    try:
        # Brain logo
        brain_img = ImageClip(str(assets_path / "smarter-dev-brain.png"))
        brain_clip = (brain_img
                     .resized(height=300)
                     .with_start(BRAND_START)
                     .with_duration(CLOSING_END - BRAND_START)
                     .with_position(('center', VIDEO_HEIGHT // 2 - 100)))
        clips.append(brain_clip)
        
        # "Smarter" text
        if bungee_font:
            smarter_text = (TextClip("smarter", 
                                   fontsize=80, 
                                   color=WHITE,
                                   font=bungee_font)
                           .with_position(('center', VIDEO_HEIGHT // 2 + 150))
                           .with_start(BRAND_START + 0.5)
                           .with_duration(CLOSING_END - BRAND_START - 0.5))
        else:
            smarter_text = (TextClip("smarter", 
                                   fontsize=80, 
                                   color=WHITE)
                           .with_position(('center', VIDEO_HEIGHT // 2 + 150))
                           .with_start(BRAND_START + 0.5)
                           .with_duration(CLOSING_END - BRAND_START - 0.5))
        clips.append(smarter_text)
        
        # "Dev" text
        if bruno_font:
            dev_text = (TextClip("dev", 
                               fontsize=80, 
                               color=CYAN,
                               font=bruno_font)
                       .with_position(('center', VIDEO_HEIGHT // 2 + 230))
                       .with_start(BRAND_START + 1.0)
                       .with_duration(CLOSING_END - BRAND_START - 1.0))
        else:
            dev_text = (TextClip("dev", 
                               fontsize=80, 
                               color=CYAN)
                       .with_position(('center', VIDEO_HEIGHT // 2 + 230))
                       .with_start(BRAND_START + 1.0)
                       .with_duration(CLOSING_END - BRAND_START - 1.0))
        clips.append(dev_text)
        
        # Subtitle
        if bungee_font:
            subtitle = (TextClip("Level up your code", 
                               fontsize=40, 
                               color=WHITE,
                               font=bungee_font)
                       .with_position(('center', VIDEO_HEIGHT // 2 + 320))
                       .with_start(BRAND_END)
                       .with_duration(CLOSING_END - BRAND_END))
        else:
            subtitle = (TextClip("Level up your code", 
                               fontsize=40, 
                               color=WHITE)
                       .with_position(('center', VIDEO_HEIGHT // 2 + 320))
                       .with_start(BRAND_END)
                       .with_duration(CLOSING_END - BRAND_END))
        clips.append(subtitle)
        
    except Exception as e:
        print(f"⚠️  Could not create brand elements: {e}")
    
    # Create audio track
    print("🎵 Setting up audio...")
    audio_clips = []
    
    try:
        # Bass rumble
        if (audio_path / "bass-rumble.wav").exists():
            bass_rumble = (AudioFileClip(str(audio_path / "bass-rumble.wav"))
                          .with_start(0)
                          .with_duration(OPENING_END)
                          .with_volume_scaled(0.7))
            audio_clips.append(bass_rumble)
            print("✅ Added bass rumble")
    except Exception as e:
        print(f"⚠️  Could not load bass rumble: {e}")
    
    try:
        # Countdown beeps
        for i in range(1, 4):
            beep_file = audio_path / f"beep-{i}.mp3"
            if beep_file.exists():
                beep_time = COUNTDOWN_START + (i-1) * 1.0
                beep = (AudioFileClip(str(beep_file))
                       .with_start(beep_time)
                       .with_volume_scaled(0.8))
                audio_clips.append(beep)
        print("✅ Added countdown beeps")
    except Exception as e:
        print(f"⚠️  Could not load countdown beeps: {e}")
    
    try:
        # Epic hit
        if (audio_path / "epic-hit.mp3").exists():
            epic_hit = (AudioFileClip(str(audio_path / "epic-hit.mp3"))
                       .with_start(REVEAL_START)
                       .with_volume_scaled(1.2))
            audio_clips.append(epic_hit)
            print("✅ Added epic hit")
    except Exception as e:
        print(f"⚠️  Could not load epic hit: {e}")
    
    try:
        # Background music
        if (audio_path / "background-music.mp3").exists():
            background_music = (AudioFileClip(str(audio_path / "background-music.mp3"))
                              .with_start(BRAND_START)
                              .with_duration(CLOSING_END - BRAND_START)
                              .with_volume_scaled(0.5))
            audio_clips.append(background_music)
            print("✅ Added background music")
    except Exception as e:
        print(f"⚠️  Could not load background music: {e}")
    
    # Combine audio
    if audio_clips:
        final_audio = CompositeAudioClip(audio_clips).with_duration(TOTAL_DURATION)
        print(f"🎵 Combined {len(audio_clips)} audio clips")
    else:
        print("⚠️  No audio files loaded, using silent track")
        final_audio = AudioClip(lambda t: np.zeros(2), duration=TOTAL_DURATION)
    
    # Create final video
    print("🎭 Compositing final video...")
    final_video = CompositeVideoClip(clips, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
    final_video = final_video.with_audio(final_audio).with_duration(TOTAL_DURATION)
    
    # Export video
    output_file = output_path / "smarter_dev_rebrand_reveal.mp4"
    print(f"🚀 Exporting to {output_file}...")
    
    try:
        final_video.write_videofile(
            str(output_file),
            fps=FPS,
            codec='libx264',
            audio_codec='aac',
            temp_audiofile='temp-audio.m4a',
            remove_temp=True
        )
        print(f"✅ Video generation complete! Output: {output_file}")
        return output_file
    except Exception as e:
        print(f"❌ Error during video export: {e}")
        return None

if __name__ == "__main__":
    generate_video()