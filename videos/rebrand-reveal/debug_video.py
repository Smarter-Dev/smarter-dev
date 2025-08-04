#!/usr/bin/env python3
"""
Debug version - Test each element individually
"""

import os
from pathlib import Path
from moviepy import *
import numpy as np

def test_basic_elements():
    """Test each element individually."""
    print("🔍 Testing basic video elements...")
    
    base_path = Path(__file__).parent.resolve()
    assets_path = base_path / "assets"
    audio_path = base_path / "audio" 
    output_path = base_path / "output"
    output_path.mkdir(exist_ok=True)
    
    VIDEO_WIDTH = 1920
    VIDEO_HEIGHT = 1080
    
    # 1. Test text rendering
    print("1️⃣ Testing text rendering...")
    try:
        text_clip = (TextClip("TEST TEXT", fontsize=100, color='white')
                    .with_position('center')
                    .with_duration(3))
        
        final = CompositeVideoClip([
            ColorClip((VIDEO_WIDTH, VIDEO_HEIGHT), color=(50, 50, 50)).with_duration(3),
            text_clip
        ])
        
        output_file = output_path / "test_text.mp4"
        final.write_videofile(str(output_file), fps=24)
        print("✅ Text test successful")
    except Exception as e:
        print(f"❌ Text test failed: {e}")
    
    # 2. Test countdown specifically
    print("2️⃣ Testing countdown...")
    try:
        clips = [ColorClip((VIDEO_WIDTH, VIDEO_HEIGHT), color=(20, 20, 40)).with_duration(5)]
        
        for i, number in enumerate([5, 4, 3, 2, 1]):
            start_time = i * 1.0
            text_clip = (TextClip(str(number), 
                                fontsize=200, 
                                color='cyan')
                        .with_position('center')
                        .with_start(start_time)
                        .with_duration(1.0))
            clips.append(text_clip)
            print(f"Added countdown: {number} at {start_time}s")
        
        final = CompositeVideoClip(clips)
        output_file = output_path / "test_countdown.mp4"
        final.write_videofile(str(output_file), fps=24)
        print("✅ Countdown test successful")
    except Exception as e:
        print(f"❌ Countdown test failed: {e}")
    
    # 3. Test audio
    print("3️⃣ Testing audio...")
    try:
        # Test if audio files exist and can be loaded
        audio_files = [
            "bass-rumble.wav",
            "beep-1.mp3", 
            "beep-2.mp3",
            "beep-3.mp3",
            "epic-hit.mp3",
            "background-music.mp3"
        ]
        
        working_audio = []
        for audio_file in audio_files:
            audio_path_full = audio_path / audio_file
            if audio_path_full.exists():
                try:
                    audio_clip = AudioFileClip(str(audio_path_full))
                    print(f"✅ {audio_file}: Duration {audio_clip.duration:.2f}s")
                    working_audio.append(audio_clip.with_start(0).with_duration(2))
                except Exception as e:
                    print(f"❌ {audio_file}: {e}")
            else:
                print(f"⚠️  {audio_file}: File not found")
        
        if working_audio:
            # Create simple video with audio
            background = ColorClip((VIDEO_WIDTH, VIDEO_HEIGHT), color=(30, 30, 60)).with_duration(5)
            test_audio = CompositeAudioClip(working_audio).with_duration(5)
            
            final = background.with_audio(test_audio)
            output_file = output_path / "test_audio.mp4"
            final.write_videofile(str(output_file), fps=24)
            print("✅ Audio test successful")
        else:
            print("❌ No working audio files found")
            
    except Exception as e:
        print(f"❌ Audio test failed: {e}")
    
    # 4. Test image loading
    print("4️⃣ Testing image loading...")
    try:
        required_images = [
            "video-bg.png",
            "smarter-dev-brain.png",
            "streak.png"
        ]
        
        for img_name in required_images:
            img_path = assets_path / img_name
            if img_path.exists():
                try:
                    img_clip = ImageClip(str(img_path))
                    print(f"✅ {img_name}: Size {img_clip.size}")
                except Exception as e:
                    print(f"❌ {img_name}: {e}")
            else:
                print(f"⚠️  {img_name}: File not found")
                
    except Exception as e:
        print(f"❌ Image test failed: {e}")

if __name__ == "__main__":
    test_basic_elements()