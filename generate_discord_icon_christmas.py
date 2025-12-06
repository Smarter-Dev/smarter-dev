#!/usr/bin/env python3
"""
Discord Server Icon GIF Generator
Creates an animated 256x256 GIF suitable for Discord server icons with:
- SD brain PNG
- Grid overlay with blend mode
- Very dark blue background
- Up to 4 animated streaks
- Perfect loop at 30fps
"""

import math
import random
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

# Configuration
ICON_SIZE = 256
FPS = 30
NUM_STREAKS = 5  # Fixed number of streaks

# Colors (RGB)
VERY_DARK_BLUE = (2, 0, 15)  # #02000F background
WHITE = (255, 255, 255)
STREAK_COLOR = (0, 225, 255)  # Cyan like the brand

# Grid configuration
GRID_SPACING = 32  # Scaled down from 120px for 256px canvas
GRID_ANGLE = -15  # degrees
STREAK_SPEED = 120  # pixels per second - increased for more visible movement
MAX_STREAKS = 4

# Asset paths
BRAIN_PATH = Path("resources/smarter-dev-brain-no-glow.png")
GRID_PATH = Path("resources/bg-grid.png")
OUTPUT_PATH = Path("discord_server_icon.gif")

class StreakAnimation:
    """Individual animated streak following a fixed path loop."""

    def __init__(self, path_start_x, path_start_y, path_length, start_offset, color):
        self.path_start_x = path_start_x
        self.path_start_y = path_start_y
        self.path_length = path_length  # Total length of the path
        self.start_offset = start_offset  # Initial position on path (0 to path_length)

        # Movement direction: down and to the right at -15 degrees
        angle_rad = math.radians(GRID_ANGLE)
        self.dx = math.sin(angle_rad)  # Normalized direction
        self.dy = math.cos(angle_rad)  # Normalized direction

        self.speed_per_frame = STREAK_SPEED / FPS

        self.color = color

        self.imgcached = None

    def get_position(self, frame):
        """Get position for specific frame."""
        # Calculate distance traveled along path
        distance_traveled = (self.start_offset + frame * self.speed_per_frame) % self.path_length

        # Convert distance to x,y coordinates along the angled path
        x = self.path_start_x + distance_traveled * self.dx
        y = self.path_start_y + distance_traveled * self.dy

        return (x, y)

    def get_alpha(self, frame):
        """Get alpha for specific frame - always full opacity."""
        return 255  # Always visible

    def is_visible(self, frame):
        """Check if streak is visible at this frame."""
        return True  # Always visible in loop system

    def apply_color(self, im):
        if self.imgcached == None:
            # Split channels
            _,_,_,a = im.split()
            # Create new RGB image filled with the desired color
            new_rgb = Image.new("RGB", im.size, self.color)
            # Combine with original alpha
            self.imgcached = Image.merge("RGBA", (*new_rgb.split(), a))

        return self.imgcached


