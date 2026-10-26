import json
import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from app.main import app

def export_openapi():
    """Extract OpenAPI schema from FastAPI app and save to openapi.json."""
    openapi_schema = app.openapi()
    output_path = root_dir / "openapi.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(openapi_schema, f, indent=2)
    print(f"Successfully exported OpenAPI schema to {output_path}")

if __name__ == "__main__":
    export_openapi()
