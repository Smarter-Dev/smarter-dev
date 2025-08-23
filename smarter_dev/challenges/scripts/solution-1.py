import sys

def is_reconnaissance(hex_color):
    # Remove the # and convert to RGB
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    
    # Check if sum divisible by 3 AND red > blue
    rgb_sum = r + g + b
    return (rgb_sum % 3 == 0) and (r > b)

# Read input
lines = sys.stdin.readlines()

# Count reconnaissance packets
recon_count = 0
for color in lines:
    if is_reconnaissance(color):
        recon_count += 1

print(recon_count)
