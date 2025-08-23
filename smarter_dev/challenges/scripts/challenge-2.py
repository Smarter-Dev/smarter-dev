import random
import json

def generate_hex_color():
    return f"#{random.randint(0, 255):02X}{random.randint(0, 255):02X}{random.randint(0, 255):02X}"

def calculate_size(hex_color):
    # Remove the # and convert to RGB
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    
    # Calculate size: (R × 256) + G + (B ÷ 16)
    return (r * 256) + g + (b // 16)

# Generate random gradient lines
num_lines = random.randint(20, 100)
lines = []
total_size = 0

for _ in range(num_lines):
    # Random number of colors per gradient (need at least 2 for extraction at position 2)
    num_colors = random.randint(5, 20)
    gradient = []
    
    for i in range(num_colors):
        gradient.append(generate_hex_color())
    
    # Extract colors at positions 2, 5, 8, 11, 14... (1-based indexing)
    for pos in range(2, num_colors + 1, 3):
        total_size += calculate_size(gradient[pos - 1])  # Convert to 0-based
    
    lines.append(" ".join(gradient))

input_text = "\n".join(lines)
result = str(total_size)

output = {
    "input": input_text,
    "result": result
}

print(json.dumps(output))
