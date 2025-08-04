#!/usr/bin/env python3
"""
Smarter Dev Rebrand Reveal Video Generator - Text Positioning Fixed
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
    """Generate the rebrand reveal video with fixed text positioning."""
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
        print("✅ Background loaded")
    except Exception as e:
        print(f"⚠️  Could not load background: {e}")
        background = ColorClip((VIDEO_WIDTH, VIDEO_HEIGHT), color=(20, 20, 40)).with_duration(TOTAL_DURATION)
    
    clips = [background]
    
    # Create countdown text with explicit sizing
    print("🔢 Creating countdown...")
    fonts_path = assets_path / "fonts" / "Bruno_Ace_SC" 
    bruno_font = str(fonts_path / "BrunoAceSC-Regular.ttf") if fonts_path.exists() else None
    
    for i, number in enumerate([5, 4, 3, 2, 1]):
        start_time = COUNTDOWN_START + i * 1.0
        
        try:
            # Create text with explicit size and positioning
            text_clip = TextClip(
                text=str(number), 
                font_size=200, 
                color=CYAN,
                font=bruno_font,
                size=(400, 300),  # Explicit text area size
                method='caption'  # Better text rendering
            )
            
            # Position explicitly in center
            text_clip = (text_clip
                        .with_position(('center', 'center'))
                        .with_start(start_time)
                        .with_duration(1.0))
            
            clips.append(text_clip)
            print(f"✅ Added countdown: {number}")
        except Exception as e:
            print(f"⚠️  Could not create countdown {number}: {e}")
            # Fallback without custom font
            try:
                text_clip = TextClip(
                    text=str(number), 
                    font_size=200, 
                    color=CYAN,
                    size=(400, 300),
                    method='caption'
                )
                
                text_clip = (text_clip
                            .with_position(('center', 'center'))
                            .with_start(start_time)
                            .with_duration(1.0))
                
                clips.append(text_clip)
                print(f"✅ Added countdown: {number} (fallback font)")
            except Exception as e2:
                print(f"❌ Countdown {number} failed completely: {e2}")
    
    # Create "BEGINNER.CODES" glitch with explicit sizing
    print("⚡ Creating glitch transition...")
    bungee_font_path = assets_path / "fonts" / "Bungee Hairline"
    bungee_font = str(bungee_font_path / "BungeeHairline-Regular.ttf") if bungee_font_path.exists() else None
    
    try:
        old_brand = TextClip(
            text="BEGINNER.CODES", 
            font_size=60, 
            color=WHITE,
            font=bungee_font,
            size=(800, 100),  # Explicit size
            method='caption'
        )
        
        old_brand = (old_brand
                    .with_position(('center', 'center'))
                    .with_start(REVEAL_START)
                    .with_duration(1.0))
        
        clips.append(old_brand)
        print("✅ Added BEGINNER.CODES transition")
    except Exception as e:
        print(f"⚠️  Could not create old brand text: {e}")
        # Fallback without custom font
        try:
            old_brand = TextClip(
                text="BEGINNER.CODES", 
                font_size=60, 
                color=WHITE,
                size=(800, 100),
                method='caption'
            )
            
            old_brand = (old_brand
                        .with_position(('center', 'center'))
                        .with_start(REVEAL_START)
                        .with_duration(1.0))
            
            clips.append(old_brand)
            print("✅ Added BEGINNER.CODES transition (fallback font)")
        except Exception as e2:
            print(f"❌ BEGINNER.CODES transition failed: {e2}")
    
    # Create brand reveal with explicit positioning
    print("🧠 Creating brand reveal...")
    try:
        # Brain logo
        brain_img = ImageClip(str(assets_path / "smarter-dev-brain.png"))
        brain_clip = (brain_img
                     .resized(height=300)
                     .with_start(BRAND_START)
                     .with_duration(CLOSING_END - BRAND_START)
                     .with_position(('center', VIDEO_HEIGHT // 2 - 200)))  # Moved up more
        clips.append(brain_clip)
        print("✅ Added brain logo")
        
        # "Smarter" text - positioned higher
        try:
            smarter_text = TextClip(
                text="smarter", 
                font_size=80, 
                color=WHITE,
                font=bungee_font,
                size=(400, 120),
                method='caption'
            )
            
            smarter_text = (smarter_text
                           .with_position(('center', VIDEO_HEIGHT // 2 + 50))  # Higher position
                           .with_start(BRAND_START + 0.5)
                           .with_duration(CLOSING_END - BRAND_START - 0.5))
            
            clips.append(smarter_text)
            print("✅ Added 'smarter' text")
        except Exception as e:
            # Fallback without custom font
            smarter_text = TextClip(
                text="smarter", 
                font_size=80, 
                color=WHITE,
                size=(400, 120),
                method='caption'
            )
            
            smarter_text = (smarter_text
                           .with_position(('center', VIDEO_HEIGHT // 2 + 50))
                           .with_start(BRAND_START + 0.5)
                           .with_duration(CLOSING_END - BRAND_START - 0.5))
            
            clips.append(smarter_text)
            print("✅ Added 'smarter' text (fallback font)")
        
        # "Dev" text - positioned below smarter
        try:
            dev_text = TextClip(
                text="dev", 
                font_size=80, 
                color=CYAN,
                font=bruno_font,
                size=(200, 120),
                method='caption'
            )
            
            dev_text = (dev_text
                       .with_position(('center', VIDEO_HEIGHT // 2 + 150))  # Below smarter
                       .with_start(BRAND_START + 1.0)
                       .with_duration(CLOSING_END - BRAND_START - 1.0))
            
            clips.append(dev_text)
            print("✅ Added 'dev' text")
        except Exception as e:
            # Fallback without custom font
            dev_text = TextClip(
                text="dev", 
                font_size=80, 
                color=CYAN,
                size=(200, 120),
                method='caption'
            )
            
            dev_text = (dev_text
                       .with_position(('center', VIDEO_HEIGHT // 2 + 150))
                       .with_start(BRAND_START + 1.0)
                       .with_duration(CLOSING_END - BRAND_START - 1.0))
            
            clips.append(dev_text)
            print("✅ Added 'dev' text (fallback font)")
        
        # Subtitle - positioned at bottom
        try:
            subtitle = TextClip(
                text="Level up your code", 
                font_size=40, 
                color=WHITE,
                font=bungee_font,
                size=(600, 60),
                method='caption'
            )
            
            subtitle = (subtitle
                       .with_position(('center', VIDEO_HEIGHT // 2 + 280))  # Bottom position
                       .with_start(BRAND_END)
                       .with_duration(CLOSING_END - BRAND_END))
            
            clips.append(subtitle)
            print("✅ Added subtitle")
        except Exception as e:
            # Fallback without custom font
            subtitle = TextClip(
                text="Level up your code", 
                font_size=40, 
                color=WHITE,
                size=(600, 60),
                method='caption'
            )
            
            subtitle = (subtitle
                       .with_position(('center', VIDEO_HEIGHT // 2 + 280))
                       .with_start(BRAND_END)
                       .with_duration(CLOSING_END - BRAND_END))
            
            clips.append(subtitle)
            print("✅ Added subtitle (fallback font)")
        
    except Exception as e:
        print(f"⚠️  Could not create brand elements: {e}")
    
    # Create audio track (same as before)
    print("🎵 Setting up audio...")
    audio_clips = []
    
    # Bass rumble
    try:
        if (audio_path / "bass-rumble.wav").exists():
            bass_rumble = (AudioFileClip(str(audio_path / "bass-rumble.wav"))
                          .subclipped(0, OPENING_END)
                          .with_start(0)
                          .with_volume_scaled(0.7))
            audio_clips.append(bass_rumble)
            print("✅ Added bass rumble")
    except Exception as e:
        print(f"⚠️  Could not load bass rumble: {e}")
    
    # Countdown beeps
    try:
        beep_files = ["beep-1.mp3", "beep-2.mp3", "beep-3.mp3"]
        for i, beep_file in enumerate(beep_files):
            if (audio_path / beep_file).exists():
                beep_time = COUNTDOWN_START + i * 1.0
                beep = (AudioFileClip(str(audio_path / beep_file))
                       .subclipped(0, 1.0)
                       .with_start(beep_time)
                       .with_volume_scaled(0.8))
                audio_clips.append(beep)
        print("✅ Added countdown beeps")
    except Exception as e:
        print(f"⚠️  Could not load countdown beeps: {e}")
    
    # Epic hit
    try:
        if (audio_path / "epic-hit.mp3").exists():
            epic_hit = (AudioFileClip(str(audio_path / "epic-hit.mp3"))
                       .with_start(REVEAL_START)
                       .with_volume_scaled(1.2))
            audio_clips.append(epic_hit)
            print("✅ Added epic hit")
    except Exception as e:
        print(f"⚠️  Could not load epic hit: {e}")
    
    # Background music
    try:
        if (audio_path / "background-music.mp3").exists():
            bg_music_duration = CLOSING_END - BRAND_START
            background_music = (AudioFileClip(str(audio_path / "background-music.mp3"))
                              .subclipped(0, bg_music_duration)
                              .with_start(BRAND_START)
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
    print(f"📊 Total clips: {len(clips)}")
    
    final_video = CompositeVideoClip(clips, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
    final_video = final_video.with_audio(final_audio).with_duration(TOTAL_DURATION)
    
    # Export video
    output_file = output_path / "smarter_dev_rebrand_reveal_text_fixed.mp4"
    print(f"🚀 Exporting to {output_file}...")
    
    try:
        final_video.write_videofile(
            str(output_file),
            fps=FPS,
            codec='libx264',
            audio_codec='aac',
            temp_audiofile='temp-audio-text-fixed.m4a',
            remove_temp=True
        )
        print(f"✅ Video generation complete! Output: {output_file}")
        return output_file
    except Exception as e:
        print(f"❌ Error during video export: {e}")
        return None

if __name__ == "__main__":
    generate_video()