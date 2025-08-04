#!/usr/bin/env python3
"""
Smarter Dev Rebrand Reveal Video Generator - Final Working Version
Epic "Level Up" reveal video with smooth countdown transitions using basic effects.
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
        
        # Create opacity effect - fade in and out
        def opacity_func(t):
            if t < 0.1:  # Fade in
                return t / 0.1
            elif t < 0.9:  # Stay visible
                return 1.0
            else:  # Fade out
                return 1.0 - ((t - 0.9) / 0.1)
        
        text_clip = (text_clip
                    .with_position(('center', 'center'))
                    .with_start(start_time)
                    .with_duration(1.0)
                    .resized(scale_func)
                    .with_opacity(opacity_func))
        
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
        
        def opacity_func(t):
            if t < 0.1:
                return t / 0.1
            elif t < 0.9:
                return 1.0
            else:
                return 1.0 - ((t - 0.9) / 0.1)
        
        text_clip = (text_clip
                    .with_position(('center', 'center'))
                    .with_start(start_time)
                    .with_duration(1.0)
                    .resized(scale_func)
                    .with_opacity(opacity_func))
        
        return text_clip

def generate_video():
    """Generate the final rebrand reveal video."""
    print("🎬 Starting Smarter Dev Rebrand Reveal Video Generation (Final Version)...")
    
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
    
    # Create "BEGINNER.CODES" transition
    print("⚡ Creating BEGINNER.CODES transition...")
    bungee_font_path = assets_path / "fonts" / "Bungee Hairline"
    bungee_font = str(bungee_font_path / "BungeeHairline-Regular.ttf") if bungee_font_path.exists() else None
    
    try:
        old_brand = TextClip(
            text="BEGINNER.CODES", 
            font_size=60, 
            color=WHITE,
            font=bungee_font,
            size=(800, 100),
            method='caption'
        )
        
        # Create a glitch-like opacity effect
        def glitch_opacity(t):
            if t < 0.1:
                return t / 0.1  # Fade in
            elif t < 0.5:
                return 1.0  # Stay visible
            else:
                # Glitch out effect - flickering opacity
                flicker = 0.5 + 0.5 * np.sin(t * 50)  # Fast flicker
                fade = 1.0 - ((t - 0.5) / 0.5)  # Overall fade out
                return flicker * fade
        
        old_brand = (old_brand
                    .with_position(('center', 'center'))
                    .with_start(REVEAL_START)
                    .with_duration(1.0)
                    .with_opacity(glitch_opacity))
        
        clips.append(old_brand)
        print("✅ Added BEGINNER.CODES with glitch effect")
    except Exception as e:
        print(f"⚠️  Could not create BEGINNER.CODES: {e}")
    
    # Create brand reveal
    print("🧠 Creating brand reveal...")
    try:
        # Brain logo with smooth scale entrance
        brain_img = ImageClip(str(assets_path / "smarter-dev-brain.png"))
        
        def brain_scale(t):
            if t < 0.5:
                return 0.8 + 0.2 * (t / 0.5)  # Scale from 80% to 100%
            else:
                return 1.0
        
        def brain_opacity(t):
            if t < 0.5:
                return t / 0.5  # Fade in over 0.5 seconds
            else:
                return 1.0
        
        brain_clip = (brain_img
                     .resized(height=300)
                     .with_start(BRAND_START)
                     .with_duration(CLOSING_END - BRAND_START)
                     .with_position(('center', VIDEO_HEIGHT // 2 - 200))
                     .resized(brain_scale)
                     .with_opacity(brain_opacity))
        clips.append(brain_clip)
        print("✅ Added brain logo")
        
        # "Smarter" text with slide-up effect
        try:
            smarter_text = TextClip(
                text="smarter", 
                font_size=80, 
                color=WHITE,
                font=bungee_font,
                size=(400, 120),
                method='caption'
            )
            
            def slide_position(t):
                if t < 0.4:
                    slide_offset = 30 * (1 - t / 0.4)  # Slide from 30px below
                    return ('center', VIDEO_HEIGHT // 2 + 50 + slide_offset)
                else:
                    return ('center', VIDEO_HEIGHT // 2 + 50)
            
            def slide_opacity(t):
                if t < 0.4:
                    return t / 0.4
                else:
                    return 1.0
            
            smarter_text = (smarter_text
                           .with_position(slide_position)
                           .with_start(BRAND_START + 0.5)
                           .with_duration(CLOSING_END - BRAND_START - 0.5)
                           .with_opacity(slide_opacity))
            
            clips.append(smarter_text)
            print("✅ Added 'smarter' text with slide-up")
        except Exception as e:
            # Simple fallback
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
            print("✅ Added 'smarter' text (fallback)")
        
        # "Dev" text with slide-up effect
        try:
            dev_text = TextClip(
                text="dev", 
                font_size=80, 
                color=CYAN,
                font=bruno_font,
                size=(200, 120),
                method='caption'
            )
            
            def dev_slide_position(t):
                if t < 0.4:
                    slide_offset = 30 * (1 - t / 0.4)
                    return ('center', VIDEO_HEIGHT // 2 + 150 + slide_offset)
                else:
                    return ('center', VIDEO_HEIGHT // 2 + 150)
            
            def dev_slide_opacity(t):
                if t < 0.4:
                    return t / 0.4
                else:
                    return 1.0
            
            dev_text = (dev_text
                       .with_position(dev_slide_position)
                       .with_start(BRAND_START + 1.0)
                       .with_duration(CLOSING_END - BRAND_START - 1.0)
                       .with_opacity(dev_slide_opacity))
            
            clips.append(dev_text)
            print("✅ Added 'dev' text with slide-up")
        except Exception as e:
            # Simple fallback
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
            print("✅ Added 'dev' text (fallback)")
        
        # Subtitle with gentle fade
        try:
            subtitle = TextClip(
                text="Level up your code", 
                font_size=40, 
                color=WHITE,
                font=bungee_font,
                size=(600, 60),
                method='caption'
            )
            
            def subtitle_opacity(t):
                if t < 1.0:
                    return t / 1.0  # Slow fade in
                else:
                    return 1.0
            
            subtitle = (subtitle
                       .with_position(('center', VIDEO_HEIGHT // 2 + 280))
                       .with_start(BRAND_END)
                       .with_duration(CLOSING_END - BRAND_END)
                       .with_opacity(subtitle_opacity))
            
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
    output_file = output_path / "smarter_dev_rebrand_reveal_FINAL.mp4"
    print(f"🚀 Exporting to {output_file}...")
    
    try:
        final_video.write_videofile(
            str(output_file),
            fps=FPS,
            codec='libx264',
            audio_codec='aac',
            temp_audiofile='temp-audio-FINAL.m4a',
            remove_temp=True
        )
        print(f"✅ Video generation complete! Output: {output_file}")
        print(f"🎉 FINAL VIDEO READY: {output_file}")
        return output_file
    except Exception as e:
        print(f"❌ Error during video export: {e}")
        return None

if __name__ == "__main__":
    generate_video()