#!/usr/bin/env python3
"""
Interactive Smarter Dev Rebrand Reveal
Live countdown to August 8th noon ET with interactive transformation sequence.
"""

import pygame
import sys
import datetime
import pytz
import random
import math
import numpy as np
import argparse
from pathlib import Path

# Configuration
SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080
FPS = 60

# Colors
CYAN = (0, 225, 255)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
DARK_BG = (20, 20, 40)

# Grid configuration
GRID_SPACING = 120
GRID_ANGLE = -15  # degrees
STREAK_SPEED = 200  # pixels per second

# Target datetime: August 8th noon Eastern Time (EDT in summer)
eastern_tz = pytz.timezone('US/Eastern')
TARGET_DATETIME = eastern_tz.localize(datetime.datetime(2025, 8, 8, 12, 0, 0))

class GridRenderer:
    """Handles grid generation and overlay blending."""
    
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.grid_surface = None
        self.blend_cache = {}  # Cache for expensive blend operations
        self.cached_blend_surface = None  # Full blended surface cache
        self.dirty_rects = []  # Rectangles that need re-blending
        self.tile_size = 128  # Size of each tile for dirty rectangle system
        self.generate_grid()
    
    def generate_grid(self):
        """Generate procedural grid with 120px spacing and -15° vertical lines."""
        self.grid_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        self.grid_surface.fill((0, 0, 0, 0))  # Transparent
        
        # Convert angle to radians
        angle_rad = math.radians(GRID_ANGLE)
        slope = math.tan(angle_rad)
        
        # Draw horizontal grid lines (straight)
        y = 0
        while y < self.height:
            pygame.draw.line(self.grid_surface, WHITE, (0, y), (self.width, y), 1)
            y += GRID_SPACING
        
        # Draw vertical grid lines (angled at -15°)
        # Start from negative x to ensure coverage on the left edge
        start_x = -abs(slope * self.height) - GRID_SPACING
        x = start_x
        while x < self.width + abs(slope * self.height) + GRID_SPACING:
            # Calculate line endpoints for angled vertical lines
            x1 = x
            y1 = 0
            x2 = x + slope * self.height  # Offset by slope * height
            y2 = self.height
            
            # Only draw if line intersects the screen (with generous buffer)
            if x2 > -100 and x1 < self.width + 100:
                pygame.draw.line(self.grid_surface, WHITE, (x1, y1), (x2, y2), 1)
            
            x += GRID_SPACING
    
    def apply_overlay_blend(self, base_surface):
        """Apply overlay blend mode to combine grid with base surface."""
        # Get pixel arrays
        base_array = pygame.surfarray.array3d(base_surface).astype(float) / 255.0
        grid_array = pygame.surfarray.array3d(self.grid_surface).astype(float) / 255.0
        
        # Get alpha channel from grid
        try:
            grid_alpha = pygame.surfarray.array_alpha(self.grid_surface).astype(float) / 255.0
        except:
            # Fallback: use brightness as alpha
            grid_alpha = np.mean(grid_array, axis=2)
        
        # Apply overlay blend formula
        result = base_array.copy()
        
        # Create mask for areas where grid has content
        blend_mask = grid_alpha > 0.01
        
        if np.any(blend_mask):
            for c in range(3):  # RGB channels
                base_channel = base_array[:, :, c]
                overlay_channel = grid_array[:, :, c]
                
                # Overlay blend formula
                overlay_result = np.where(
                    base_channel <= 0.5,
                    2 * base_channel * overlay_channel,
                    1 - 2 * (1 - base_channel) * (1 - overlay_channel)
                )
                
                # Apply 50% opacity blend where grid has content
                result[:, :, c] = np.where(
                    blend_mask,
                    overlay_result * 0.5 + base_channel * 0.5,  # 50% overlay effect
                    base_channel  # Keep original where grid is transparent
                )
        
        # Convert back to surface
        result_array = (np.clip(result, 0, 1) * 255).astype(np.uint8)
        result_surface = pygame.surfarray.make_surface(result_array)
        
        return result_surface
    
    def mark_dirty_rect(self, rect):
        """Mark a rectangle as dirty for re-blending."""
        # Ensure input rect is valid
        if rect.width <= 0 or rect.height <= 0:
            return
        
        # Expand rect slightly to account for streak size
        expanded_rect = pygame.Rect(rect.x - 50, rect.y - 50, rect.width + 100, rect.height + 100)
        # Clamp to screen bounds
        screen_rect = pygame.Rect(0, 0, self.width, self.height)
        expanded_rect = expanded_rect.clip(screen_rect)
        
        # Skip if rectangle is empty after clipping
        if expanded_rect.width <= 0 or expanded_rect.height <= 0:
            return
        
        # Align to tile boundaries for efficiency
        tile_x = (expanded_rect.x // self.tile_size) * self.tile_size
        tile_y = (expanded_rect.y // self.tile_size) * self.tile_size
        tile_w = ((expanded_rect.right - tile_x + self.tile_size - 1) // self.tile_size) * self.tile_size
        tile_h = ((expanded_rect.bottom - tile_y + self.tile_size - 1) // self.tile_size) * self.tile_size
        
        aligned_rect = pygame.Rect(tile_x, tile_y, tile_w, tile_h)
        aligned_rect = aligned_rect.clip(screen_rect)
        
        # Skip if aligned rectangle is empty
        if aligned_rect.width <= 0 or aligned_rect.height <= 0:
            return
        
        # Add to dirty rects if not already present
        if aligned_rect not in self.dirty_rects:
            self.dirty_rects.append(aligned_rect)
    
    def apply_overlay_blend_optimized(self, base_surface):
        """Apply overlay blend with dirty rectangle optimization."""
        # Initialize cached blend surface if needed
        if self.cached_blend_surface is None:
            self.cached_blend_surface = self.apply_overlay_blend(base_surface)
            return self.cached_blend_surface.copy()
        
        # If no dirty rects, return cached version
        if not self.dirty_rects:
            return self.cached_blend_surface.copy()
        
        # Create result surface from cached blend
        result_surface = self.cached_blend_surface.copy()
        
        # Re-blend only dirty rectangles
        for dirty_rect in self.dirty_rects:
            # Ensure dirty rect is within surface bounds
            clipped_rect = dirty_rect.clip(pygame.Rect(0, 0, self.width, self.height))
            
            # Skip if rectangle is empty after clipping
            if clipped_rect.width <= 0 or clipped_rect.height <= 0:
                continue
            
            # Extract the dirty region from base surface
            base_region = base_surface.subsurface(clipped_rect)
            
            # Apply blend to this region only
            blended_region = self._blend_region(base_region, clipped_rect)
            
            # Blit the blended region back
            result_surface.blit(blended_region, clipped_rect)
            
            # Update cached surface
            self.cached_blend_surface.blit(blended_region, clipped_rect)
        
        # Clear dirty rects
        self.dirty_rects.clear()
        
        return result_surface
    
    def _blend_region(self, base_region, region_rect):
        """Apply overlay blend to a specific region."""
        # Get pixel arrays for the region
        base_array = pygame.surfarray.array3d(base_region).astype(float) / 255.0
        
        # Get corresponding grid region
        grid_region = self.grid_surface.subsurface(region_rect)
        grid_array = pygame.surfarray.array3d(grid_region).astype(float) / 255.0
        
        # Get alpha channel from grid region
        try:
            grid_alpha = pygame.surfarray.array_alpha(grid_region).astype(float) / 255.0
        except:
            grid_alpha = np.mean(grid_array, axis=2)
        
        # Apply overlay blend formula
        result = base_array.copy()
        blend_mask = grid_alpha > 0.01
        
        if np.any(blend_mask):
            for c in range(3):  # RGB channels
                base_channel = base_array[:, :, c]
                overlay_channel = grid_array[:, :, c]
                
                # Overlay blend formula
                overlay_result = np.where(
                    base_channel <= 0.5,
                    2 * base_channel * overlay_channel,
                    1 - 2 * (1 - base_channel) * (1 - overlay_channel)
                )
                
                # Apply 50% opacity blend where grid has content
                result[:, :, c] = np.where(
                    blend_mask,
                    overlay_result * 0.5 + base_channel * 0.5,
                    base_channel
                )
        
        # Convert back to surface
        result_array = (np.clip(result, 0, 1) * 255).astype(np.uint8)
        return pygame.surfarray.make_surface(result_array)

class Streak:
    """Individual animated streak element."""
    
    def __init__(self, start_x, start_y, line_slope, rotated_image):
        self.x = float(start_x)  # Use float for smooth movement
        self.y = float(start_y)
        self.prev_x = float(start_x)  # Previous position for dirty rect tracking
        self.prev_y = float(start_y)
        self.slope = line_slope  # How much x changes per y (for vertical movement)
        self.speed = STREAK_SPEED  # Consistent speed for all streaks
        self.alpha = random.randint(128, 255)  # Varying opacity
        self.has_played_sound = False  # Track if sound has been played for this streak
        
        # Use pre-rotated image instead of rotating each time
        self.rotated_image = rotated_image.copy()
        self.rotated_image.set_alpha(self.alpha)
    
    def update(self, dt):
        """Update streak position falling down the angled vertical line."""
        # Store previous position
        self.prev_x = self.x
        self.prev_y = self.y
        
        # Move down along the angled vertical line
        dy = self.speed * dt
        dx = self.slope * dy  # x changes based on the line's slope
        
        self.x += dx
        self.y += dy
    
    def is_20_percent_visible(self, screen_height):
        """Check if streak is 20% visible on screen."""
        streak_height = self.rotated_image.get_height()
        # Be very conservative - only consider visible when streak is actually entering screen
        # This ensures the user can definitely see the streak when the sound plays
        return self.y >= -streak_height * 0.1  # Only trigger when top 10% above screen at most
    
    def is_off_screen(self, screen_width, screen_height):
        """Check if streak has moved off screen."""
        return self.y > screen_height + 100 or self.x > screen_width + 100 or self.x < -100
    
    def get_dirty_rect(self):
        """Get the rectangle covering both previous and current positions."""
        # Get rects for both positions
        current_rect = self.rotated_image.get_rect(center=(int(self.x), int(self.y)))
        prev_rect = self.rotated_image.get_rect(center=(int(self.prev_x), int(self.prev_y)))
        
        # Union them to cover both areas
        return current_rect.union(prev_rect)
    
    def draw(self, surface):
        """Draw the streak."""
        rect = self.rotated_image.get_rect(center=(int(self.x), int(self.y)))
        # Only draw if the rect intersects with the screen area
        screen_rect = pygame.Rect(0, 0, surface.get_width(), surface.get_height())
        if rect.colliderect(screen_rect):
            surface.blit(self.rotated_image, rect)

class StreakManager:
    """Manages multiple animated streaks along grid lines."""
    
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.streaks = []
        self.streak_image = None
        self.rotated_streak_image = None  # Pre-rotated image for performance
        self.spawn_timer = 0
        self.spawn_interval = 0.67  # Spawn every 0.67 seconds (tripled rate from 2.0)
        self.max_streaks = 8  # Limit total streaks for performance
        
        # Grid line cooldown system
        # Calculate grid line parameters first
        self.angle_rad = math.radians(GRID_ANGLE)
        self.slope = math.tan(self.angle_rad)
        
        # Calculate grid starting position to match grid generation
        self.grid_start_x = -abs(self.slope * self.height) - GRID_SPACING
        self.num_grid_lines = int((self.width + 2 * abs(self.slope * self.height) + 2 * GRID_SPACING) // GRID_SPACING) + 1
        self.line_cooldown = {}  # Track when each line was last used
        self.generation_counter = 0
        self.cooldown_generations = 3
        
        # Load streak image
        self.load_streak_image()
        
        # Load streak sound
        self.load_streak_sound()
        
        # Sound cooldown system
        self.sound_cooldown = {}  # Track when each sound was last used
        self.sound_generation_counter = 0
        self.sound_cooldown_generations = 3
    
    def load_streak_image(self):
        """Load streak image with fallback."""
        try:
            streak_path = Path(__file__).parent.parent.parent / "resources" / "streak.png"
            self.streak_image = pygame.image.load(str(streak_path))
            # Scale down if too large
            if self.streak_image.get_width() > 100:
                scale_factor = 100 / self.streak_image.get_width()
                new_size = (int(self.streak_image.get_width() * scale_factor),
                           int(self.streak_image.get_height() * scale_factor))
                self.streak_image = pygame.transform.scale(self.streak_image, new_size)
            print("✅ Loaded streak image")
        except Exception as e:
            print(f"⚠️  Could not load streak image: {e}")
            # Create fallback streak
            self.streak_image = pygame.Surface((50, 20), pygame.SRCALPHA)
            pygame.draw.ellipse(self.streak_image, CYAN, self.streak_image.get_rect())
        
        # Pre-rotate the streak image once for performance
        self.rotated_streak_image = pygame.transform.rotate(self.streak_image, GRID_ANGLE)
    
    def load_streak_sound(self):
        """Load multiple piano note sound effects."""
        self.streak_sounds = []
        audio_path = Path(__file__).parent / "audio"
        
        try:
            # Load all piano note files
            piano_files = list(audio_path.glob("piano*.mp3"))
            piano_files.sort()  # Sort for consistent ordering
            
            for sound_file in piano_files:
                sound = pygame.mixer.Sound(str(sound_file))
                sound.set_volume(0.15)  # Set volume to 15%
                self.streak_sounds.append(sound)
            
            if self.streak_sounds:
                print(f"✅ Loaded {len(self.streak_sounds)} piano note sounds")
            else:
                print("⚠️  No piano*.mp3 files found in audio directory")
        except Exception as e:
            print(f"⚠️  Could not load streak sounds: {e}")
            self.streak_sounds = []
    
    def spawn_streak(self):
        """Spawn a new streak on an available vertical grid line."""
        # Find available grid lines (not in cooldown) and not within 100px of screen edges
        available_lines = []
        current_generation = self.generation_counter
        
        for line_index in range(1, self.num_grid_lines):  # Skip first grid line (index 0)
            last_used_generation = self.line_cooldown.get(line_index, -999)
            if current_generation - last_used_generation >= self.cooldown_generations:
                # Calculate where this line would appear on screen
                line_x = self.grid_start_x + line_index * GRID_SPACING
                # Only include if the line is at least 100px from either edge
                if line_x >= 100 and line_x <= (self.width - 100):
                    available_lines.append(line_index)
        
        # If no lines available within safe zone, use any safe line (fallback)
        if not available_lines:
            for line_index in range(1, self.num_grid_lines):
                line_x = self.grid_start_x + line_index * GRID_SPACING
                if line_x >= 100 and line_x <= (self.width - 100):
                    available_lines.append(line_index)
        
        # If still no lines (shouldn't happen), skip spawning
        if not available_lines:
            return
        
        # Choose a random available line
        line_index = random.choice(available_lines)
        start_x = self.grid_start_x + line_index * GRID_SPACING
        
        # Mark this line as used
        self.line_cooldown[line_index] = current_generation
        self.generation_counter += 1
        
        # Start from top edge
        start_y = -100
        
        # Adjust start_x to account for the line's position at y = -100
        # Since the grid line goes from (x, 0) to (x + slope * height, height)
        # At y = -100, the line would be at x - slope * 100
        adjusted_start_x = start_x + self.slope * start_y  # start_y is negative, so this subtracts
        
        # Create new streak
        streak = Streak(float(adjusted_start_x), float(start_y), self.slope, self.rotated_streak_image)
        self.streaks.append(streak)
    
    def update(self, dt):
        """Update all streaks and spawn new ones."""
        # Update spawn timer - only spawn if under the limit
        self.spawn_timer += dt
        if self.spawn_timer >= self.spawn_interval and len(self.streaks) < self.max_streaks:
            self.spawn_streak()
            self.spawn_timer = 0
        
        # Update existing streaks
        for streak in self.streaks[:]:
            streak.update(dt)
            
            # Check if streak is 20% visible and actually on screen before playing sound
            if not streak.has_played_sound and streak.is_20_percent_visible(self.height):
                # Check if streak is actually on screen horizontally
                streak_width = streak.rotated_image.get_width()
                left_edge = streak.x - streak_width/2
                right_edge = streak.x + streak_width/2
                # Require at least 25% of the streak width to be on screen
                visible_width = min(right_edge, self.width) - max(left_edge, 0)
                on_screen_x = visible_width >= streak_width * 0.25
                
                if on_screen_x:
                    streak.has_played_sound = True
                    if self.streak_sounds:
                        # Find available sounds (not in cooldown)
                        available_sounds = []
                        current_generation = self.sound_generation_counter
                    
                        for i, sound in enumerate(self.streak_sounds):
                            last_used_generation = self.sound_cooldown.get(i, -999)
                            if current_generation - last_used_generation >= self.sound_cooldown_generations:
                                available_sounds.append((i, sound))
                        
                        # If no sounds available, use any sound (fallback)
                        if not available_sounds:
                            available_sounds = [(i, sound) for i, sound in enumerate(self.streak_sounds)]
                        
                        # Choose a random available sound
                        sound_index, chosen_sound = random.choice(available_sounds)
                        chosen_sound.play(loops=0)  # Explicitly play once, no looping
                        
                        # Mark this sound as used
                        self.sound_cooldown[sound_index] = current_generation
                        self.sound_generation_counter += 1
            
            # Remove off-screen streaks
            if streak.is_off_screen(self.width, self.height):
                self.streaks.remove(streak)
    
    def get_dirty_rectangles(self):
        """Get all dirty rectangles from moving streaks."""
        dirty_rects = []
        for streak in self.streaks:
            dirty_rects.append(streak.get_dirty_rect())
        return dirty_rects
    
    def draw(self, surface):
        """Draw all streaks."""
        for streak in self.streaks:
            streak.draw(surface)

class TextRenderer:
    """Handles text rendering with custom fonts."""
    
    def __init__(self):
        self.fonts = {}
        self.load_fonts()
    
    def load_fonts(self):
        """Load custom fonts with fallbacks."""
        base_path = Path(__file__).parent.parent.parent / "resources" / "fonts"
        
        font_configs = [
            ("bruno_large", "Bruno_Ace_SC/BrunoAceSC-Regular.ttf", 72),
            ("bruno_medium", "Bruno_Ace_SC/BrunoAceSC-Regular.ttf", 90),
            ("bruno_small", "Bruno_Ace_SC/BrunoAceSC-Regular.ttf", 48),
            ("bungee_large", "Bungee Hairline/BungeeHairline-Regular.ttf", 120),
            ("bungee_medium", "Bungee Hairline/BungeeHairline-Regular.ttf", 80),
        ]
        
        for font_name, font_file, size in font_configs:
            try:
                font_path = base_path / font_file
                if font_path.exists():
                    self.fonts[font_name] = pygame.font.Font(str(font_path), size)
                    print(f"✅ Loaded {font_name}: {font_file}")
                else:
                    raise FileNotFoundError(f"Font file not found: {font_path}")
            except Exception as e:
                # Fallback to system font
                self.fonts[font_name] = pygame.font.Font(None, size)
                print(f"⚠️  Using system font fallback for {font_name}: {e}")
        
        print("✅ Font loading complete")
    
    def render_text(self, text, font_name, color, center_pos=None):
        """Render text with specified font and return surface and rect."""
        font = self.fonts.get(font_name)
        if font is None:
            print(f"⚠️  Font '{font_name}' not found, using default")
            font = pygame.font.Font(None, 48)
        
        surface = font.render(text, True, color)
        
        if center_pos:
            rect = surface.get_rect(center=center_pos)
            return surface, rect
        else:
            return surface, surface.get_rect()

class TransformationManager:
    """Manages the transformation animation sequence."""
    
    def __init__(self, text_renderer):
        self.text_renderer = text_renderer
        self.reset()
    
    def reset(self):
        """Reset transformation state."""
        self.timer = 0
        self.phase = 0  # 0=normal, 1=glitch, 2=show_smarter, 3=typing
        self.fade_alpha = 255
        self.glitch_intensity = 0
        self.typing_progress = 0
        self.typing_text = "Level up your code"
        self.cursor_visible = True
        self.cursor_timer = 0
    
    def update(self, dt):
        """Update transformation animation."""
        self.timer += dt
        
        # Phase 0: Fade out countdown (0-1s)
        if self.timer < 1.0:
            self.phase = 0
            self.fade_alpha = int(255 * (1.0 - self.timer))
        
        # Phase 1: Glitch effect (1-7s) - extended for more "SMARTER DEV" intermixing
        elif self.timer < 7.0:
            self.phase = 1
            progress = (self.timer - 1.0) / 6.0  # Now 6 seconds long
            self.glitch_intensity = max(0, int(8 * (1.0 - progress)))  # Decrease intensity
        
        # Phase 2: Show Smarter Dev (7-9s)
        elif self.timer < 9.0:
            self.phase = 2
            self.glitch_intensity = 0
        
        # Phase 3: Typing effect (9+s) - removed brain logo phase
        else:
            self.phase = 3
            typing_elapsed = self.timer - 9.0
            chars_per_second = 12
            self.typing_progress = min(len(self.typing_text), int(typing_elapsed * chars_per_second))
            
            # Update cursor blink
            self.cursor_timer += dt
            if self.cursor_timer >= 0.5:
                self.cursor_visible = not self.cursor_visible
                self.cursor_timer = 0
    
    def draw_glitch_text(self, surface, text, font_name, base_pos, progress=0.0):
        """Draw text with flicker glitch effect."""
        if self.glitch_intensity <= 0:
            text_surface, text_rect = self.text_renderer.render_text(text, font_name, WHITE, base_pos)
            surface.blit(text_surface, text_rect)
            return
        
        # Create flickering pattern with longer random durations
        import time
        current_time = time.time()
        
        # Use a slower base speed and create longer visibility periods
        flicker_cycle = 0.3  # Each cycle lasts 300ms
        cycle_index = int(current_time / flicker_cycle)
        
        # Seed random with cycle index for consistent behavior within each cycle
        random.seed(cycle_index)
        
        # Random duration within each cycle (20% to 80% of cycle length)
        visibility_duration = random.uniform(0.2, 0.8) * flicker_cycle
        time_in_cycle = current_time % flicker_cycle
        
        # Determine visibility chance - starts high, gradually decreases
        base_visibility_chance = max(0.4, 1.0 - progress * 0.4)  # 100% to 60% visibility
        
        # Check if we're within the visible portion of this cycle
        should_show = time_in_cycle < visibility_duration and random.random() < base_visibility_chance
        
        # Reset random seed to avoid affecting other random calls
        random.seed()
        
        if should_show:
            # Vary opacity for flicker effect
            if progress < 0.3:
                opacity = random.choice([255, 230, 200])
            elif progress < 0.7:
                opacity = random.choice([230, 200, 170])
            else:
                opacity = random.choice([200, 170, 140])
            
            # No position offset - clean flicker without rumble
            text_surface, text_rect = self.text_renderer.render_text(text, font_name, WHITE, base_pos)
            text_surface.set_alpha(opacity)
            surface.blit(text_surface, text_rect)

class InteractiveReveal:
    """Main application class."""
    
    def __init__(self, width=SCREEN_WIDTH, height=SCREEN_HEIGHT, test_mode=False):
        pygame.init()
        pygame.mixer.init(frequency=22050, size=-16, channels=2, buffer=512)
        
        # Store screen dimensions
        self.width = width
        self.height = height
        self.test_mode = test_mode
        
        # Set up frameless display
        self.screen = pygame.display.set_mode((self.width, self.height), pygame.NOFRAME)
        pygame.display.set_caption("Smarter Dev Rebrand Reveal - Interactive")
        self.clock = pygame.time.Clock()
        
        # Initialize components
        self.grid_renderer = GridRenderer(self.width, self.height)
        self.streak_manager = StreakManager(self.width, self.height)
        self.text_renderer = TextRenderer()
        self.transformation_manager = TransformationManager(self.text_renderer)
        
        # Load background
        self.load_background()
        
        # Load and start background music
        self.load_background_music()
        
        # Load glitch sound effect
        self.load_glitch_sound()
        
        # Don't initialize webcam yet - wait for user to toggle it
        
        # Create working surface to avoid repeated copying
        self.working_surface = pygame.Surface((self.width, self.height))
        
        # State management
        self.state = "countdown"  # countdown, standby, transformation
        self.transformation_started = False
        self.transition_start_time = None
        self.transition_duration = 1.5  # 1.5 second crossfade
        self.show_fps = False  # Toggle for FPS counter
        self.show_grid = True  # Toggle for grid overlay (G key)
        self.show_webcam = False  # Toggle for webcam (W key)
        self.webcam = None  # Will be initialized when first toggled
        self.webcam_surface = None
        self.webcam_update_timer = 0  # Limit webcam update frequency
        self.webcam_update_interval = 1/15  # Update webcam at 15 FPS instead of 60
        self.webcam_thread = None  # Background thread for webcam
        self.latest_webcam_frame = None  # Latest frame from background thread
        self.webcam_thread_running = False
        
        # No longer need text dirty rectangle tracking since text is rendered after overlay
        
        # Set target datetime based on mode
        if self.test_mode:
            # Test mode: 5 seconds from now
            eastern_tz = pytz.timezone('US/Eastern')
            self.target_datetime = datetime.datetime.now(eastern_tz) + datetime.timedelta(seconds=5)
            print(f"🧪 Test target: {self.target_datetime.strftime('%H:%M:%S ET')}")
        else:
            # Production mode: August 8th noon ET
            self.target_datetime = TARGET_DATETIME
    
    def load_background(self):
        """Load background image with fallback."""
        try:
            bg_path = Path(__file__).parent.parent.parent / "resources" / "video-bg.png"
            self.background = pygame.image.load(str(bg_path))
            self.background = pygame.transform.scale(self.background, (self.width, self.height))
            print("✅ Loaded background image")
        except Exception as e:
            print(f"⚠️  Could not load background: {e}")
            # Create gradient background
            self.background = pygame.Surface((self.width, self.height))
            self.background.fill(DARK_BG)
    
    def load_background_music(self):
        """Load and start background music."""
        try:
            music_path = Path(__file__).parent / "audio" / "aurora.mp3"
            pygame.mixer.music.load(str(music_path))
            pygame.mixer.music.set_volume(0.4)  # Set volume to 40%
            pygame.mixer.music.play(-1)  # Loop indefinitely
            print("✅ Started background music")
        except Exception as e:
            print(f"⚠️  Could not load background music: {e}")
    
    def load_glitch_sound(self):
        """Load glitch sound effect."""
        try:
            glitch_path = Path(__file__).parent / "audio" / "glitch.mp3"
            if glitch_path.exists():
                self.glitch_sound = pygame.mixer.Sound(str(glitch_path))
                self.glitch_sound.set_volume(0.8)  # Set volume to 80%
                print("✅ Loaded glitch sound effect")
            else:
                print("⚠️  Glitch sound file not found")
                self.glitch_sound = None
        except Exception as e:
            print(f"⚠️  Could not load glitch sound: {e}")
            self.glitch_sound = None
    
    def init_webcam(self):
        """Initialize webcam for video capture."""
        try:
            import cv2
            print("🔍 OpenCV imported successfully")
            
            # Try different camera backends/indices for macOS compatibility
            camera_configs = [
                (0, cv2.CAP_AVFOUNDATION),  # macOS AVFoundation backend
                (0, cv2.CAP_ANY),           # Default backend
                (1, cv2.CAP_AVFOUNDATION),  # Try index 1 with AVFoundation
                (0, None),                  # Fallback without specifying backend
            ]
            
            for i, (index, backend) in enumerate(camera_configs):
                print(f"🔍 Trying camera config {i+1}: index={index}, backend={backend}")
                try:
                    if backend is not None:
                        self.webcam = cv2.VideoCapture(index, backend)
                    else:
                        self.webcam = cv2.VideoCapture(index)
                    
                    print(f"🔍 VideoCapture created: {self.webcam}")
                    
                    if self.webcam.isOpened():
                        print("✅ Webcam initialized and opened")
                        # Try to read a test frame
                        ret, frame = self.webcam.read()
                        print(f"🔍 Test frame read: ret={ret}, frame={'None' if frame is None else 'OK'}")
                        if ret:
                            print(f"🔍 Frame shape: {frame.shape}")
                            
                            # Set lower resolution for better performance
                            self.webcam.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
                            self.webcam.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
                            # Set lower FPS at camera level too
                            self.webcam.set(cv2.CAP_PROP_FPS, 15)
                            
                            print(f"✅ Successfully initialized webcam with config {i+1}")
                            print(f"🔍 Set webcam to 320x240@15fps for better performance")
                            return  # Success! Exit the function
                        else:
                            print("⚠️  Could not read test frame")
                            self.webcam.release()
                            self.webcam = None
                    else:
                        print("⚠️  Could not open webcam with this config")
                        if self.webcam:
                            self.webcam.release()
                        self.webcam = None
                
                except Exception as config_e:
                    print(f"⚠️  Config {i+1} failed: {config_e}")
                    if hasattr(self, 'webcam') and self.webcam:
                        self.webcam.release()
                    self.webcam = None
            
            # If we get here, all configs failed
            print("⚠️  All camera configurations failed")
            self.webcam = None
        except ImportError:
            print("⚠️  OpenCV not available, webcam disabled")
            self.webcam = None
        except Exception as e:
            print(f"⚠️  Could not initialize webcam: {e}")
            self.webcam = None
    
    def webcam_background_thread(self):
        """Background thread to continuously grab webcam frames."""
        import cv2
        while self.webcam_thread_running and self.webcam and self.webcam.isOpened():
            try:
                ret, frame = self.webcam.read()
                if ret:
                    self.latest_webcam_frame = frame
                else:
                    # If read fails, stop the thread
                    break
            except Exception as e:
                print(f"⚠️  Webcam background thread error: {e}")
                break
        
        self.webcam_thread_running = False
    
    def start_webcam_thread(self):
        """Start the background webcam thread."""
        if not self.webcam_thread_running and self.webcam:
            import threading
            self.webcam_thread_running = True
            self.webcam_thread = threading.Thread(target=self.webcam_background_thread, daemon=True)
            self.webcam_thread.start()
            print("🎥 Started webcam background thread")
    
    def stop_webcam_thread(self):
        """Stop the background webcam thread."""
        if self.webcam_thread_running:
            self.webcam_thread_running = False
            if self.webcam_thread and self.webcam_thread.is_alive():
                self.webcam_thread.join(timeout=1.0)  # Wait up to 1 second
            print("🎥 Stopped webcam background thread")
    
    def update_webcam(self):
        """Update webcam display using latest frame from background thread."""
        if not self.latest_webcam_frame is None:
            try:
                import cv2
                frame = self.latest_webcam_frame
                webcam_size = 150  # Diameter of circle
                
                # Resize frame immediately to reduce processing overhead
                frame_resized = cv2.resize(frame, (webcam_size, webcam_size))
                
                # Convert BGR to RGB and flip in one operation
                frame_rgb = cv2.flip(cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB), 1)
                
                # Convert to pygame surface more efficiently
                frame_surface = pygame.surfarray.make_surface(frame_rgb.swapaxes(0, 1))
                
                # Create circular webcam surface with pre-cached mask if possible
                if not hasattr(self, '_webcam_mask') or self._webcam_mask is None:
                    # Cache the circular mask to avoid recreating it every frame
                    self._webcam_mask = pygame.Surface((webcam_size, webcam_size), pygame.SRCALPHA)
                    pygame.draw.circle(self._webcam_mask, (255, 255, 255, 255), 
                                     (webcam_size//2, webcam_size//2), webcam_size//2)
                
                # Create webcam surface
                webcam_surface = pygame.Surface((webcam_size, webcam_size), pygame.SRCALPHA)
                webcam_surface.blit(frame_surface, (0, 0))
                webcam_surface.blit(self._webcam_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
                
                self.webcam_surface = webcam_surface
            except Exception as e:
                print(f"⚠️  Error updating webcam: {e}")
    
    def draw_webcam(self, surface):
        """Draw webcam in bottom right corner as a circle."""
        if not self.show_webcam or not self.webcam_surface:
            return
        
        # Position in bottom right corner with some padding
        webcam_size = 150
        padding = 20
        x = self.width - webcam_size - padding
        y = self.height - webcam_size - padding
        
        # Draw border circle
        border_color = WHITE
        border_width = 3
        pygame.draw.circle(surface, border_color, 
                         (x + webcam_size//2, y + webcam_size//2), 
                         webcam_size//2 + border_width, border_width)
        
        # Draw webcam feed
        surface.blit(self.webcam_surface, (x, y))
    
    def get_time_remaining(self):
        """Calculate time remaining until target datetime."""
        eastern_tz = pytz.timezone('US/Eastern')
        now = datetime.datetime.now(eastern_tz)
        
        if now >= self.target_datetime:
            return None  # Countdown finished
        
        delta = self.target_datetime - now
        
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        return {
            'days': days,
            'hours': hours, 
            'minutes': minutes,
            'seconds': seconds
        }
    
    def handle_events(self):
        """Handle pygame events."""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    return False
                
                elif event.key == pygame.K_SPACE:
                    if self.state == "standby" and not self.transformation_started:
                        print("🚀 Space pressed! Starting transformation...")
                        self.transformation_started = True
                        self.transformation_manager.reset()
                        self.state = "transformation"
                        
                        # Stop background music and play glitch sound on loop
                        pygame.mixer.music.stop()
                        if self.glitch_sound:
                            self.glitch_sound.play(-1)  # Loop indefinitely
                            print("🎵 Stopped background music, playing glitch sound (looping)")
                
                elif event.key == pygame.K_f:
                    self.show_fps = not self.show_fps
                    print(f"📊 FPS counter {'enabled' if self.show_fps else 'disabled'}")
                
                elif event.key == pygame.K_g:
                    self.show_grid = not self.show_grid
                    # Invalidate cache when toggling grid
                    self.grid_renderer.cached_blend_surface = None
                    print(f"🌐 Grid overlay {'enabled' if self.show_grid else 'disabled'}")
                
                elif event.key == pygame.K_w:
                    if not self.show_webcam:
                        # Turning webcam ON - initialize if needed
                        if self.webcam is None:
                            print("📷 Initializing webcam...")
                            self.init_webcam()
                        
                        if self.webcam is not None:
                            self.show_webcam = True
                            self.start_webcam_thread()  # Start background thread
                            print("📷 Webcam enabled")
                        else:
                            print("📷 Failed to initialize webcam")
                    else:
                        # Turning webcam OFF - release resources
                        self.show_webcam = False
                        self.stop_webcam_thread()  # Stop background thread
                        if self.webcam is not None:
                            self.webcam.release()
                            self.webcam = None
                        self.webcam_surface = None
                        self.latest_webcam_frame = None
                        print("📷 Webcam disabled and released")
        
        return True
    
    def update(self, dt, current_time):
        """Update game state."""
        
        # Mark dirty rectangles from streak movement before updating
        if self.show_grid:
            for dirty_rect in self.streak_manager.get_dirty_rectangles():
                self.grid_renderer.mark_dirty_rect(dirty_rect)
        
        
        # Update streak animations
        self.streak_manager.update(dt)
        
        # Check countdown state
        time_remaining = self.get_time_remaining()
        if time_remaining is None and self.state == "countdown":
            self.state = "countdown_to_standby_transition"
            self.transition_start_time = current_time
            print("🎯 Countdown finished! Starting crossfade to standby...")
        
        # Handle crossfade transition
        if self.state == "countdown_to_standby_transition":
            if current_time - self.transition_start_time >= self.transition_duration:
                self.state = "standby"
                self.transition_start_time = None
                # Force text cache refresh
                if hasattr(self.text_renderer, 'fonts'):
                    pass  # Text renderer should re-render
                print("🎯 Crossfade complete! Press SPACE to begin transformation...")
        
        # Update transformation if active
        if self.state == "transformation":
            prev_phase = self.transformation_manager.phase
            self.transformation_manager.update(dt)
            
            # Stop glitch sound when glitch phase ends (phase 1 -> phase 2)
            if prev_phase == 1 and self.transformation_manager.phase == 2:
                if hasattr(self, 'glitch_sound') and self.glitch_sound:
                    self.glitch_sound.stop()
                    print("🎵 Glitch effect ended, stopped glitch sound")
        
        # Update webcam if enabled (but limit frequency for performance)
        if self.show_webcam and self.webcam:
            self.webcam_update_timer += dt
            if self.webcam_update_timer >= self.webcam_update_interval:
                self.update_webcam()
                self.webcam_update_timer = 0
    
    def render(self, current_time):
        """Render the current frame."""
        # Use working surface instead of copying background each frame
        self.working_surface.blit(self.background, (0, 0))
        
        # Draw streaks on working surface
        self.streak_manager.draw(self.working_surface)
        
        # Apply grid overlay blend if enabled (without text)
        if self.show_grid:
            blended_surface = self.grid_renderer.apply_overlay_blend_optimized(self.working_surface)
            self.screen.blit(blended_surface, (0, 0))
        else:
            # Skip expensive grid overlay blend
            self.screen.blit(self.working_surface, (0, 0))
        
        # Draw text content on top of blended surface (directly to screen)
        if self.state == "countdown":
            self.draw_countdown_state(self.screen)
        elif self.state == "countdown_to_standby_transition":
            self.draw_countdown_to_standby_transition(self.screen, current_time)
        elif self.state == "standby":
            self.draw_standby_state(self.screen)
        elif self.state == "transformation":
            self.draw_transformation_state(self.screen)
        
        # Debug info
        self.draw_debug_info()
        
        # Draw webcam overlay (after everything else)
        self.draw_webcam(self.screen)
        
        pygame.display.flip()
    
    def draw_countdown_state(self, surface):
        """Draw countdown state."""
        # Draw "Beginner.Codes" title
        title_surface, title_rect = self.text_renderer.render_text(
            "BEGINNER.CODES", "bungee_large", WHITE, (self.width // 2, self.height // 2)
        )
        surface.blit(title_surface, title_rect)
        
        # Draw countdown with monospaced appearance
        time_remaining = self.get_time_remaining()
        if time_remaining:
            # Format: dd hh mm ss
            countdown_parts = [
                f"{time_remaining['days']:02d}",
                f"{time_remaining['hours']:02d}", 
                f"{time_remaining['minutes']:02d}",
                f"{time_remaining['seconds']:02d}"
            ]
        else:
            # Show final countdown when time is up
            countdown_parts = ["00", "00", "00", "00"]
        
        # Get the width of a "0" character to use as reference for spacing
        zero_surface, _ = self.text_renderer.render_text("0", "bruno_large", CYAN, (0, 0))
        char_width = zero_surface.get_width()
        space_width = char_width // 2  # Space between groups
        
        # Calculate total width needed for proper centering
        total_chars = 8  # 8 digits total (2+2+2+2)
        total_spaces = 3  # 3 spaces between groups
        total_width = total_chars * char_width + total_spaces * space_width
        
        # Start position (centered)
        start_x = (self.width - total_width) // 2
        y_pos = self.height // 2 + 150
        
        # Draw each digit group with proper spacing
        current_x = start_x
        for i, part in enumerate(countdown_parts):
            # Draw each digit in this part
            for digit in part:
                # Render the digit
                digit_surface, _ = self.text_renderer.render_text(digit, "bruno_large", CYAN, (0, 0))
                
                # Center the digit within the character width
                digit_x = current_x + (char_width - digit_surface.get_width()) // 2
                digit_rect = digit_surface.get_rect(center=(digit_x + digit_surface.get_width()//2, y_pos))
                
                surface.blit(digit_surface, digit_rect)
                current_x += char_width
            
            # Add space between groups (but not after the last group)
            if i < len(countdown_parts) - 1:
                current_x += space_width
    
    def draw_standby_state(self, surface):
        """Draw standby state."""
        # Draw "Beginner.Codes" title
        title_surface, title_rect = self.text_renderer.render_text(
            "BEGINNER.CODES", "bungee_large", WHITE, (self.width // 2, self.height // 2)
        )
        surface.blit(title_surface, title_rect)
        
        # Draw "Standby" message
        standby_surface, standby_rect = self.text_renderer.render_text(
            "STANDBY FOR SOMETHING EPIC!", "bruno_small", CYAN, (self.width // 2, self.height // 2 + 150)
        )
        surface.blit(standby_surface, standby_rect)
    
    def draw_countdown_to_standby_transition(self, surface, current_time):
        """Draw crossfade transition from countdown to standby."""
        if self.transition_start_time is None:
            return
        
        # Calculate fade progress (0.0 to 1.0)
        elapsed = current_time - self.transition_start_time
        fade_progress = min(1.0, elapsed / self.transition_duration)
        
        # Create surfaces for both states
        countdown_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        standby_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        
        # Draw countdown state on its surface
        # Title (same for both)
        title_surface, title_rect = self.text_renderer.render_text(
            "BEGINNER.CODES", "bungee_large", WHITE, (self.width // 2, self.height // 2)
        )
        countdown_surface.blit(title_surface, title_rect)
        standby_surface.blit(title_surface, title_rect)
        
        # Countdown text (fading out) - using same monospaced logic
        countdown_parts = ["00", "00", "00", "00"]  # Final countdown
        
        # Get the width of a "0" character to use as reference for spacing
        zero_surface, _ = self.text_renderer.render_text("0", "bruno_large", CYAN, (0, 0))
        char_width = zero_surface.get_width()
        space_width = char_width // 2  # Space between groups
        
        # Calculate total width needed for proper centering
        total_chars = 8  # 8 digits total (2+2+2+2)
        total_spaces = 3  # 3 spaces between groups
        total_width = total_chars * char_width + total_spaces * space_width
        
        # Start position (centered)
        start_x = (self.width - total_width) // 2
        y_pos = self.height // 2 + 150
        
        # Draw each digit group with proper spacing on countdown surface
        current_x = start_x
        for i, part in enumerate(countdown_parts):
            # Draw each digit in this part
            for digit in part:
                # Render the digit
                digit_surface, _ = self.text_renderer.render_text(digit, "bruno_large", CYAN, (0, 0))
                
                # Center the digit within the character width
                digit_x = current_x + (char_width - digit_surface.get_width()) // 2
                digit_rect = digit_surface.get_rect(center=(digit_x + digit_surface.get_width()//2, y_pos))
                
                countdown_surface.blit(digit_surface, digit_rect)
                current_x += char_width
            
            # Add space between groups (but not after the last group)
            if i < len(countdown_parts) - 1:
                current_x += space_width
        
        # Standby text (fading in)
        standby_text_surface, standby_text_rect = self.text_renderer.render_text(
            "STANDBY FOR SOMETHING EPIC!", "bruno_small", CYAN, (self.width // 2, self.height // 2 + 150)
        )
        standby_surface.blit(standby_text_surface, standby_text_rect)
        
        # Apply alpha blending
        countdown_alpha = int(255 * (1.0 - fade_progress))
        standby_alpha = int(255 * fade_progress)
        
        countdown_surface.set_alpha(countdown_alpha)
        standby_surface.set_alpha(standby_alpha)
        
        # Blit both surfaces
        surface.blit(countdown_surface, (0, 0))
        surface.blit(standby_surface, (0, 0))
    
    def draw_transformation_state(self, surface):
        """Draw transformation sequence."""
        tm = self.transformation_manager
        
        if tm.phase == 0:  # Transition to glitch (no fade)
            # Just show normal text, no fade effect
            title_surface, title_rect = self.text_renderer.render_text(
                "BEGINNER.CODES", "bungee_large", WHITE, (self.width // 2, self.height // 2)
            )
            surface.blit(title_surface, title_rect)
            
        elif tm.phase == 1:  # Glitch
            # Calculate progress through glitch phase (0.0 to 1.0)
            glitch_progress = (tm.timer - 1.0) / 6.0  # Phase 1 runs from 1s to 7s
            
            # Start showing "SMARTER DEV" earlier and more frequently (after 33% progress)
            if glitch_progress > 0.33 and random.random() < (glitch_progress - 0.33) / 0.67 * 0.4:  # Up to 40% chance at end
                # Show "SMARTER DEV" flicker
                tm.draw_glitch_text(surface, "SMARTER DEV", "bungee_large", (self.width // 2, self.height // 2), glitch_progress)
            else:
                # Show "BEGINNER.CODES" flicker
                tm.draw_glitch_text(surface, "BEGINNER.CODES", "bungee_large", (self.width // 2, self.height // 2), glitch_progress)
            
        elif tm.phase >= 2:  # Show Smarter Dev and beyond
            # Draw "SMARTER" in white
            smarter_surface, smarter_rect = self.text_renderer.render_text(
                "SMARTER", "bungee_large", WHITE
            )
            
            # Draw "DEV" in cyan  
            dev_surface, dev_rect = self.text_renderer.render_text(
                "DEV", "bruno_medium", CYAN
            )
            
            # Position side by side, centered (reduced gap from 50 to 20)
            gap = 20
            total_width = smarter_rect.width + gap + dev_rect.width
            start_x = (self.width - total_width) // 2
            center_y = self.height // 2
            
            smarter_pos = (start_x + smarter_rect.width // 2, center_y)
            dev_pos = (start_x + smarter_rect.width + gap + dev_rect.width // 2, center_y)
            
            smarter_surface, smarter_rect = self.text_renderer.render_text("SMARTER", "bungee_large", WHITE, smarter_pos)
            dev_surface, dev_rect = self.text_renderer.render_text("DEV", "bruno_medium", CYAN, dev_pos)
            
            surface.blit(smarter_surface, smarter_rect)
            surface.blit(dev_surface, dev_rect)
            
            # Draw typing effect (no brain logo)
            if tm.phase >= 3 and tm.typing_progress > 0:
                current_text = tm.typing_text[:tm.typing_progress]
                typing_surface, typing_rect = self.text_renderer.render_text(
                    current_text, "bruno_small", WHITE, (self.width // 2, self.height // 2 + 200)
                )
                surface.blit(typing_surface, typing_rect)
                
                # Draw cursor
                if tm.typing_progress < len(tm.typing_text) or tm.cursor_visible:
                    cursor_x = typing_rect.right + 10
                    cursor_surface, cursor_rect = self.text_renderer.render_text(
                        "_", "bruno_small", WHITE, (cursor_x, self.height // 2 + 200)
                    )
                    surface.blit(cursor_surface, cursor_rect)
    
    def draw_debug_info(self):
        """Draw debug information."""
        # Show FPS only if toggled on
        if self.show_fps:
            debug_font = pygame.font.Font(None, 32)
            fps_value = self.clock.get_fps()
            fps_text = f"FPS: {fps_value:.1f}"
            fps_surface = debug_font.render(fps_text, True, WHITE)
            
            # Center the FPS display at the top of the screen
            fps_x = (self.width - fps_surface.get_width()) // 2
            fps_y = 10
            
            # Add a semi-transparent background to make it more visible
            bg_rect = pygame.Rect(fps_x - 2, fps_y - 2, fps_surface.get_width() + 4, fps_surface.get_height() + 4)
            pygame.draw.rect(self.screen, (0, 0, 0, 128), bg_rect)
            self.screen.blit(fps_surface, (fps_x, fps_y))
    
    def run(self):
        """Main game loop."""
        print("🚀 Starting Interactive Smarter Dev Rebrand Reveal...")
        if self.test_mode:
            print(f"🎯 Test Target: {self.target_datetime.strftime('%H:%M:%S ET')} (5 seconds)")
        else:
            print(f"🎯 Target: {self.target_datetime.strftime('%B %d, %Y at %I:%M %p ET')}")
        print("💡 Press CTRL+C to exit, or ESC key")
        
        try:
            running = True
            while running:
                dt = self.clock.tick(FPS) / 1000.0  # Delta time in seconds
                
                import time
                current_time = time.time()
                
                running = self.handle_events()
                self.update(dt, current_time)
                self.render(current_time)
        
        except KeyboardInterrupt:
            print("\n👋 Exiting...")
        
        # Cleanup webcam if it was initialized
        if hasattr(self, 'webcam') and self.webcam:
            self.stop_webcam_thread()  # Stop background thread first
            self.webcam.release()
        
        pygame.quit()
        sys.exit()

def main():
    """Main function with command-line argument parsing."""
    parser = argparse.ArgumentParser(description="Interactive Smarter Dev Rebrand Reveal")
    parser.add_argument("--720p", action="store_true", help="Use 720p resolution (1280x720) instead of 1080p")
    parser.add_argument("--test", action="store_true", help="Set countdown to 5 seconds from program start for testing")
    
    args = parser.parse_args()
    
    if args.__dict__['720p']:
        width, height = 1280, 720
        print("🖥️  Using 720p resolution (1280x720)")
    else:
        width, height = SCREEN_WIDTH, SCREEN_HEIGHT
        print("🖥️  Using 1080p resolution (1920x1080)")
    
    if args.test:
        print("🧪 TEST MODE: Countdown set to 5 seconds from now")
    
    app = InteractiveReveal(width, height, test_mode=args.test)
    app.run()

if __name__ == "__main__":
    main()