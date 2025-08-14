import random
import json

def generate_hex_color():
    return f"#{random.randint(0, 255):02X}{random.randint(0, 255):02X}{random.randint(0, 255):02X}"

def generate_valid_sequence():
    # Generate a valid sequence
    length = random.randint(4, 10)
    sequence = []
    
    # First color - need even green value
    r = random.randint(0, 255)
    g = random.randint(0, 127) * 2  # Ensure even
    b = random.randint(0, 255)
    sequence.append(f"#{r:02X}{g:02X}{b:02X}")
    
    # Middle colors - need ascending red values
    red_values = sorted([random.randint(0, 255) for _ in range(length - 2)])
    for red in red_values:
        g = random.randint(0, 255)
        b = random.randint(0, 255)
        sequence.append(f"#{red:02X}{g:02X}{b:02X}")
    
    # Last color - need blue divisible by 5
    r = random.randint(0, 255)
    g = random.randint(0, 255)
    b = random.randint(0, 51) * 5  # Ensure divisible by 5
    sequence.append(f"#{r:02X}{g:02X}{b:02X}")
    
    return sequence

def generate_invalid_sequence():
    # Generate a sequence that violates at least one rule
    length = random.randint(2, 10)  # Sometimes less than 4
    sequence = []
    
    for _ in range(length):
        sequence.append(generate_hex_color())
    
    return sequence

def is_valid_sequence(sequence):
    if len(sequence) < 4:
        return False
    
    # Check first color's green value
    first_g = int(sequence[0][3:5], 16)
    if first_g % 2 != 0:
        return False
    
    # Check last color's blue value
    last_b = int(sequence[-1][5:7], 16)
    if last_b % 5 != 0:
        return False
    
    # Check middle colors have ascending red values
    if len(sequence) > 2:
        prev_red = -1
        for color in sequence[1:-1]:
            red = int(color[1:3], 16)
            if red <= prev_red:
                return False
            prev_red = red
    
    return True

# Generate random sequences
num_sequences = random.randint(30, 150)
sequences = []
valid_count = 0

for _ in range(num_sequences):
    # Mix of valid and invalid sequences
    if random.random() < 0.3:  # 30% chance of valid
        sequence = generate_valid_sequence()
    else:
        sequence = generate_invalid_sequence()
    
    sequences.append(" ".join(sequence))
    if is_valid_sequence(sequence):
        valid_count += 1

input_text = "\n".join(sequences)
result = str(valid_count)

output = {
    "input": input_text,
    "result": result
}

print(json.dumps(output))
