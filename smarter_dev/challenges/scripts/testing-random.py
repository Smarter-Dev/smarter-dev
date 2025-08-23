from random import randint
from json import dumps

x = randint(0, 100)
print(
    dumps(
        {
            "input": f"TESTING INPUT {x}",
            "result": f"TESTING RESULT {x}"
        }
    )
)

