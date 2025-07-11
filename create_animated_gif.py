from PIL import Image, ImageDraw
import os

def create_animated_gif():
    # Configuration
    width, height = 256, 256
    fps = 18
    duration_seconds = 3
    total_frames = fps * duration_seconds  # 54 frames
    bg_color = "#02000F"  # Dark blue/black background
    
    # Load images
    brain_path = "resources/smarter-dev-brain-no-glow.png"
    streak_path = "resources/streak-no-glow.png"
    
    # Load brain image (static, centered with 16px top/bottom margin)
    brain_img = Image.open(brain_path).convert("RGBA")
    brain_width, brain_height = brain_img.size
    
    # Calculate available space with 32px margins
    available_height = height - 64  # 32px top + 32px bottom
    
    # Scale brain to fit within available space while maintaining aspect ratio
    if brain_height > available_height:
        scale_factor = available_height / brain_height
        new_width = int(brain_width * scale_factor)
        new_height = int(brain_height * scale_factor)
        brain_img = brain_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        brain_width, brain_height = new_width, new_height
    
    # Center horizontally, position with 32px top margin
    brain_x = (width - brain_width) // 2
    brain_y = 32 + (available_height - brain_height) // 2
    
    # Load streak image
    streak_img = Image.open(streak_path).convert("RGBA")
    streak_width, streak_height = streak_img.size
    
    # Calculate animation parameters
    # Total travel distance: from above canvas to below canvas
    travel_distance = height + streak_height
    pixels_per_frame = travel_distance / total_frames
    
    # Position 5 streaks at different x positions
    streak_x_positions = [
        width * 0.15,   # 15% from left
        width * 0.35,   # 35% from left
        width * 0.55,   # 55% from left
        width * 0.75,   # 75% from left
        width * 0.95    # 95% from left
    ]
    
    # Stagger starting positions with varied ordering (not left to right)
    # Mix up the appearance order for more dynamic animation
    streak_start_offsets = [
        total_frames * 0.6,   # Third streak to appear (60% delay)
        0,                    # First streak starts immediately
        total_frames * 0.8,   # Fifth streak to appear (80% delay)
        total_frames * 0.2,   # Second streak to appear (20% delay)
        total_frames * 0.4    # Fourth streak to appear (40% delay)
    ]
    
    frames = []
    
    for frame_num in range(total_frames):
        # Create new frame with background color
        frame = Image.new("RGBA", (width, height), bg_color)
        
        # Draw streaks first (background layer)
        for i, (x_pos, start_offset) in enumerate(zip(streak_x_positions, streak_start_offsets)):
            # Calculate current position for this streak
            adjusted_frame = (frame_num + start_offset) % total_frames
            y_pos = -streak_height + (adjusted_frame * pixels_per_frame)
            
            # Only draw if streak is visible (partially or fully on screen)
            if y_pos > -streak_height and y_pos < height:
                streak_x = int(x_pos - streak_width // 2)
                streak_y = int(y_pos)
                
                # Paste streak with transparency
                frame.paste(streak_img, (streak_x, streak_y), streak_img)
        
        # Draw brain on top (foreground layer - always visible)
        frame.paste(brain_img, (brain_x, brain_y), brain_img)
        
        # Convert to RGB for GIF (with transparency handled properly)
        frame_rgb = Image.new("RGB", (width, height), bg_color)
        frame_rgb.paste(frame, (0, 0), frame)
        
        frames.append(frame_rgb)
    
    # Save as animated GIF
    output_path = "animated_logo.gif"
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=int(1000 / fps),  # Duration in milliseconds
        loop=0,  # Infinite loop
        optimize=True
    )
    
    print(f"Animated GIF created: {output_path}")
    print(f"Dimensions: {width}x{height}")
    print(f"Frames: {total_frames}")
    print(f"FPS: {fps}")
    print(f"Duration: {duration_seconds} seconds")

if __name__ == "__main__":
    create_animated_gif()