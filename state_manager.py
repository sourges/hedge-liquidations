import json
import os

class StateManager:
    def __init__(self, filepath="deal_state.json"):
        self.filepath = filepath

    def save_state(self, state_data):
        """Saves the current dictionary state to a local JSON file safely."""
        try:
            with open(self.filepath, "w") as f:
                json.dump(state_data, f, indent=4)
        except Exception as e:
            print(f"⚠️ Error saving state to disk: {e}")

    def load_state(self):
        """Loads the previous state if it exists, otherwise returns None."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    state_data = json.load(f)
                    print(f"📂 Found existing deal state saved on disk. Resuming...")
                    return state_data
            except Exception as e:
                print(f"⚠️️ Error reading state file: {e}")
        return None