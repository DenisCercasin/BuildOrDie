"""Write JSON Schemas for the shared contracts to schemas/ (share these with teammates)."""

import json
from pathlib import Path

from decision_engine import models

OUT = Path(__file__).parent / "schemas"
OUT.mkdir(exist_ok=True)

for name in ["BookRequest", "MerchantPolicy", "MerchantOffer", "EvaluateInput", "EngineResult", "EngineConfig"]:
    model = getattr(models, name)
    mode = "serialization" if name == "EngineResult" else "validation"
    (OUT / f"{name}.json").write_text(json.dumps(model.model_json_schema(mode=mode), indent=2))
    print(f"wrote schemas/{name}.json")
