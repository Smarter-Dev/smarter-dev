import sys

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

# Read input
lines = sys.stdin.read().strip().split('\n')

# Count valid sequences
valid_count = 0
for line in lines:
    sequence = line.split()
    if is_valid_sequence(sequence):
        valid_count += 1

print(valid_count)
