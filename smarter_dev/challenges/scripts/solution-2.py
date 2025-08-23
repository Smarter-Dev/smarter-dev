import sys

def calculate_size(hex_color):
    # Remove the # and convert to RGB
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    
    # Calculate size: (R × 256) + G + (B ÷ 16)
    return (r * 256) + g + (b // 16)

# Read input
lines = sys.stdin.read().strip().split('\n')

# Process each gradient line
total_size = 0
for line in lines:
    colors = line.split()
    
    # Extract colors at positions 2, 5, 8, 11, 14... (1-based indexing)
    for pos in range(2, len(colors) + 1, 3):
        if pos <= len(colors):
            total_size += calculate_size(colors[pos - 1])  # Convert to 0-based

print(total_size)
