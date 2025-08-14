import subprocess
import json
import sys

def run_challenge_generator(challenge_num):
    """Run the challenge generator script and return input/result"""
    with open(f'challenge-{challenge_num}.py', 'r') as f:
        code = f.read()
    
    # Create a namespace with necessary imports
    namespace = {}
    
    # Capture stdout from exec
    import io
    from contextlib import redirect_stdout
    
    stdout_capture = io.StringIO()
    with redirect_stdout(stdout_capture):
        exec(code, namespace)
    
    output = stdout_capture.getvalue()
    return json.loads(output)

def run_solution(solution_num, input_data):
    """Run the solution script with input and return output"""
    result = subprocess.run(
        [sys.executable, f'solution-{solution_num}.py'],
        input=input_data,
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        raise Exception(f"Solution {solution_num} crashed: {result.stderr}")
    
    return result.stdout.strip()

def validate_challenge(challenge_num, iterations=10000):
    """Validate a challenge/solution pair"""
    print(f"Validating Challenge {challenge_num}...")
    
    for i in range(iterations):
        if i % 1000 == 0:
            print(f"  Progress: {i}/{iterations}")
        
        # Generate test case
        try:
            test_case = run_challenge_generator(challenge_num)
        except Exception as e:
            print(f"\n❌ ERROR generating test case for Challenge {challenge_num}")
            print(f"Iteration: {i+1}")
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        # Run solution
        try:
            solution_output = run_solution(challenge_num, test_case['input'])
        except Exception as e:
            print(f"\n❌ ERROR running solution for Challenge {challenge_num}")
            print(f"Iteration: {i+1}")
            print(f"Error: {e}")
            print(f"\nInput (first 500 chars):")
            print(test_case['input'][:500])
            if len(test_case['input']) > 500:
                print(f"... (truncated, total length: {len(test_case['input'])})")
            print(f"\nExpected result: {test_case['result']}")
            return False
        
        # Compare results
        if solution_output != test_case['result']:
            print(f"\n❌ MISMATCH in Challenge {challenge_num}")
            print(f"Iteration: {i+1}")
            print(f"Expected: {test_case['result']}")
            print(f"Got: {solution_output}")
            print(f"\nInput (first 500 chars):")
            print(test_case['input'][:500])
            if len(test_case['input']) > 500:
                print(f"... (truncated, total length: {len(test_case['input'])})")
            
            # Show first few lines of input for debugging
            input_lines = test_case['input'].split('\n')
            print(f"\nFirst 5 lines of input:")
            for line in input_lines[:5]:
                print(f"  {line}")
            if len(input_lines) > 5:
                print(f"  ... ({len(input_lines)} total lines)")
            
            return False
    
    print(f"  ✅ Challenge {challenge_num} passed all {iterations} tests!")
    return True

def main():
    print("Starting validation of all challenges...")
    print("=" * 50)
    
    # Test all 4 challenges
    for challenge_num in range(1, 5):
        if not validate_challenge(challenge_num, iterations=10000):
            print(f"\n🛑 VALIDATION FAILED at Challenge {challenge_num}")
            print("Fix the issues above before continuing.")
            sys.exit(1)
    
    print("\n" + "=" * 50)
    print("🎉 ALL CHALLENGES VALIDATED SUCCESSFULLY!")
    print("All 40,000 test cases passed!")

if __name__ == "__main__":
    main()
