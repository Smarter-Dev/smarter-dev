import random
import json

def generate_hex_color():
    return f"#{random.randint(0, 255):02X}{random.randint(0, 255):02X}{random.randint(0, 255):02X}"

def is_reconnaissance(hex_color):
    # Remove the # and convert to RGB
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    
    # Check if sum divisible by 3 AND red > blue
    rgb_sum = r + g + b
    return (rgb_sum % 3 == 0) and (r > b)

# Generate random input
num_colors = random.randint(100, 500)
colors = []
recon_count = 0

for _ in range(num_colors):
    color = generate_hex_color()
    colors.append(color)
    if is_reconnaissance(color):
        recon_count += 1

input_text = "\n".join(colors)
result = str(recon_count)

output = {
    "input": input_text,
    "result": result
}

print(json.dumps(output))
