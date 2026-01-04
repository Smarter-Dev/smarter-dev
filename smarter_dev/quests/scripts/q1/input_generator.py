import random
import json

a = random.randint(1, 50)
b = random.randint(1, 50)

output = {
    "input": f"{a} {b}",
    "result": str(a + b)
}

print(json.dumps(output))