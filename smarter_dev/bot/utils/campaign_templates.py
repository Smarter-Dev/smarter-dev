"""Campaign templates for quick setup.

This module provides predefined campaign templates to streamline campaign creation
with common configurations and challenge sets.
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any
from uuid import uuid4


class CampaignTemplate:
    """Base class for campaign templates."""
    
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
    
    def generate_config(self, guild_id: str, **kwargs) -> Dict[str, Any]:
        """Generate campaign configuration from template."""
        base_config = {
            "guild_id": guild_id,
            "campaign_type": kwargs.get("campaign_type", "player"),
            "state": "draft",
            "scoring_type": "time_based",
            "starting_points": 100,
            "points_decrease_step": 10,
            "release_delay_minutes": 1440,  # 24 hours
            "announcement_channel_id": kwargs.get("announcement_channel_id")
        }
        
        # Merge with template-specific config
        template_config = self.get_template_config(**kwargs)
        base_config.update(template_config)
        
        return base_config
    
    def get_template_config(self, **kwargs) -> Dict[str, Any]:
        """Get template-specific configuration. Override in subclasses."""
        return {}
    
    def get_challenges(self, **kwargs) -> List[Dict[str, Any]]:
        """Get template challenge definitions. Override in subclasses."""
        return []


class BeginnerPythonTemplate(CampaignTemplate):
    """Template for beginner Python programming challenges."""
    
    def __init__(self):
        super().__init__(
            name="Beginner Python",
            description="A series of beginner-friendly Python programming challenges"
        )
    
    def get_template_config(self, **kwargs) -> Dict[str, Any]:
        return {
            "name": kwargs.get("name", "Beginner Python Challenge"),
            "description": "Learn Python fundamentals through hands-on coding challenges. Perfect for beginners!",
            "scoring_type": "time_based",
            "starting_points": 100,
            "points_decrease_step": 5  # Gentler scoring for beginners
        }
    
    def get_challenges(self, **kwargs) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Hello World",
                "description": "Your first Python program",
                "difficulty_level": 1,
                "order_position": 1,
                "categories": ["basics", "syntax"],
                "problem_statement": """Write a Python program that prints "Hello, World!" to the console.

This is your first step into Python programming!""",
                "generation_script": """
import json
print(json.dumps({
    "input": "",
    "expected": "Hello, World!"
}))
""",
                "expected_output_format": "A single line of text: Hello, World!"
            },
            {
                "title": "Simple Calculator",
                "description": "Basic arithmetic operations",
                "difficulty_level": 2,
                "order_position": 2,
                "categories": ["basics", "math"],
                "problem_statement": """Create a program that takes two numbers and an operation (+, -, *, /) and returns the result.

Input format: number1 operation number2
Example: "5 + 3" should return 8""",
                "generation_script": """
import json
import random

operations = ['+', '-', '*', '/']
op = random.choice(operations)
a = random.randint(1, 20)
b = random.randint(1, 10) if op != '/' else random.randint(1, 10)

if op == '+':
    result = a + b
elif op == '-':
    result = a - b
elif op == '*':
    result = a * b
else:  # division
    result = a / b

print(json.dumps({
    "input": f"{a} {op} {b}",
    "expected": str(result)
}))
""",
                "expected_output_format": "The numeric result of the operation"
            },
            {
                "title": "Count Vowels",
                "description": "String manipulation and counting",
                "difficulty_level": 3,
                "order_position": 3,
                "categories": ["strings", "loops"],
                "problem_statement": """Write a program that counts the number of vowels (a, e, i, o, u) in a given string.

The input will be a single line of text.
Count both uppercase and lowercase vowels.""",
                "generation_script": """
import json
import random

words = [
    "programming", "python", "challenge", "algorithm", "function",
    "variable", "loop", "condition", "string", "integer"
]

word = random.choice(words)
vowels = "aeiouAEIOU"
count = sum(1 for char in word if char in vowels)

print(json.dumps({
    "input": word,
    "expected": str(count)
}))
""",
                "expected_output_format": "The number of vowels as an integer"
            },
            {
                "title": "Reverse String",
                "description": "String manipulation challenge",
                "difficulty_level": 3,
                "order_position": 4,
                "categories": ["strings"],
                "problem_statement": """Write a program that reverses a given string.

Input: A single line containing a string
Output: The string reversed""",
                "generation_script": """
