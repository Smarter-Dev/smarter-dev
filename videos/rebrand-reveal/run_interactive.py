#!/usr/bin/env python3
"""
Interactive Reveal Launcher
Simple launcher for the interactive rebrand reveal.
"""

import sys
import os
from pathlib import Path

def main():
    print("🚀 Launching Interactive Smarter Dev Rebrand Reveal...")
    
    # Add current directory to path
    current_dir = Path(__file__).parent
    sys.path.insert(0, str(current_dir))
    
    try:
        # Import and run the interactive reveal with command-line args
        from interactive_reveal import main as interactive_main
        interactive_main()
        
    except KeyboardInterrupt:
        print("\n👋 Interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()