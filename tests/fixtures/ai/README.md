Provider responses in each provider's documented wire format (status, headers, body).
They are hand-built, not captured from live calls: no API keys were available when they
were written. Replace them with real recordings when keys are available; the tests only
depend on the format.

- groq_profile_response.json: strict json_schema mode, x-ratelimit-* headers,
  Groq usage extras (queue_time, x_groq).
- deepseek_profile_response.json: json_object mode, reasoning_content (must be ignored),
  prompt_cache_hit_tokens / prompt_cache_miss_tokens usage fields.
- groq_score_response.json / deepseek_score_response.json: a scoring reply in each
  provider's format; the DeepSeek one includes reasoning_content containing a different,
  wrong JSON object, to prove only `content` is used.