import json
import random

words = ["hello", "world", "python", "code", "reverse", "string", "challenge"]
word = random.choice(words)
reversed_word = word[::-1]

print(json.dumps({
    "input": word,
    "expected": reversed_word
}))
""",
                "expected_output_format": "The reversed string"
            },
            {
                "title": "FizzBuzz",
                "description": "Classic programming challenge",
                "difficulty_level": 4,
                "order_position": 5,
                "categories": ["loops", "conditionals"],
                "problem_statement": """Write a program that prints numbers from 1 to N, but:
- For multiples of 3, print "Fizz" instead of the number
- For multiples of 5, print "Buzz" instead of the number  
- For multiples of both 3 and 5, print "FizzBuzz"

Input: A single integer N
Output: One line per number/word, separated by spaces""",
                "generation_script": """
import json
import random

n = random.randint(10, 20)
result = []

for i in range(1, n + 1):
    if i % 15 == 0:
        result.append("FizzBuzz")
    elif i % 3 == 0:
        result.append("Fizz")
    elif i % 5 == 0:
        result.append("Buzz")
    else:
        result.append(str(i))

print(json.dumps({
    "input": str(n),
    "expected": " ".join(result)
}))
""",
                "expected_output_format": "Numbers and words separated by spaces"
            }
        ]


class AlgorithmsTemplate(CampaignTemplate):
    """Template for algorithm and data structure challenges."""
    
    def __init__(self):
        super().__init__(
            name="Algorithms & Data Structures",
            description="Classic algorithms and data structure problems"
        )
    
    def get_template_config(self, **kwargs) -> Dict[str, Any]:
        return {
            "name": kwargs.get("name", "Algorithms Challenge"),
            "description": "Master fundamental algorithms and data structures through challenging problems.",
            "scoring_type": "time_based",
            "starting_points": 150,
            "points_decrease_step": 15
        }
    
    def get_challenges(self, **kwargs) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Binary Search",
                "description": "Implement binary search algorithm",
                "difficulty_level": 5,
                "order_position": 1,
                "categories": ["algorithms", "search"],
                "problem_statement": """Implement binary search to find a target value in a sorted array.

Input: First line contains the sorted array (space-separated integers)
Second line contains the target value

Output: The index of the target (0-based), or -1 if not found""",
                "generation_script": """
import json
import random

# Generate sorted array
size = random.randint(5, 15)
arr = sorted(random.sample(range(1, 50), size))

# Sometimes include target, sometimes don't
if random.choice([True, False]):
    target = random.choice(arr)
    expected = str(arr.index(target))
else:
    target = random.randint(51, 100)  # Not in array
    expected = "-1"

print(json.dumps({
    "input": f"{' '.join(map(str, arr))}\\n{target}",
    "expected": expected
}))
""",
                "expected_output_format": "Index as integer, or -1 if not found"
            },
            {
                "title": "Two Sum",
                "description": "Find two numbers that sum to target",
                "difficulty_level": 6,
                "order_position": 2,
                "categories": ["algorithms", "arrays"],
                "problem_statement": """Given an array of integers and a target sum, find two numbers that add up to the target.

Input: First line contains the array (space-separated integers)
Second line contains the target sum

Output: The indices of the two numbers (space-separated), or "None" if no solution""",
                "generation_script": """
import json
import random

size = random.randint(4, 10)
arr = [random.randint(1, 20) for _ in range(size)]

# Ensure there's a valid pair
i, j = random.sample(range(size), 2)
target = arr[i] + arr[j]

print(json.dumps({
    "input": f"{' '.join(map(str, arr))}\\n{target}",
    "expected": f"{i} {j}" if i < j else f"{j} {i}"
}))
""",
                "expected_output_format": "Two indices separated by space"
            },
            {
                "title": "Valid Parentheses",
                "description": "Check if parentheses are properly balanced",
                "difficulty_level": 6,
                "order_position": 3,
                "categories": ["algorithms", "stack"],
                "problem_statement": """Check if a string of parentheses is valid (properly balanced).

