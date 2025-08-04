#!/usr/bin/env python3
"""
Smarter Dev Rebrand Reveal Video Generator - Working Version
Epic "Level Up" reveal video with smooth countdown transitions using basic scaling only.
"""

import os
from pathlib import Path
from moviepy import *
import numpy as np

# Configuration
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30
TOTAL_DURATION = 50  # Extended to 50 seconds for 30-second final screen

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
FINAL_SCREEN_START = 20  # When final brand screen is fully visible
CLOSING_END = 50  # Extended total duration

def create_number_with_effect(number, start_time, assets_path, bruno_font):
    """Create a countdown number with scaling effect."""
    try:
        text_clip = TextClip(
            text=str(number), 
            font_size=200, 
            color=CYAN,
            font=bruno_font,
            size=(400, 300),
            method='caption'
        )
        
        # Create scaling effect - starts small, grows to normal size, then shrinks
        def scale_func(t):
            if t < 0.2:  # First 0.2 seconds - grow from 0.5 to 1.0 
                return 0.5 + 0.5 * (t / 0.2)
            elif t < 0.8:  # Middle 0.6 seconds - stay at normal size
                return 1.0
            else:  # Last 0.2 seconds - shrink to 0.8
                return 1.0 - 0.2 * ((t - 0.8) / 0.2)
        
        text_clip = (text_clip
                    .with_position(('center', 'center'))
                    .with_start(start_time)
                    .with_duration(1.0)
                    .resized(scale_func))
        
        return text_clip
        
    except Exception as e:
        print(f"⚠️  Font error for {number}, using fallback: {e}")
        # Fallback without custom font
        text_clip = TextClip(
            text=str(number), 
            font_size=200, 
            color=CYAN,
            size=(400, 300),
            method='caption'
        )
        
        def scale_func(t):
            if t < 0.2:
                return 0.5 + 0.5 * (t / 0.2)
            elif t < 0.8:
                return 1.0
            else:
                return 1.0 - 0.2 * ((t - 0.8) / 0.2)
        
        text_clip = (text_clip
                    .with_position(('center', 'center'))
                    .with_start(start_time)
                    .with_duration(1.0)
                    .resized(scale_func))
        
        return text_clip

