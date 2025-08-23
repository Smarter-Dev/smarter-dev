"""
Your input file contains a log of intercepted headers. Each line shows a hex color value like #A3F2B1. The attacker marks reconnaissance packets using a simple rule: if the sum of the color's RGB values (in decimal) is divisible by 3, AND the red value is greater than the blue value, then that packet is part of their scanning sequence.

Example:
#A3F2B1  → R=163, G=242, B=177 → Sum=582 → 582÷3=194 (divisible!) → R(163) < B(177) → NOT reconnaissance

#F23C4A  → R=242, G=60, B=74 → Sum=376 → 376÷3=125.33... (not divisible) → NOT reconnaissance  

#E14B3C  → R=225, G=75, B=60 → Sum=360 → 360÷3=120 (divisible!) → R(225) > B(60) → IS reconnaissance
"""

from pathlib import Path

def solution_1(lines):
    recon_count = 0
    for line in lines:
        if is_reconnaissance(line):
            recon_count += 1
    return recon_count

def is_reconnaissance(line):
    # Remove the # and convert to RGB
    r = int(line[1:3], 16)
    g = int(line[3:5], 16)
    b = int(line[5:7], 16)
    
    # Check if sum divisible by 3 AND red > blue
    rgb_sum = r + g + b
    return (rgb_sum % 3 == 0) and (r > b)

def solve():
    with open(Path(__file__).parent / 'input-c.txt', 'r') as file:
        lines = file.readlines()
        print(solution_1(lines)) 

def test():
    assert solution_1(["#A3F2B1", "#F23C4A", "#E14B3C"]) == 1
    print("All tests passed")

solve()