Valid parentheses: (), [], {}
Invalid: ([)], ((, etc.

Input: A string containing only parentheses characters
Output: "True" if valid, "False" if invalid""",
                "generation_script": """
import json
import random

def is_valid(s):
    stack = []
    mapping = {')': '(', '}': '{', ']': '['}
    
    for char in s:
        if char in mapping:
            if not stack or stack.pop() != mapping[char]:
                return False
        else:
            stack.append(char)
    
    return len(stack) == 0

# Generate test cases
parens = ['()', '[]', '{}']
if random.choice([True, False]):
    # Valid case
    s = ''.join(random.choices(parens, k=random.randint(1, 4)))
    expected = "True"
else:
    # Invalid case
    chars = list('()[]{')
    s = ''.join(random.choices(chars, k=random.randint(2, 6)))
    expected = "True" if is_valid(s) else "False"

print(json.dumps({
    "input": s,
    "expected": expected
}))
""",
                "expected_output_format": "True or False"
            }
        ]


class MathChallengeTemplate(CampaignTemplate):
    """Template for mathematical programming challenges."""
    
    def __init__(self):
        super().__init__(
            name="Math Challenge",
            description="Mathematical and computational problems"
        )
    
    def get_template_config(self, **kwargs) -> Dict[str, Any]:
        return {
            "name": kwargs.get("name", "Math Programming Challenge"),
            "description": "Solve mathematical problems through programming. Sharpen your problem-solving skills!",
            "scoring_type": "time_based",
            "starting_points": 120,
            "points_decrease_step": 10
        }
    
    def get_challenges(self, **kwargs) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Prime Numbers",
                "description": "Check if a number is prime",
                "difficulty_level": 4,
                "order_position": 1,
                "categories": ["math", "numbers"],
                "problem_statement": """Write a program to check if a given number is prime.

A prime number is a natural number greater than 1 that has no positive divisors other than 1 and itself.

Input: A single integer
Output: "True" if prime, "False" if not prime""",
                "generation_script": """
import json
import random

def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(n ** 0.5) + 1):
        if n % i == 0:
            return False
    return True

# Mix of prime and non-prime numbers
num = random.choice([2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 
                     4, 6, 8, 9, 10, 12, 14, 15, 16, 18, 20, 21, 22, 24, 25])

expected = "True" if is_prime(num) else "False"

print(json.dumps({
    "input": str(num),
    "expected": expected
}))
""",
                "expected_output_format": "True or False"
            },
            {
                "title": "Fibonacci Sequence",
                "description": "Calculate the nth Fibonacci number",
                "difficulty_level": 5,
                "order_position": 2,
                "categories": ["math", "sequences"],
                "problem_statement": """Calculate the nth number in the Fibonacci sequence.

The Fibonacci sequence: 0, 1, 1, 2, 3, 5, 8, 13, 21, 34, ...
F(0) = 0, F(1) = 1, F(n) = F(n-1) + F(n-2)

Input: A single integer n (0 ≤ n ≤ 30)
Output: The nth Fibonacci number""",
                "generation_script": """
import json
import random

def fibonacci(n):
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b

n = random.randint(0, 15)
expected = str(fibonacci(n))

print(json.dumps({
    "input": str(n),
    "expected": expected
}))
""",
                "expected_output_format": "The nth Fibonacci number as integer"
            },
            {
                "title": "Greatest Common Divisor",
                "description": "Find GCD of two numbers",
                "difficulty_level": 5,
                "order_position": 3,
                "categories": ["math", "algorithms"],
                "problem_statement": """Find the Greatest Common Divisor (GCD) of two positive integers.

Use the Euclidean algorithm: gcd(a, b) = gcd(b, a mod b)

Input: Two positive integers separated by space
Output: Their GCD""",
                "generation_script": """
import json
import random
import math

a = random.randint(10, 100)
b = random.randint(10, 100)
expected = str(math.gcd(a, b))

print(json.dumps({
    "input": f"{a} {b}",
    "expected": expected
}))
""",
                "expected_output_format": "The GCD as an integer"
            }
        ]


# Template registry
CAMPAIGN_TEMPLATES = {
    "beginner_python": BeginnerPythonTemplate(),
    "algorithms": AlgorithmsTemplate(),
    "math_challenge": MathChallengeTemplate()
}


def get_available_templates() -> Dict[str, CampaignTemplate]:
    """Get all available campaign templates."""
    return CAMPAIGN_TEMPLATES.copy()


def get_template(template_name: str) -> CampaignTemplate:
    """Get a specific template by name."""
    return CAMPAIGN_TEMPLATES.get(template_name)