def generate_video():
    """Generate the final rebrand reveal video."""
    print("🎬 Starting Smarter Dev Rebrand Reveal Video Generation (Working Version)...")
    
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
    
    # Create countdown with smooth scaling transitions
    print("🔢 Creating countdown with smooth scaling transitions...")
    fonts_path = assets_path / "fonts" / "Bruno_Ace_SC" 
    bruno_font = str(fonts_path / "BrunoAceSC-Regular.ttf") if fonts_path.exists() else None
    
    for i, number in enumerate([5, 4, 3, 2, 1]):
        start_time = COUNTDOWN_START + i * 1.0
        
        number_clip = create_number_with_effect(number, start_time, assets_path, bruno_font)
        if number_clip:
            clips.append(number_clip)
            print(f"✅ Added countdown: {number} with scaling effect")
    
    # Create \"BEGINNER.CODES\" transition with glitch effect
    print("⚡ Creating BEGINNER.CODES glitch transition...")
    bungee_font_path = assets_path / "fonts" / "Bungee Hairline"
    bungee_font = str(bungee_font_path / "BungeeHairline-Regular.ttf") if bungee_font_path.exists() else None
    
    try:
        # First show static text briefly before flickering
        static_text = TextClip(
            text="BEGINNER.CODES", 
            font_size=60, 
            color=WHITE,
            font=bungee_font,
            size=(800, 100),
            method='caption'
        ).with_position(('center', 'center')).with_start(REVEAL_START).with_duration(1.5)
        
        clips.append(static_text)
        
        # Create simple flickering effect
        def create_flickering_effect():
            flicker_clips = []
            flicker_start = REVEAL_START + 1.5
            flicker_duration = 3.0  # 3 seconds of flickering, ending at REVEAL_START + 4.5
            
            # Create the base text clip
            base_text = TextClip(
                text="BEGINNER.CODES", 
                font_size=60, 
                color=WHITE,
                font=bungee_font,
                size=(800, 100),
                method='caption'
            ).with_position(('center', 'center'))
            
            # Create flickering pattern: alternating visible/invisible
            flicker_frame_duration = 0.08  # Fast flicker (80ms per frame)
            num_flicker_frames = int(flicker_duration / flicker_frame_duration)
            
            for i in range(num_flicker_frames):
                frame_start = flicker_start + i * flicker_frame_duration
                
                # Randomize flicker pattern - sometimes visible, sometimes not
                # Start with more visible frames, gradually become more invisible
                visibility_chance = max(0.1, 1.0 - (i / num_flicker_frames) * 0.7)  # 100% to 30% chance
                
                if np.random.random() < visibility_chance:
                    # Visible frame - sometimes at full opacity, sometimes dimmed
                    opacity = np.random.choice([1.0, 0.8, 0.6, 0.4], p=[0.4, 0.3, 0.2, 0.1])
                    flicker_frame = (base_text
                                   .with_opacity(opacity)
                                   .with_start(frame_start)
                                   .with_duration(flicker_frame_duration))
                    flicker_clips.append(flicker_frame)
                # If not visible, don't add any frame (creates the flicker gap)
            
            return flicker_clips
        
        # Add flickering effect
        flicker_clips = create_flickering_effect()
        clips.extend(flicker_clips)
        
        print(f"⚡ Added flickering effect with {len(flicker_clips)} flicker frames over 3 seconds")
        print("✅ Added BEGINNER.CODES with flickering then disappearing transition")
    except Exception as e:
        print(f"⚠️  Could not create BEGINNER.CODES: {e}")
        # Fallback without glitch effect
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
                        .with_duration(3.0))
            
            clips.append(old_brand)
            print("✅ Added BEGINNER.CODES (fallback without glitch)")
        except Exception as e2:
            print(f"⚠️  BEGINNER.CODES failed completely: {e2}")
    
    # Create brand reveal
    print("🧠 Creating brand reveal...")
    try:
        # Brain logo with smooth scale entrance
        brain_img = ImageClip(str(assets_path / "smarter-dev-brain.png"))
        
        brain_clip = (brain_img
                     .resized(height=150)  # Make brain logo about half the size (was 300, now 150)
                     .with_start(REVEAL_START + 7.5)  # Start 3 seconds after Smarter Dev text (4.5 + 3 = 7.5)
                     .with_duration(CLOSING_END - REVEAL_START - 7.5)
                     .with_position(('center', VIDEO_HEIGHT // 2 - 300)))  # Nudge further up
        clips.append(brain_clip)
        print("✅ Added brain logo")
        
        # \"Smarter Dev\" text on one line with dramatic reveal
        try:
            # Create "Smarter" in white using Bungee Hairline
            smarter_text = TextClip(
                text="SMARTER ", 
                font_size=60, 
                color=WHITE,
                font=bungee_font,
                size=(400, 100),
                method='caption'
            )
            
            # Create "Dev" in cyan using Bruno Ace SC  
            dev_text = TextClip(
                text="DEV", 
                font_size=60, 
                color=CYAN,
                font=bruno_font,
                size=(200, 100),
                method='caption'
            )
            
            # Position them closer together on a single line, centered as a unit
            brand_text = CompositeVideoClip([
                smarter_text.with_position((120, 0)),   # Position "SMARTER " 
                dev_text.with_position((420, 0))        # Nudge "DEV" 20px to the right (was 400, now 420)
            ], size=(800, 100))
            
            def brand_slide_position(t):
                if t < 0.5:
                    slide_offset = 40 * (1 - t / 0.5)  # Slide from below
                    return ('center', 'center')
                else:
                    return ('center', 'center')
            
            def brand_scale(t):
                if t < 0.3:
                    return 0.8 + 0.2 * (t / 0.3)  # Scale from 80% to 100%
                else:
                    return 1.0
            
            brand_text = (brand_text
                         .with_position(brand_slide_position)
                         .with_start(REVEAL_START + 4.5)  # Start immediately after flickering ends
                         .with_duration(CLOSING_END - REVEAL_START - 4.5))
            
            clips.append(brand_text)
            print("✅ Added 'Smarter Dev' text on one line with dramatic reveal")
        except Exception as e:
            # Simple fallback - match BEGINNER.CODES exactly
            brand_text = TextClip(
                text="SMARTER DEV", 
                font_size=60, 
                color=WHITE,
                size=(800, 100),
                method='caption'
            )
            
            brand_text = (brand_text
                         .with_position(('center', 'center'))
                         .with_start(REVEAL_START + 4.5)
                         .with_duration(CLOSING_END - REVEAL_START - 4.5))
            
            clips.append(brand_text)
            print("✅ Added 'Smarter Dev' text (fallback)")
        
        # Subtitle
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
                       .with_position(('center', VIDEO_HEIGHT // 2 + 80))   # Move CTA text up for equal spacing between logo, name, and CTA
                       .with_start(REVEAL_START + 9.5)  # Start 5 seconds after Smarter Dev text (4.5 + 5 = 9.5)
                       .with_duration(CLOSING_END - REVEAL_START - 9.5))
            
            clips.append(subtitle)
            print("✅ Added subtitle")
        except Exception as e:
            print(f"⚠️  Could not create subtitle: {e}")
        
    except Exception as e:
        print(f"⚠️  Could not create brand elements: {e}")
    
    # Create audio track - using first beep for all countdown numbers
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
    
    # Countdown beeps - using ONLY the first beep, making them slightly different
    try:
        if (audio_path / "beep-1.mp3").exists():
            for i in range(5):  # 5 numbers: 5, 4, 3, 2, 1
                beep_time = COUNTDOWN_START + i * 1.0
                # Trim to different lengths and adjust volume for variation
                duration = 0.6 + i * 0.05  # Slightly longer each time
                volume = 0.7 + i * 0.06  # Slightly louder each time
                
                beep = (AudioFileClip(str(audio_path / "beep-1.mp3"))
                       .subclipped(0, duration)
                       .with_start(beep_time)
                       .with_volume_scaled(volume))
                audio_clips.append(beep)
            print("✅ Added countdown beeps (all using beep-1 with variations)")
    except Exception as e:
        print(f"⚠️  Could not load countdown beep: {e}")
    
    # Glitch sound effect (if available)
    try:
        if (audio_path / "glitch.mp3").exists():
            glitch_sound = (AudioFileClip(str(audio_path / "glitch.mp3"))
                           .with_start(REVEAL_START + 1.5)  # Start when glitch visual begins
                           .with_volume_scaled(0.8))
            audio_clips.append(glitch_sound)
            print("✅ Added glitch sound effect")
    except Exception as e:
        print(f"⚠️  Could not load glitch sound: {e}")
    
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
    
    # Background music - start earlier to overlap with epic hit
    try:
        if (audio_path / "background-music.mp3").exists():
            bg_music_start = REVEAL_START + 0.5  # Start 0.5 seconds after epic hit begins
            bg_music_duration = CLOSING_END - bg_music_start
            background_music = (AudioFileClip(str(audio_path / "background-music.mp3"))
                              .subclipped(0, min(bg_music_duration, 45))  # Cap at 45 seconds max
                              .with_start(bg_music_start)
                              .with_volume_scaled(0.4))  # Slightly quieter to blend with epic hit
            audio_clips.append(background_music)
            print("✅ Added background music (starts during epic hit)")
    except Exception as e:
        print(f"⚠️  Could not load background music: {e}")
    
    # Combine audio
    if audio_clips:
        final_audio = CompositeAudioClip(audio_clips).with_duration(TOTAL_DURATION)
        print(f"🎵 Combined {len(audio_clips)} audio clips")
    else:
        print("⚠️  No audio files loaded, using silent track")
        final_audio = AudioClip(lambda t: 0, duration=TOTAL_DURATION)
    
    # Create final video
    print("🎭 Compositing final video...")
    print(f"📊 Total clips: {len(clips)}")
    
    final_video = CompositeVideoClip(clips, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
    final_video = final_video.with_audio(final_audio).with_duration(TOTAL_DURATION)
    
    # Export video
    output_file = output_path / "smarter_dev_rebrand_reveal_FLICKERING.mp4"
    print(f"🚀 Exporting to {output_file}...")
    print(f"📏 Video duration: {TOTAL_DURATION} seconds (30-second final screen)")
    print("⚡ Features: FLICKERING EFFECT - Text flickers for 3 seconds then disappears + Single line 'Smarter Dev'")
    
    try:
        final_video.write_videofile(
            str(output_file),
            fps=FPS,
            codec='libx264',
            audio_codec='aac',
            temp_audiofile='temp-audio-FLICKERING.m4a',
            remove_temp=True
        )
        print(f"✅ Video generation complete! Output: {output_file}")
        print(f"⚡ FLICKERING VIDEO READY: {output_file}")
        return output_file
    except Exception as e:
        print(f"❌ Error during video export: {e}")
        return None

if __name__ == "__main__":
    generate_video()