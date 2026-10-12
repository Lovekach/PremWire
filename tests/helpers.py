from premwire.config import EditorConfig

def editor_config() -> EditorConfig:
    return EditorConfig(enabled=True, provider='ollama', model='qwen3.5:4b',
                        base_url='http://127.0.0.1:11434', timeout_seconds=240,
                        max_source_chars=8000, max_output_chars=900, temperature=0.3)
