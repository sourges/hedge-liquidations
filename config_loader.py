import json
import os

def load_config(filename="config.json"):
    """Loads bot configuration parameters from a JSON file."""
    if not os.path.exists(filename):
        raise FileNotFoundError(f"Configuration file '{filename}' not found! Please create it first.")
    
    with open(filename, "r") as f:
        config = json.load(f)
    
    print(f"⚙️ Successfully loaded configuration for symbol: {config.get('symbol')}")
    return config

if __name__ == "__main__":
    # Quick test when running this file directly
    cfg = load_config()
    print("Loaded Config Data:", cfg)