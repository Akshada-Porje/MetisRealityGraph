"""One cheap call to confirm your key + model work: python -m metis.smoke"""
from .llm import GeminiClient

c = GeminiClient(cache_dir=None)
r = c.generate_json('Reply with JSON only.', 'Return {"ok": true, "word": "metis"}')
print("model:", c.model, "| parsed:", r.parsed, "| tokens:", r.prompt_tokens, r.output_tokens)
