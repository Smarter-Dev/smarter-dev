def validate(user_output: str, expected_output: str) -> bool:
    try:
        return user_output.strip() == expected_output.strip()
    except Exception:
        return False