import sys

data = sys.stdin.read().strip()
a, b = map(int, data.split())
print(a + b)