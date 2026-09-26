from dataclasses import dataclass

from pathlib import Path


@dataclass
class Config:
    """event_memory 全局配置。"""

    workspace_root: Path
    mount_max_tokens: int = 4096
    hot_cache_entries: int = 20
    retrieval_candidates: int = 5
    time_window_days: int = 30
    compress_threshold: float = 0.8
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    hot_keywords_max: int = 5
    soft_keywords_max: int = 8
