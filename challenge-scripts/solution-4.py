import sys

def should_block(hex_color):
    # Remove the # and convert to RGB
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    
    # Check if grayscale (never block)
    if r == g == b:
        return False
    
    # Check all three conditions
    condition1 = r > 128
    condition2 = g < b
    condition3 = (r + g + b) % 2 == 1  # Sum is odd
    
    return condition1 and condition2 and condition3

# Read input
lines = sys.stdin.read().strip().split('\n')

# Count blocked colors
blocked_count = 0
for color in lines:
    if should_block(color):
        blocked_count += 1

print(blocked_count)
