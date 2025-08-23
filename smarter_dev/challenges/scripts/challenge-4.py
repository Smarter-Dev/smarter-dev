import random
import json

def generate_hex_color():
    return f"#{random.randint(0, 255):02X}{random.randint(0, 255):02X}{random.randint(0, 255):02X}"

def generate_grayscale():
    val = random.randint(0, 255)
    return f"#{val:02X}{val:02X}{val:02X}"

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

# Generate random colors
num_colors = random.randint(100, 500)
colors = []
blocked_count = 0

for _ in range(num_colors):
    # Sometimes generate grayscale colors
    if random.random() < 0.1:  # 10% grayscale
        color = generate_grayscale()
    else:
        color = generate_hex_color()
    
    colors.append(color)
    if should_block(color):
        blocked_count += 1

input_text = "\n".join(colors)
result = str(blocked_count)

output = {
    "input": input_text,
    "result": result
}

print(json.dumps(output))