class DiscordIconGenerator:
    """Generates animated Discord server icon GIF."""

    def __init__(self):
        self.frames = []
        self.streaks = []
        self.grid_overlay = None
        self.brain_image = None

    def load_assets(self):
        """Load brain image and streak image."""
        print("📋 Loading assets...")

        # Load brain image
        if BRAIN_PATH.exists():
            brain = Image.open(BRAIN_PATH).convert("RGBA")

            _,_,_,a = brain.split()
            # Create new RGB image filled with the desired color
            new_rgb = Image.new("RGB", brain.size, (255, 215, 0))
            # Combine with original alpha
            brain = Image.merge("RGBA", (*new_rgb.split(), a))

            # Resize to fit nicely in 256x256 (leave space for grid effect)
            brain_size = int(ICON_SIZE * 0.6)  # 60% of icon size
            brain = brain.resize((brain_size, brain_size), Image.Resampling.LANCZOS)

            # Center the brain vertically and horizontally
            self.brain_image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
            brain_x = (ICON_SIZE - brain_size) // 2
            brain_y = (ICON_SIZE - brain_size) // 2
            self.brain_image.paste(brain, (brain_x, brain_y), brain)
            print(f"✅ Loaded brain image: {brain_size}x{brain_size}")
        else:
            print(f"⚠️  Brain image not found at {BRAIN_PATH}, creating placeholder")
            self.brain_image = self.create_brain_placeholder()

        # Load streak image
        streak_path = Path("resources/streak.png")
        if streak_path.exists():
            self.streak_image = Image.open(streak_path).convert("RGBA")
            # Rotate the streak by -15 degrees (DO NOT RESIZE)
            self.rotated_streak = self.streak_image.rotate(GRID_ANGLE, expand=True)

            # Get actual dimensions after rotation
            self.streak_width = self.rotated_streak.width
            self.streak_height = self.rotated_streak.height

            print(f"✅ Loaded streak image: {self.streak_image.width}x{self.streak_image.height}")
            print(f"✅ Rotated streak dimensions: {self.streak_width}x{self.streak_height}")
        else:
            print(f"⚠️  Streak image not found, skipping streaks")
            self.streak_image = None
            self.rotated_streak = None
            self.streak_width = 0
            self.streak_height = 0

    def create_brain_placeholder(self):
        """Create a simple brain placeholder if image not found."""
        brain = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(brain)

        # Simple brain-like shape
        center_x = ICON_SIZE // 2
        center_y = ICON_SIZE // 2
        radius = int(ICON_SIZE * 0.25)

        # Main circle
        draw.ellipse([center_x - radius, center_y - radius,
                     center_x + radius, center_y + radius],
                    fill=(100, 150, 255, 200))

        # Add some "brain folds"
        for i in range(3):
            offset_x = random.randint(-radius//2, radius//2)
            offset_y = random.randint(-radius//2, radius//2)
            small_radius = radius // 3
            draw.ellipse([center_x + offset_x - small_radius,
                         center_y + offset_y - small_radius,
                         center_x + offset_x + small_radius,
                         center_y + offset_y + small_radius],
                        fill=(120, 180, 255, 150))

        return brain

    def create_grid_overlay(self):
        """Create grid overlay pattern."""
        grid = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(grid)

        # Convert angle to radians for slope calculation
        angle_rad = math.radians(GRID_ANGLE)
        slope = math.tan(angle_rad)

        # Draw horizontal grid lines
        y = 0
        while y < ICON_SIZE:
            draw.line([(0, y), (ICON_SIZE, y)], fill=WHITE + (100,), width=1)
            y += GRID_SPACING

        # Draw angled vertical grid lines
        start_x = -abs(slope * ICON_SIZE) - GRID_SPACING
        x = start_x
        while x < ICON_SIZE + abs(slope * ICON_SIZE) + GRID_SPACING:
            # Calculate line endpoints
            x1 = x
            y1 = 0
            x2 = x + slope * ICON_SIZE
            y2 = ICON_SIZE

            # Only draw if line intersects the canvas
            if x2 > -50 and x1 < ICON_SIZE + 50:
                draw.line([(x1, y1), (x2, y2)], fill=WHITE + (100,), width=1)

            x += GRID_SPACING

        self.grid_overlay = grid

    def setup_streaks(self):
        """Initialize 5 streak animations on fixed rails for perfect looping."""
        print("⚡ Setting up streak rail system...")

        if not self.rotated_streak:
            print("⚠️  No streak image available")
            return

        # Calculate path length - long enough to go from off-screen to off-screen
        buffer = self.streak_height + 100
        path_length = ICON_SIZE + 2 * buffer

        # Create 5 equally spaced path positions
        path_spacing = ICON_SIZE // 5  # 51.2 pixels between each path
        path_positions = [
            path_spacing // 2,                          # Path 1: x = 25
            path_spacing * 1 + path_spacing // 2,       # Path 2: x = 76
            path_spacing * 2 + path_spacing // 2,       # Path 3: x = 128
            path_spacing * 3 + path_spacing // 2,       # Path 4: x = 179
            path_spacing * 4 + path_spacing // 2,       # Path 5: x = 230
        ]

        colors = [
            (255,0,0),
            (255,120,120),
            (255,255,255),
            (116,214,128),
            (55,139,41)
        ]
        random.shuffle(colors)

        # Create streaks with spacing that ensures continuous coverage
        # Use offsets that guarantee overlap while maintaining irregularity
        timing_variations = [0.0, 0.18, 0.35, 0.53, 0.71]  # Spaced to ensure coverage
        start_offsets = [int(variation * path_length) for variation in timing_variations]
        random.shuffle(start_offsets)  # Randomize which streak gets which timing

        # Add primary streaks (one per path)
        for i in range(5):
            path_start_x = path_positions[i]
            path_start_y = -buffer  # Start above frame

            streak = StreakAnimation(path_start_x, path_start_y, path_length, start_offsets[i], colors[i])
            self.streaks.append(streak)

        # Add second streaks to three random paths, offset by half the path duration
        paths_with_double_streaks = random.sample(range(5), 3)  # Pick 3 random paths
        half_path_offset = path_length // 2

        for path_idx in paths_with_double_streaks:
            path_start_x = path_positions[path_idx]
            path_start_y = -buffer

            # Second streak offset by half the path length from the first streak
            second_streak_offset = (start_offsets[path_idx] + half_path_offset) % path_length
            streak = StreakAnimation(path_start_x, path_start_y, path_length, second_streak_offset, colors[path_idx])
            self.streaks.append(streak)

        # Calculate total frames for perfect loop
        # The loop completes when all streaks return to their original relative positions
        frames_per_path = path_length / (STREAK_SPEED / FPS)
        self.total_frames = int(frames_per_path)

        print(f"✅ Created {len(self.streaks)} streak animations on rails (5 paths, 3 with double streaks)")
        print(f"✅ Paths with double streaks: {paths_with_double_streaks}")
        print(f"✅ Path length: {path_length} pixels, Loop duration: {self.total_frames} frames")

    def apply_overlay_blend(self, base, overlay, opacity=0.3):
        """Apply overlay blend mode between two images."""
        # Convert to numpy arrays for processing
        base_array = np.array(base, dtype=np.float32) / 255.0
        overlay_array = np.array(overlay, dtype=np.float32) / 255.0

        # Extract RGB channels (ignore alpha for blend calculation)
        base_rgb = base_array[:, :, :3]
        overlay_rgb = overlay_array[:, :, :3]
        overlay_alpha = overlay_array[:, :, 3] if overlay_array.shape[2] == 4 else np.ones(overlay_rgb.shape[:2])

        # Apply overlay blend formula
        result_rgb = np.where(
            base_rgb <= 0.5,
            2 * base_rgb * overlay_rgb,
            1 - 2 * (1 - base_rgb) * (1 - overlay_rgb)
        )

        # Blend with original based on overlay alpha and opacity
        final_rgb = base_rgb * (1 - overlay_alpha[:, :, np.newaxis] * opacity) + \
                   result_rgb * (overlay_alpha[:, :, np.newaxis] * opacity)

        # Combine with original alpha channel
        if base_array.shape[2] == 4:
            result_array = np.dstack([final_rgb, base_array[:, :, 3]])
        else:
            result_array = final_rgb

        # Convert back to PIL image
        result_array = np.clip(result_array * 255, 0, 255).astype(np.uint8)
        if result_array.shape[2] == 4:
            return Image.fromarray(result_array)
        else:
            return Image.fromarray(result_array)

    def draw_streaks(self, frame):
        """Draw streaks for specific frame using actual streak.png."""
        if not self.rotated_streak:
            return Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))

        streak_layer = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))

        for streak in self.streaks:
            if not streak.is_visible(frame):
                continue

            position = streak.get_position(frame)
            if position is None:  # Finished journey
                continue

            x, y = position
            alpha = int(streak.get_alpha(frame))

            # Only draw if streak has any alpha
            if alpha > 0:
                # Use streak at full opacity
                streak_to_draw = streak.apply_color(self.rotated_streak.copy())

                # Calculate position - use top-left corner of streak, not center
                paste_x = int(x)
                paste_y = int(y)

                # Always draw the streak, even if partially off-screen
                # PIL will automatically clip to the canvas bounds
                try:
                    streak_layer.paste(streak_to_draw, (paste_x, paste_y), streak_to_draw)
                except:
                    # If paste fails (completely off screen), skip silently
                    pass

        return streak_layer

    def generate_frame(self, frame_num):
        """Generate a single frame."""
        # Create base background
        frame = Image.new("RGB", (ICON_SIZE, ICON_SIZE), VERY_DARK_BLUE)

        # Draw streaks using actual streak.png (behind brain)
        streaks_layer = self.draw_streaks(frame_num)
        if not all(pixel[3] == 0 for pixel in streaks_layer.getdata()):  # If not empty
            frame_rgba = frame.convert("RGBA")
            frame_rgba = Image.alpha_composite(frame_rgba, streaks_layer)
            frame = frame_rgba.convert("RGB")

        # Add brain image on top (top layer)
        if self.brain_image:
            frame_rgba = frame.convert("RGBA")
            frame_rgba = Image.alpha_composite(frame_rgba, self.brain_image)
            frame = frame_rgba.convert("RGB")

        # Grid overlay removed per user request
        frame_rgba = frame.convert("RGBA")

        return frame_rgba.convert("RGB")

    def generate_gif(self):
        """Generate the complete animated GIF."""
        print(f"🎬 Generating {self.total_frames} frames at {FPS}fps...")

        self.frames = []
        for frame_num in range(self.total_frames):
            frame = self.generate_frame(frame_num)
            self.frames.append(frame)

            if (frame_num + 1) % 10 == 0:
                print(f"📽️  Generated {frame_num + 1}/{self.total_frames} frames")

        print("💾 Saving GIF...")

        # Save as GIF with optimization
        self.frames[0].save(
            OUTPUT_PATH,
            save_all=True,
            append_images=self.frames[1:],
            duration=int(1000/FPS),  # Duration in milliseconds
            loop=0,  # Infinite loop
            optimize=True,
            quality=85
        )

        print(f"✅ Discord server icon saved: {OUTPUT_PATH}")
        print(f"📊 Final size: {OUTPUT_PATH.stat().st_size / 1024:.1f} KB")

def main():
    """Generate the Discord server icon GIF."""
    print("🎯 Generating Discord Server Icon GIF")
    print(f"📐 Size: {ICON_SIZE}x{ICON_SIZE}")
    print(f"⚡ Number of streaks: {NUM_STREAKS}")

    generator = DiscordIconGenerator()

    try:
        generator.load_assets()
        generator.setup_streaks()
        generator.generate_gif()

        print("\n🎉 Discord server icon generation complete!")
        print(f"📁 Output: {OUTPUT_PATH.absolute()}")

    except Exception as e:
        print(f"❌ Error generating icon: {e}")
        raise

if __name__ == "__main__":
    main()