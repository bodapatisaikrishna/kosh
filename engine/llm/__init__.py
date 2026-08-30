"""Provider-agnostic LLM adapter for L3.

The brief specs the Anthropic SDK (claude-sonnet-5). No Anthropic key was
available when this was built; an NVIDIA NIM key was, which serves open models
(Llama, Nemotron, ...) via an OpenAI-compatible API, not Claude. Rather than
couple L3's agent loop to one SDK's message/tool-call shape, everything speaks
the small `LLMClient` protocol in base.py - `NimClient` (real, used for the
reference benchmark) and `AnthropicClient` (spec-complete, unit-tested against
a mocked SDK, ready for a real key) both implement it identically.
"""
