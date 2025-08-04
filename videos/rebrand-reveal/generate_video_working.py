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
    
    # Create background with overlay blend mode grid
    print("📽️  Creating background with overlay blend mode grid...")
    try:
        bg_img = ImageClip(str(assets_path / "video-bg.png"))
        background = bg_img.resized((VIDEO_WIDTH, VIDEO_HEIGHT)).with_duration(TOTAL_DURATION)
        
        # Apply overlay blend mode to background
        try:
            def overlay_blend(base_frame, overlay_frame, opacity=0.8):
                """
                Implement true overlay blend mode using the mathematical formula:
                - if base <= 0.5: result = 2 * base * overlay
                - else: result = 1 - 2 * (1 - base) * (1 - overlay)
                """
                # Normalize to 0-1 range
                base = base_frame.astype(np.float32) / 255.0
                overlay = overlay_frame.astype(np.float32) / 255.0
                
                # Apply overlay blend formula
                mask = base <= 0.5
                result = np.zeros_like(base)
                
                # For dark areas: 2 * base * overlay (makes overlay nearly invisible)
                result[mask] = 2 * base[mask] * overlay[mask]
                
                # For light areas: 1 - 2 * (1 - base) * (1 - overlay) (makes overlay glow)
                result[~mask] = 1 - 2 * (1 - base[~mask]) * (1 - overlay[~mask])
                
                # Apply opacity and convert back to 0-255 range
                result = base + opacity * (result - base)
                return np.clip(result * 255, 0, 255).astype(np.uint8)
            
            # Load grid image and get frame data
            grid_img = ImageClip(str(assets_path / "bg-grid.png"))
            grid_img = grid_img.resized((VIDEO_WIDTH, VIDEO_HEIGHT))
            grid_frame = grid_img.get_frame(0)
            
            # Apply overlay blend to background using fl_image
            def apply_grid_blend(frame):
                return overlay_blend(frame, grid_frame, opacity=0.8)
            
            background = background.fl_image(apply_grid_blend)
            print("✅ Background loaded with true overlay blend mode grid")
        except Exception as e:
            print(f"⚠️ Could not apply overlay blend, using plain background: {e}")
        
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
        # Brain logo with reverse glitch reveal effect
        brain_img = ImageClip(str(assets_path / "smarter-dev-brain.png"))
        
        def create_reverse_glitch_brain():
            """Create brain logo with reverse glitch reveal effect."""
            brain_clips = []
            glitch_start = REVEAL_START + 7.5  # Start 3 seconds after Smarter Dev text
            glitch_duration = 2.0  # 2 seconds of reverse glitch effect
            
            # Create the base brain image
            base_brain = brain_img.resized(height=150).with_position(('center', VIDEO_HEIGHT // 2 - 200))
            
            # Create reverse flickering pattern: starts invisible, gradually becomes more visible
            flicker_frame_duration = 0.08  # Fast flicker (80ms per frame)
            num_flicker_frames = int(glitch_duration / flicker_frame_duration)
            
            for i in range(num_flicker_frames):
                frame_start = glitch_start + i * flicker_frame_duration
                
                # Reverse glitch pattern - starts with low visibility, gradually increases
                visibility_chance = min(1.0, (i / num_flicker_frames) * 1.3 + 0.1)  # 10% to 100% chance
                
                if np.random.random() < visibility_chance:
                    # Visible frame - starts dimmed, gradually gets brighter
                    opacity_progression = i / num_flicker_frames
                    if opacity_progression < 0.3:
                        opacity = np.random.choice([0.2, 0.4, 0.6], p=[0.5, 0.3, 0.2])
                    elif opacity_progression < 0.7:
                        opacity = np.random.choice([0.4, 0.6, 0.8], p=[0.3, 0.4, 0.3])
                    else:
                        opacity = np.random.choice([0.6, 0.8, 1.0], p=[0.2, 0.3, 0.5])
                    
                    glitch_frame = (base_brain
                                   .with_opacity(opacity)
                                   .with_start(frame_start)
                                   .with_duration(flicker_frame_duration))
                    brain_clips.append(glitch_frame)
                # If not visible, don't add any frame (creates the reverse flicker gap)
            
            # Add final solid brain logo after glitch effect ends
            final_brain = (base_brain
                          .with_start(glitch_start + glitch_duration)
                          .with_duration(CLOSING_END - glitch_start - glitch_duration))
            brain_clips.append(final_brain)
            
            return brain_clips
        
        # Add reverse glitch brain effect
        brain_clips = create_reverse_glitch_brain()
        clips.extend(brain_clips)
        print(f"🧠 Added brain logo with reverse glitch effect ({len(brain_clips)} frames over 2 seconds)")
        
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
                font_size=45, 
                color=CYAN,
                font=bruno_font,
                size=(200, 100),
                method='caption'
            )
            
            # Center "SMARTER DEV" using the original 215px relative positioning
            smarter_width = smarter_text.size[0]    # Get actual rendered width of SMARTER
            dev_width = dev_text.size[0]            # Get actual rendered width of DEV
            relative_gap = 315  # Original gap (215px) + 100px nudge
            
            total_width = relative_gap + dev_width  # Total span from start of SMARTER to end of DEV
            start_x = (800 - total_width) // 2     # Center this total span
            
            brand_text = CompositeVideoClip([
                smarter_text.with_position((start_x, 0)),                    # SMARTER at start of centered span
                dev_text.with_position((start_x + relative_gap, 10))         # DEV at original 215px offset
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
            
            # Center the composite so the actual text content is centered on screen
            # Our text starts at x=142 within the 800px composite
            # To center the actual text: screen_center (960px) - composite_start - text_start_within_composite
            # We want: (1920 - total_width) / 2 to be where our text actually appears
            composite_x = (1920 - total_width) // 2 - start_x
            print(f"📏 Positioning brand composite at x={composite_x} so text appears centered")
            
            brand_text = (brand_text
                         .with_position((composite_x, 'center'))
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
        
        # Subtitle with typing effect and flashing cursor
        try:
            def create_typing_effect():
                """Create typing effect with separate text and cursor clips to prevent shifting."""
                typing_clips = []
                typing_start = REVEAL_START + 7.5  # Start same time as brain glitch
                full_text = "Level up your code"
                typing_speed = 0.08  # 80ms per character
                cursor_blink_speed = 0.5  # 500ms cursor blink cycle
                
                # Create the full text clip to measure its width and position
                full_text_clip = TextClip(
                    text=full_text,
                    font_size=40,
                    color=WHITE,
                    font=bungee_font,
                    size=(600, 60),
                    method='caption'
                )
                
                # Get the actual rendered width to calculate positioning
                full_text_width = full_text_clip.size[0]
                text_x = (VIDEO_WIDTH - full_text_width) // 2  # Center the full text
                text_y = VIDEO_HEIGHT // 2 + 80
                
                # Create typing animation - progressive text reveal
                for i in range(len(full_text) + 1):
                    partial_text = full_text[:i]
                    frame_start = typing_start + i * typing_speed
                    
                    if partial_text:
                        # Create partial text clip
                        partial_clip = TextClip(
                            text=partial_text,
                            font_size=40,
                            color=WHITE,
                            font=bungee_font,
                            size=(600, 60),
                            method='caption'
                        )
                        
                        # Position at same location as full text will be
                        partial_clip = (partial_clip
                                      .with_position((text_x, text_y))
                                      .with_start(frame_start)
                                      .with_duration(typing_speed))
                        typing_clips.append(partial_clip)
                        
                        # Add cursor positioned after the partial text
                        if i < len(full_text):
                            # Measure partial text width to position cursor
                            partial_width = partial_clip.size[0]
                            cursor_x = text_x + partial_width
                            
                            cursor_clip = TextClip(
                                text="_",
                                font_size=40,
                                color=WHITE,
                                font=bungee_font,
                                size=(20, 60),
                                method='caption'
                            )
                            
                            cursor_clip = (cursor_clip
                                         .with_position((cursor_x, text_y))
                                         .with_start(frame_start)
                                         .with_duration(typing_speed))
                            typing_clips.append(cursor_clip)
                
                # After typing is complete, show final text with blinking cursor
                typing_end = typing_start + len(full_text) * typing_speed
                final_text_duration = CLOSING_END - typing_end
                
                # Add stable final text
                final_text_clip = (full_text_clip
                                 .with_position((text_x, text_y))
                                 .with_start(typing_end)
                                 .with_duration(final_text_duration))
                typing_clips.append(final_text_clip)
                
                # Add blinking cursor after final text
                cursor_x = text_x + full_text_width
                cursor_blink_frames = int(final_text_duration / cursor_blink_speed)
                
                for i in range(cursor_blink_frames):
                    frame_start = typing_end + i * cursor_blink_speed
                    
                    # Only show cursor on even frames (creates blinking effect)
                    if i % 2 == 0:
                        cursor_clip = TextClip(
                            text="_",
                            font_size=40,
                            color=WHITE,
                            font=bungee_font,
                            size=(20, 60),
                            method='caption'
                        )
                        
                        cursor_clip = (cursor_clip
                                     .with_position((cursor_x, text_y))
                                     .with_start(frame_start)
                                     .with_duration(cursor_blink_speed))
                        typing_clips.append(cursor_clip)
                
                return typing_clips
            
            # Add typing effect
            typing_clips = create_typing_effect()
            clips.extend(typing_clips)
            print(f"⌨️  Added typing effect for CTA text ({len(typing_clips)} frames with flashing cursor)")
        except Exception as e:
            print(f"⚠️  Could not create typing effect: {e}")
            # Fallback to regular subtitle
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
                           .with_position(('center', VIDEO_HEIGHT // 2 + 80))
                           .with_start(REVEAL_START + 9.5)
                           .with_duration(CLOSING_END - REVEAL_START - 9.5))
                
                clips.append(subtitle)
                print("✅ Added subtitle (fallback)")
            except Exception as e2:
                print(f"⚠️  Subtitle fallback failed: {e2}")
        
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
    
    # Grid overlay is now applied directly to the background above
    
    # Create final video
    print("🎭 Compositing final video...")
    print(f"📊 Total clips: {len(clips)}")
    
    final_video = CompositeVideoClip(clips, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
    final_video = final_video.with_audio(final_audio).with_duration(TOTAL_DURATION)
    
    # Export video
    output_file = output_path / "smarter_dev_rebrand_reveal_OVERLAY_BLEND.mp4"
    print(f"🚀 Exporting to {output_file}...")
    print(f"📏 Video duration: {TOTAL_DURATION} seconds (30-second final screen)")
    print("⚡ Features: OVERLAY BLEND MODE - Grid overlay with proper blending + Reverse glitch brain + Typing effect + Centered text")
    
    try:
        final_video.write_videofile(
            str(output_file),
            fps=FPS,
            codec='libx264',
            audio_codec='aac',
            temp_audiofile='temp-audio-OVERLAY_BLEND.m4a',
            remove_temp=True
        )
        print(f"✅ Video generation complete! Output: {output_file}")
        print(f"🌐 OVERLAY BLEND MODE VIDEO READY: {output_file}")
        return output_file
    except Exception as e:
        print(f"❌ Error during video export: {e}")
        return None

if __name__ == "__main__":
    generate_video()